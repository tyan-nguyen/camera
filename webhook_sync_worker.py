import json
import logging
import threading
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple

from database import SessionLocal
from models import ActionGroup, VehicleLog, WebhookSyncQueue, Camera
from webhook_service import webhook_circuit_breaker, test_action_group_health
from websocket_manager import websocket_manager

logger = logging.getLogger(__name__)

BACKOFF_DELAYS = [15, 45, 120, 300, 900, 1800]  # 15s, 45s, 2m, 5m, 15m, 30m

class WebhookSyncWorker:
    """
    Background Worker chạy ngầm để:
    1. Kiểm tra sức khỏe (Heartbeat) định kỳ của các Webhook API đối tác.
    2. Tự động đồng bộ các bản ghi bị hoãn/lỗi từ bảng webhook_sync_queue theo cơ chế FIFO và Exponential Backoff.
    3. Cập nhật trạng thái và kết quả đối soát ngược lại vào VehicleLog ngay khi đồng bộ thành công.
    """
    def __init__(self, interval_seconds: int = 5):
        self.interval_seconds = interval_seconds
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._wake_event = threading.Event()
        self._last_health_check = 0.0

    def start(self):
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._run_loop, name="WebhookSyncWorker", daemon=True)
        self._thread.start()
        logger.info("[WebhookSyncWorker] Background sync & health monitoring thread started.")

    def stop(self):
        self.running = False
        self._wake_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        logger.info("[WebhookSyncWorker] Stopped.")

    def trigger_now(self):
        """Đánh thức worker để xử lý hàng đợi ngay lập tức"""
        self._wake_event.set()

    def _run_loop(self):
        while self.running:
            try:
                # 1. Định kỳ mỗi 30 giây kiểm tra sức khỏe các ActionGroup
                now_ts = time.time()
                if now_ts - self._last_health_check >= 30:
                    self._check_all_action_groups_health()
                    self._last_health_check = now_ts

                # 2. Xử lý các bản ghi đang chờ trong hàng đợi
                self._process_pending_queue()

            except Exception as e:
                logger.error(f"[WebhookSyncWorker] Unhandled error in loop: {e}", exc_info=True)

            # Chờ interval hoặc được đánh thức bởi trigger_now()
            self._wake_event.wait(timeout=self.interval_seconds)
            self._wake_event.clear()

    def _check_all_action_groups_health(self):
        """Chạy heartbeat ping kiểm tra tất cả ActionGroup đang active"""
        db = SessionLocal()
        try:
            active_groups = db.query(ActionGroup).filter(ActionGroup.is_active == True).all()
            for ag in active_groups:
                prev_info = webhook_circuit_breaker.get_info(ag.id)
                prev_status = prev_info["status"]
                is_online, latency, msg = test_action_group_health(ag, timeout=2.5)

                # Nếu trước đó OFFLINE mà giờ khôi phục ONLINE -> lập tức đánh thức để đồng bộ queue!
                if prev_status != "ONLINE" and is_online:
                    logger.info(f"[WebhookSyncWorker] ActionGroup #{ag.id} [{ag.name}] is back ONLINE! Triggering queue flush.")
                    self.trigger_now()
        except Exception as e:
            logger.debug(f"[WebhookSyncWorker] Health check cycle note: {e}")
        finally:
            db.close()

    def _process_pending_queue(self):
        """Xử lý các bản ghi PENDING trong webhook_sync_queue"""
        db = SessionLocal()
        try:
            now_utc = datetime.utcnow()
            pending_items = (
                db.query(WebhookSyncQueue)
                .filter(
                    WebhookSyncQueue.status == "PENDING",
                    WebhookSyncQueue.next_retry_at <= now_utc
                )
                .order_by(WebhookSyncQueue.id.asc())
                .limit(50)
                .all()
            )

            if not pending_items:
                return

            # Nhóm theo log_id để duy trì tính tuần tự (Strict FIFO per Log)
            failed_log_ids_this_cycle = set()

            for item in pending_items:
                if not self.running:
                    break

                # Nếu bản ghi trước đó của cùng log_id này đã thất bại ở vòng lặp này -> tạm hoãn bản ghi tiếp theo
                if item.log_id and item.log_id in failed_log_ids_this_cycle:
                    continue

                # Kiểm tra Circuit Breaker cho ActionGroup này
                if not webhook_circuit_breaker.can_execute(item.action_group_id):
                    # Mạch vẫn đang ngắt (OPEN), không gửi để tránh tốn thời gian
                    continue

                self._dispatch_single_queue_item(db, item, failed_log_ids_this_cycle)

        finally:
            db.close()

    def _dispatch_single_queue_item(self, db, item: WebhookSyncQueue, failed_log_ids_this_cycle: set):
        ag = db.query(ActionGroup).filter(ActionGroup.id == item.action_group_id).first()
        if not ag or not ag.is_active:
            # ActionGroup đã bị xóa hoặc tắt -> hủy bản ghi này
            item.status = "CANCELLED"
            item.last_error = "ActionGroup không tồn tại hoặc đã bị tắt."
            db.commit()
            return

        cam = db.query(Camera).filter(Camera.id == item.camera_id).first() if item.camera_id else None

        try:
            payload = json.loads(item.payload_json) if item.payload_json else {}
        except Exception:
            payload = {}

        # -------------------------------------------------------------
        # Chuẩn hóa & Tự động phục hồi (Self-Healing) payload trước khi gọi API đối tác:
        # Đảm bảo 100% tuân thủ validation của Yii2 Api2Controller, tránh lỗi HTTP 422:
        # 1. Biển số xe & mã xe
        plate = str(payload.get("plate_number") or item.plate_number or "").strip().upper()
        clean_plate = "".join(c for c in plate if c.isalnum()).upper()
        payload["plate_number"] = plate
        payload["bien_so_xe"] = plate
        payload["clean_plate"] = clean_plate
        payload["ma_xe"] = clean_plate

        # 2. Thời gian phát hiện (detected_at / thoi_gian)
        time_val = payload.get("detected_at") or payload.get("thoi_gian")
        if not time_val:
            time_val = (item.created_at or datetime.utcnow()).strftime("%Y-%m-%d %H:%M:%S")
        elif isinstance(time_val, str) and len(time_val) == 10:
            time_val = f"{time_val} 00:00:00"
        payload["detected_at"] = str(time_val)
        payload["thoi_gian"] = str(time_val)

        # 3. Sự kiện & Góc chụp (su_kien / vehicle_view)
        # Đối tác Yii2 Api2Controller CHỈ chấp nhận: 'mat_truoc', 'mat_sau' (hoặc 'front', 'rear').
        # Nếu su_kien đang bị gán 'SUA_SU_KIEN', 'XOA_SU_KIEN'... hoặc không hợp lệ -> tự động chuẩn hóa về 'mat_truoc' hoặc 'mat_sau'
        raw_sk = str(payload.get("su_kien") or "").strip().lower()
        raw_view = str(payload.get("vehicle_view") or "").strip().lower()
        if raw_sk in ["mat_sau", "rear", "back", "sau"]:
            norm_sk = "mat_sau"
            norm_view = "rear"
        elif raw_sk in ["mat_truoc", "front", "truoc"]:
            norm_sk = "mat_truoc"
            norm_view = "front"
        elif raw_view in ["rear", "back", "mat_sau", "sau"]:
            norm_sk = "mat_sau"
            norm_view = "rear"
        else:
            norm_sk = "mat_truoc"
            norm_view = "front"
        payload["su_kien"] = norm_sk
        payload["vehicle_view"] = norm_view

        # 4. Mã Camera (camera_code / ma_camera)
        # Đối tác chỉ chấp nhận: cong_san_cam_bien_vao, cong_san_cam_bien_ra, cong_can_tin, cong_san_cam_bien
        cam_code = payload.get("camera_code") or payload.get("ma_camera") or getattr(cam, "camera_code", None)
        if not cam_code or str(cam_code).startswith("CAM") or str(cam_code).isdigit():
            # Tự động suy luận mã camera chuẩn từ ActionGroup hoặc Tên Camera
            ag_name = (ag.name or "").lower()
            ag_url = (ag.api_url or "").lower()
            cam_name = ((cam.camera_name if cam else "") or "").lower()
            combined = f"{ag_name} {ag_url} {cam_name}"
            if "vao" in combined or "san-cam-bien-vao" in combined:
                cam_code = "cong_san_cam_bien_vao"
            elif "ra" in combined or "san-cam-bien-ra" in combined:
                cam_code = "cong_san_cam_bien_ra"
            elif "can_tin" in combined or "can tin" in combined or "cantin" in combined:
                cam_code = "cong_can_tin"
            else:
                cam_code = "cong_san_cam_bien"
        payload["camera_code"] = cam_code
        payload["ma_camera"] = cam_code

        # 5. Đảm bảo event_action và log_id
        payload["event_action"] = item.event_action
        if item.log_id:
            payload["log_id"] = item.log_id
            payload["id_su_kien"] = item.log_id
        # -------------------------------------------------------------

        item.status = "SYNCING"
        db.commit()

        start_time = time.time()
        success, resp_data, error_str = self._send_http_request(ag, payload, item.event_action)
        latency = (time.time() - start_time) * 1000


        if success:
            # Ghi nhận thành công
            webhook_circuit_breaker.record_success(ag.id, latency_ms=latency)
            item.status = "SUCCESS"
            item.synced_at = datetime.utcnow()
            item.last_error = None

            # Cập nhật ngược lại VehicleLog nếu có
            if item.log_id:
                log_entry = db.query(VehicleLog).filter(VehicleLog.id == item.log_id).first()
                if log_entry:
                    log_entry.sync_status = "SYNCED"
                    log_entry.last_sync_at = datetime.utcnow()
                    log_entry.sync_error = None

                    # Nếu API trả về phân loại / kế hoạch đối soát mới
                    if isinstance(resp_data, dict):
                        if resp_data.get("vehicle_classification"):
                            log_entry.vehicle_classification = resp_data["vehicle_classification"]
                        if resp_data.get("action_status"):
                            log_entry.action_status = resp_data["action_status"]
                        if resp_data.get("summary"):
                            log_entry.summary = resp_data["summary"]
                        if resp_data.get("alert_level"):
                            log_entry.alert_level = resp_data["alert_level"]
                        if resp_data.get("action_result_raw"):
                            log_entry.action_result_raw = resp_data["action_result_raw"]

            db.commit()
            logger.info(f"[WebhookSyncWorker] Successfully synced Queue #{item.id} (Action: {item.event_action}, Plate: {item.plate_number}).")

            # Phát WebSocket thông báo bản ghi đã đồng bộ
            try:
                websocket_manager.broadcast_sync({
                    "event": "vehicle_log_synced",
                    "data": {
                        "queue_id": item.id,
                        "log_id": item.log_id,
                        "plate_number": item.plate_number,
                        "event_action": item.event_action,
                        "sync_status": "SYNCED",
                        "synced_at": item.synced_at.strftime("%Y-%m-%d %H:%M:%S")
                    }
                })
            except Exception:
                pass

        else:
            # Ghi nhận thất bại
            webhook_circuit_breaker.record_failure(ag.id, error_str)
            if item.log_id:
                failed_log_ids_this_cycle.add(item.log_id)

            item.retry_count += 1
            item.last_error = error_str[:500]

            if item.retry_count >= item.max_retries:
                item.status = "FAILED"
                if item.log_id:
                    log_entry = db.query(VehicleLog).filter(VehicleLog.id == item.log_id).first()
                    if log_entry:
                        log_entry.sync_status = "SYNC_FAILED"
                        log_entry.sync_error = error_str[:500]
                logger.warning(f"[WebhookSyncWorker] Queue #{item.id} exceeded max retries ({item.max_retries}). Marked as FAILED.")
            else:
                idx = min(item.retry_count - 1, len(BACKOFF_DELAYS) - 1)
                delay_sec = BACKOFF_DELAYS[idx]
                item.next_retry_at = datetime.utcnow() + timedelta(seconds=delay_sec)
                item.status = "PENDING"
                logger.info(f"[WebhookSyncWorker] Queue #{item.id} failed (attempt {item.retry_count}/{item.max_retries}). Next retry in {delay_sec}s.")

            db.commit()

            # Phát thông báo WebSocket
            try:
                websocket_manager.broadcast_sync({
                    "event": "webhook_queue_updated",
                    "data": {
                        "queue_id": item.id,
                        "log_id": item.log_id,
                        "plate_number": item.plate_number,
                        "status": item.status,
                        "retry_count": item.retry_count,
                        "last_error": item.last_error
                    }
                })
            except Exception:
                pass

    def _send_http_request(self, action_group: ActionGroup, payload: dict, event_action: str) -> Tuple[bool, Any, str]:
        """Thực hiện gọi HTTP Webhook và phân tích phản hồi"""
        api_url = action_group.api_url.strip()
        http_method = (action_group.http_method or "POST").upper()
        clean_plate = "".join(c for c in str(payload.get("plate_number", "")) if c.isalnum()).upper()

        headers = {
            "User-Agent": "Mozilla/5.0 (ANPR-SyncWorker/2.0)",
            "Content-Type": "application/json"
        }

        auth_token = getattr(action_group, "auth_token", None)
        auth_type = (getattr(action_group, "auth_type", "BEARER") or "BEARER").upper()
        if auth_token and auth_token.strip():
            tok = auth_token.strip()
            if auth_type == "BEARER":
                headers["Authorization"] = f"Bearer {tok}"
                headers["X-API-Key"] = tok
            elif auth_type == "API_KEY":
                headers["X-API-Key"] = tok
                headers["Authorization"] = f"Bearer {tok}"
            payload["token"] = tok
            payload["api_token"] = tok

        if action_group.headers_json:
            try:
                custom_headers = json.loads(action_group.headers_json)
                if isinstance(custom_headers, dict):
                    headers.update(custom_headers)
            except Exception:
                pass

        try:
            if http_method == "GET":
                if "{plate}" in api_url:
                    target_url = api_url.replace("{plate}", urllib.parse.quote(clean_plate))
                elif "{plate_number}" in api_url:
                    target_url = api_url.replace("{plate_number}", urllib.parse.quote(clean_plate))
                else:
                    sep = "&" if "?" in api_url else "?"
                    target_url = f"{api_url}{sep}plate={urllib.parse.quote(clean_plate)}"

                if auth_token and auth_token.strip():
                    sep = "&" if "?" in target_url else "?"
                    target_url = f"{target_url}{sep}token={urllib.parse.quote(auth_token.strip())}"

                if event_action in ["UPDATE", "DELETE", "RESTORE"]:
                    sep = "&" if "?" in target_url else "?"
                    target_url = f"{target_url}{sep}event_action={event_action}"
                    if payload.get("log_id"):
                        target_url += f"&log_id={payload['log_id']}"

                req = urllib.request.Request(target_url, headers=headers, method="GET")
            else:
                target_url = api_url
                post_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                req = urllib.request.Request(target_url, data=post_data, headers=headers, method="POST")

            with urllib.request.urlopen(req, timeout=8) as response:
                resp_body = response.read().decode("utf-8").strip()

                parsed_dict = {
                    "action_result_raw": resp_body
                }

                # 1. Parse boolean thuần
                if resp_body.lower() == "true":
                    parsed_dict.update({
                        "action_status": "UNPLANNED",
                        "vehicle_classification": "INTERNAL_UNPLANNED",
                        "summary": "Xe đi không lập kế hoạch",
                        "alert_level": "warning"
                    })
                    return True, parsed_dict, ""
                elif resp_body.lower() == "false":
                    parsed_dict.update({
                        "action_status": "APPROVED",
                        "vehicle_classification": "INTERNAL_VALID",
                        "summary": "Xe có kế hoạch hợp lệ",
                        "alert_level": "normal"
                    })
                    return True, parsed_dict, ""

                # 2. Parse JSON
                try:
                    parsed = json.loads(resp_body)
                    if isinstance(parsed, dict):
                        data_obj = parsed.get("data")
                        classification = parsed.get("vehicle_classification") or (data_obj.get("vehicle_classification") if isinstance(data_obj, dict) else None)
                        st = parsed.get("action_status") or parsed.get("status") or (data_obj.get("action_status") if isinstance(data_obj, dict) else None)
                        sm = parsed.get("summary") or parsed.get("message") or (data_obj.get("summary") if isinstance(data_obj, dict) else None)
                        al = parsed.get("alert_level")

                        parsed_dict.update({
                            "action_status": str(st).upper() if st else None,
                            "vehicle_classification": str(classification).upper() if classification else None,
                            "summary": str(sm) if sm else None,
                            "alert_level": str(al).lower() if al else "normal"
                        })
                        return True, parsed_dict, ""
                except Exception:
                    pass

                parsed_dict["summary"] = resp_body[:200]
                return True, parsed_dict, ""

        except urllib.error.HTTPError as he:
            err_msg = f"HTTP Error {he.code}: {he.reason}"
            try:
                raw_err = he.read().decode("utf-8", errors="ignore").strip()
                if raw_err:
                    try:
                        err_json = json.loads(raw_err)
                        if isinstance(err_json, dict):
                            msgs = []
                            if err_json.get("message"):
                                msgs.append(str(err_json["message"]))
                            if err_json.get("errors"):
                                errs = err_json["errors"]
                                if isinstance(errs, dict):
                                    for k, v in errs.items():
                                        msgs.append(f"{k}: {v}")
                                elif isinstance(errs, list):
                                    msgs.extend([str(x) for x in errs])
                            if msgs:
                                err_msg = f"HTTP Error {he.code}: {' | '.join(msgs)}"
                            else:
                                err_msg = f"HTTP Error {he.code}: {raw_err[:300]}"
                    except Exception:
                        err_msg = f"HTTP Error {he.code}: {raw_err[:300]}"
            except Exception:
                pass
            return False, None, err_msg
        except Exception as e:
            return False, None, str(e)

# Singleton
webhook_sync_worker = WebhookSyncWorker(interval_seconds=5)
