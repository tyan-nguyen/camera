import os
import time
import json
import queue
import threading
import logging
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime
from PIL import Image
import cv2
from typing import Optional, Dict, Any, Tuple
from concurrent.futures import ThreadPoolExecutor

from config import settings
from database import SessionLocal
from models import VehicleLog, Camera, ActionGroup
from vision_api import call_openai_vision_api, call_gemini_vision_api, call_lmstudio_vision_api
from notification_service import notification_service
from webhook_service import webhook_circuit_breaker, enqueue_webhook_event

logger = logging.getLogger(__name__)

def is_valid_license_plate(plate: str) -> bool:
    """Kiểm tra biển số xe có phải là chuỗi biển số hợp lệ (không phải UNKNOWN/lỗi rác)"""
    if not plate:
        return False
    import re
    clean = re.sub(r'[^A-Za-z0-9]', '', str(plate)).upper()
    if not clean or len(clean) < 4:
        return False
    if any(err in clean for err in ["UNKNOWN", "UNKN0WN", "APIERROR", "NOAPIKEY", "IMGERR", "PENDING", "NULL", "NONE"]):
        return False
    return True


def resolve_camera_traffic_flow(camera: Camera, vehicle_view: str):
    """
    Xác định Hướng Nghiệp Vụ, ActionGroup và có bỏ qua webhook hay không
    dựa vào traffic_direction của Camera và góc chụp AI.
    
    Hỗ trợ 5 chế độ:
    1. TWO_WAY: Cổng 1 Camera (2 Chiều Tự Động)
       - front -> Xe RA (Mặt trước / OUT / target_ag = front)
       - rear  -> Xe VÀO (Mặt sau / IN / target_ag = rear)
    2. OUT_FRONT_ONLY: Cổng 1 làn hoặc cổng ra - Chiều RA Chỉ Đọc Mặt Trước
       - front -> Xe RA (Mặt trước / OUT / target_ag = front)
       - rear  -> BỎ QUA GỌI WEBHOOK (đuôi xe của xe đang vào bãi)
    3. IN_FRONT_ONLY: Cổng 1 làn hoặc cổng vào - Chiều VÀO Chỉ Đọc Mặt Trước
       - front -> Xe VÀO (Mặt sau / IN / target_ag = rear)
       - rear  -> BỎ QUA GỌI WEBHOOK (đuôi xe của xe đang ra ngoài)
    4. OUT_ALL: Cổng 2 làn riêng - Làn RA riêng biệt
       - Mọi góc chụp -> Xe RA (Mặt trước / OUT / target_ag = front)
    5. IN_ALL: Cổng 2 làn riêng - Làn VÀO riêng biệt
       - Mọi góc chụp -> Xe VÀO (Mặt sau / IN / target_ag = rear)
    
    Returns:
        tuple: (target_ag_id, effective_su_kien, effective_direction, should_skip_webhook, skip_reason)
    """
    cam_direction = getattr(camera, "traffic_direction", "TWO_WAY") or "TWO_WAY"
    view_lower = str(vehicle_view or "unknown").strip().lower()

    if cam_direction == "OUT_FRONT_ONLY":
        if view_lower == "rear":
            return (None, "mat_truoc", "OUT", True, f"Camera #{camera.id} '{camera.camera_name}' (OUT_FRONT_ONLY) bỏ qua biển số đuôi xe (rear) để tránh bắt nhầm xe đang vào bãi.")
        target_ag_id = camera.action_group_front_id or camera.action_group_id
        return (target_ag_id, "mat_truoc", "OUT", False, None)

    elif cam_direction == "IN_FRONT_ONLY":
        if view_lower == "rear":
            return (None, "mat_sau", "IN", True, f"Camera #{camera.id} '{camera.camera_name}' (IN_FRONT_ONLY) bỏ qua biển số đuôi xe (rear) để tránh bắt nhầm xe đang ra ngoài.")
        target_ag_id = camera.action_group_rear_id or camera.action_group_id
        return (target_ag_id, "mat_sau", "IN", False, None)

    elif cam_direction == "OUT_ALL":
        target_ag_id = camera.action_group_front_id or camera.action_group_id
        return (target_ag_id, "mat_truoc", "OUT", False, None)

    elif cam_direction == "IN_ALL":
        target_ag_id = camera.action_group_rear_id or camera.action_group_id
        return (target_ag_id, "mat_sau", "IN", False, None)

    else: # TWO_WAY (mặc định Cổng 1 Camera 2 chiều)
        if view_lower == "rear":
            target_ag_id = camera.action_group_rear_id or camera.action_group_id
            return (target_ag_id, "mat_sau", "IN", False, None)
        else:
            target_ag_id = camera.action_group_front_id or camera.action_group_id
            return (target_ag_id, "mat_truoc", "OUT", False, None)

def execute_action_webhook(
    action_group: ActionGroup,
    camera: Camera,
    plate_number: str,
    vehicle_view: str,
    confidence_score: float,
    detected_at: datetime,
    image_full_rel: str,
    image_plate_rel: str,
    vehicle_type: str = "car",
    event_action: str = "CREATE",
    extra_payload: Optional[Dict[str, Any]] = None,
    db = None,
    log_id: Optional[int] = None
) -> Dict[str, Any]:
    """
    Gọi Webhook API của Nhóm Hành Động và chuẩn hóa kết quả trả về.
    Tích hợp Circuit Breaker (Fail-Fast) và hàng đợi đồng bộ Transactional Outbox.
    """
    clean_plate = "".join(c for c in str(plate_number) if c.isalnum()).upper()
    api_url = action_group.api_url.strip()
    http_method = (action_group.http_method or "POST").upper()

    # Lấy mã camera trực tiếp từ trường camera_code (không suy diễn từ tên camera)
    camera_code = (getattr(camera, "camera_code", None) or "").strip()
    if not camera_code:
        # Fallback an toàn nếu chưa cấu hình mã: gửi ID camera dạng chuỗi
        camera_code = str(camera.id)
    ma_camera = camera_code
    
    # Xác định Hướng Nghiệp Vụ từ resolve_camera_traffic_flow
    _, default_su_kien, default_dir, _, _ = resolve_camera_traffic_flow(camera, vehicle_view)
    su_kien = default_su_kien
    time_str = detected_at.strftime("%Y-%m-%d %H:%M:%S")

    # Payload chuẩn theo Standard Webhook Specification v3.0
    standard_payload = {
        "event_action": event_action,
        "event_id": f"EVT_{detected_at.strftime('%Y%m%d_%H%M%S')}_{clean_plate}",
        "camera_id": camera.id,
        "camera_name": camera.camera_name,
        "camera_code": camera_code,
        "camera_direction": getattr(camera, "traffic_direction", "TWO_WAY") or "TWO_WAY",
        "zone_code": camera.zone_code or "TRUONGLAI",
        "zone_name": camera.zone_name or "Trường Lái",
        "action_group_code": action_group.code,
        "action_group_name": action_group.name,
        "plate_number": plate_number,
        "clean_plate": clean_plate,
        "vehicle_view": vehicle_view,
        "vehicle_type": vehicle_type,
        "confidence_score": confidence_score,
        "detected_at": time_str,
        "direction": default_dir,
        "image_full_url": f"/static/captures/{image_full_rel}",
        "image_plate_url": f"/static/captures/{image_plate_rel}",
        # Các trường tương thích với Yii2 Api2Controller actionCameraLogs:
        "ma_camera": ma_camera,
        "bien_so_xe": plate_number,
        "su_kien": su_kien,
        "thoi_gian": time_str
    }
    if extra_payload:
        standard_payload.update(extra_payload)

    # 1. Kiểm tra Circuit Breaker trước khi gửi: nếu đang OPEN, fail-fast ngay
    can_execute = webhook_circuit_breaker.can_execute(action_group.id)
    if not can_execute:
        logger.warning(f"[CircuitBreaker] ActionGroup #{action_group.id} [{action_group.name}] is OPEN/OFFLINE. Fail-fast enqueuing to webhook_sync_queue.")
        if db and log_id:
            try:
                enqueue_webhook_event(
                    db=db,
                    action_group_id=action_group.id,
                    log_id=log_id,
                    camera_id=camera.id if camera else None,
                    event_action=event_action,
                    event_id=standard_payload["event_id"],
                    plate_number=plate_number,
                    payload_dict=standard_payload,
                    initial_error="Circuit Breaker OPEN - API đối tác đang ngoại tuyến"
                )
            except Exception as eq_err:
                logger.error(f"Error enqueuing webhook event: {eq_err}")

        return {
            "action_status": "INFO",
            "vehicle_classification": "UNKNOWN",
            "summary": "Chờ đồng bộ Webhook (API offline)",
            "message": f"API {action_group.name} đang ngoại tuyến. Bản tin đã đưa vào hàng đợi tự động đồng bộ.",
            "alert_level": "normal",
            "voice_message": f"Nhận diện xe {plate_number}",
            "barrier_action": "NONE",
            "webhook_result_bool": None,
            "action_result_raw": json.dumps({"status": "PENDING_SYNC", "reason": "Circuit Breaker OPEN"}),
            "sync_status": "PENDING_SYNC",
            "standard_payload": standard_payload
        }

    headers = {
        "User-Agent": "Mozilla/5.0 (ANPR-NguyenTrinh/2.0)",
        "Content-Type": "application/json"
    }

    # Bổ sung Token bảo mật từ cấu hình ActionGroup
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
        # Đưa kèm vào payload để tương thích nếu API đọc từ JSON Body
        standard_payload["token"] = tok
        standard_payload["api_token"] = tok

    if action_group.headers_json:
        try:
            custom_headers = json.loads(action_group.headers_json)
            if isinstance(custom_headers, dict):
                headers.update(custom_headers)
        except Exception:
            pass

    try:
        if http_method == "GET":
            # Hỗ trợ template URL: ?plate={plate} hoặc nối tham số
            if "{plate}" in api_url:
                target_url = api_url.replace("{plate}", urllib.parse.quote(clean_plate))
            elif "{plate_number}" in api_url:
                target_url = api_url.replace("{plate_number}", urllib.parse.quote(clean_plate))
            else:
                sep = "&" if "?" in api_url else "?"
                target_url = f"{api_url}{sep}plate={urllib.parse.quote(clean_plate)}"
            
            if "{camera_code}" in target_url:
                target_url = target_url.replace("{camera_code}", urllib.parse.quote(camera_code))
            if "{ma_camera}" in target_url:
                target_url = target_url.replace("{ma_camera}", urllib.parse.quote(ma_camera))

            # Gửi token qua query nếu GET
            if auth_token and auth_token.strip():
                sep = "&" if "?" in target_url else "?"
                target_url = f"{target_url}{sep}token={urllib.parse.quote(auth_token.strip())}"
            
            if event_action in ["UPDATE", "DELETE", "RESTORE"]:
                sep = "&" if "?" in target_url else "?"
                target_url = f"{target_url}{sep}event_action={event_action}"
                if extra_payload and extra_payload.get("old_plate_number"):
                    target_url += f"&old_plate={urllib.parse.quote(str(extra_payload['old_plate_number']))}"
                if extra_payload and extra_payload.get("log_id"):
                    target_url += f"&log_id={extra_payload['log_id']}"
            
            req = urllib.request.Request(target_url, headers=headers, method="GET")
        else:
            target_url = api_url
            post_data = json.dumps(standard_payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(target_url, data=post_data, headers=headers, method="POST")

        start_req_time = time.time()
        with urllib.request.urlopen(req, timeout=5) as response:
            latency_ms = (time.time() - start_req_time) * 1000
            webhook_circuit_breaker.record_success(action_group.id, latency_ms=latency_ms)
            resp_body = response.read().decode("utf-8").strip()
            
            # 1. Parse nếu trả về boolean thuần ("true" / "false")
            if resp_body.lower() == "true":
                return {
                    "action_status": "UNPLANNED",
                    "vehicle_classification": "INTERNAL_UNPLANNED",
                    "summary": "Xe đi không lập kế hoạch",
                    "message": f"Phương tiện {plate_number} đi ngoài kế hoạch quy định",
                    "alert_level": "warning",
                    "voice_message": f"Cảnh báo xe {plate_number} đi không kế hoạch",
                    "barrier_action": "NONE",
                    "webhook_result_bool": True,
                    "action_result_raw": resp_body,
                    "sync_status": "SYNCED",
                    "standard_payload": standard_payload
                }
            elif resp_body.lower() == "false":
                return {
                    "action_status": "APPROVED",
                    "vehicle_classification": "INTERNAL_VALID",
                    "summary": "Xe có kế hoạch hợp lệ",
                    "message": f"Phương tiện {plate_number} có kế hoạch hợp lệ",
                    "alert_level": "normal",
                    "voice_message": f"Xe {plate_number} hợp lệ",
                    "barrier_action": "NONE",
                    "webhook_result_bool": False,
                    "action_result_raw": resp_body,
                    "sync_status": "SYNCED",
                    "standard_payload": standard_payload
                }

            # 2. Parse nếu trả về JSON chuẩn
            try:
                parsed = json.loads(resp_body)
                if isinstance(parsed, dict):
                    vehicle_classification = parsed.get("vehicle_classification")
                    action_status = parsed.get("action_status") or parsed.get("status")
                    summary = parsed.get("summary") or parsed.get("message")
                    message = parsed.get("message") or summary or f"Kết quả từ {action_group.name}"
                    alert_level = parsed.get("alert_level")
                    voice_msg = parsed.get("voice_message") or message
                    barrier_action = parsed.get("barrier_action") or "NONE"
                    res_bool = parsed.get("webhook_result_bool")

                    # Fallback nếu kết quả nằm trong data: { ... }
                    data_obj = parsed.get("data")
                    if isinstance(data_obj, dict):
                        if not vehicle_classification:
                            vehicle_classification = data_obj.get("vehicle_classification")
                        if not action_status:
                            action_status = data_obj.get("action_status")
                        if not summary:
                            summary = data_obj.get("summary")

                    # Tự động chuẩn hóa vehicle_classification nếu API chưa trả về
                    if not vehicle_classification:
                        st_up = str(action_status).upper()
                        if st_up in ["APPROVED", "VALID"]:
                            vehicle_classification = "INTERNAL_VALID"
                        elif st_up in ["UNPLANNED", "DANGER"]:
                            vehicle_classification = "INTERNAL_UNPLANNED"
                        elif st_up in ["INFO", "WARNING", "GUEST", "UNKNOWN"]:
                            vehicle_classification = "EXTERNAL_UNKNOWN"
                        else:
                            vehicle_classification = "UNKNOWN"

                    if not action_status:
                        if vehicle_classification == "INTERNAL_VALID":
                            action_status = "APPROVED"
                        elif vehicle_classification == "INTERNAL_UNPLANNED":
                            action_status = "UNPLANNED"
                        else:
                            action_status = "INFO"

                    if not alert_level:
                        st_up = str(action_status).upper()
                        if st_up in ["REJECTED", "ERROR", "DANGER"] or vehicle_classification == "INTERNAL_UNPLANNED":
                            alert_level = "warning"
                        elif st_up in ["UNPLANNED", "WARNING"]:
                            alert_level = "warning"
                        else:
                            alert_level = "normal"

                    if res_bool is None:
                        if "success" in parsed and isinstance(parsed["success"], bool):
                            res_bool = parsed["success"]
                        elif "result" in parsed and isinstance(parsed["result"], bool):
                            res_bool = parsed["result"]
                        elif "valid" in parsed and isinstance(parsed["valid"], bool):
                            res_bool = parsed["valid"]
                        elif vehicle_classification == "INTERNAL_UNPLANNED":
                            res_bool = True
                        elif vehicle_classification == "INTERNAL_VALID":
                            res_bool = False

                    return {
                        "action_status": str(action_status).upper(),
                        "vehicle_classification": str(vehicle_classification).upper(),
                        "summary": str(summary) if summary else None,
                        "message": str(message),
                        "alert_level": str(alert_level).lower(),
                        "voice_message": str(voice_msg),
                        "barrier_action": str(barrier_action).upper(),
                        "webhook_result_bool": res_bool,
                        "action_result_raw": resp_body,
                        "sync_status": "SYNCED",
                        "standard_payload": standard_payload
                    }
                elif isinstance(parsed, bool):
                    if parsed is True:
                        return {
                            "action_status": "UNPLANNED",
                            "vehicle_classification": "INTERNAL_UNPLANNED",
                            "summary": "Xe đi không lập kế hoạch",
                            "message": f"Phương tiện {plate_number} đi ngoài kế hoạch",
                            "alert_level": "warning",
                            "voice_message": f"Cảnh báo xe {plate_number} đi không kế hoạch",
                            "barrier_action": "NONE",
                            "webhook_result_bool": True,
                            "action_result_raw": resp_body,
                            "sync_status": "SYNCED",
                            "standard_payload": standard_payload
                        }
                    else:
                        return {
                            "action_status": "APPROVED",
                            "vehicle_classification": "INTERNAL_VALID",
                            "summary": "Xe có kế hoạch hợp lệ",
                            "message": f"Phương tiện {plate_number} có kế hoạch hợp lệ",
                            "alert_level": "normal",
                            "voice_message": f"Xe {plate_number} hợp lệ",
                            "barrier_action": "NONE",
                            "webhook_result_bool": False,
                            "action_result_raw": resp_body,
                            "sync_status": "SYNCED",
                            "standard_payload": standard_payload
                        }
            except Exception:
                pass

            return {
                "action_status": "INFO",
                "vehicle_classification": "EXTERNAL_UNKNOWN",
                "summary": resp_body[:200],
                "message": resp_body,
                "alert_level": "normal",
                "voice_message": f"Nhận diện xe {plate_number}",
                "barrier_action": "NONE",
                "webhook_result_bool": None,
                "action_result_raw": resp_body,
                "sync_status": "SYNCED",
                "standard_payload": standard_payload
            }

    except Exception as e:
        webhook_circuit_breaker.record_failure(action_group.id, str(e))
        logger.warning(f"Error calling Action Webhook [{action_group.name}] for plate {plate_number}: {e}")

        if db and log_id:
            try:
                enqueue_webhook_event(
                    db=db,
                    action_group_id=action_group.id,
                    log_id=log_id,
                    camera_id=camera.id if camera else None,
                    event_action=event_action,
                    event_id=standard_payload["event_id"],
                    plate_number=plate_number,
                    payload_dict=standard_payload,
                    initial_error=str(e)
                )
            except Exception as eq_err:
                logger.error(f"Error enqueuing webhook event on error: {eq_err}")

        return {
            "action_status": "INFO",
            "vehicle_classification": "UNKNOWN",
            "summary": "Lỗi kết nối - Đã lưu hàng đợi",
            "message": f"Không thể kết nối API {action_group.name}: {e}. Đã lưu vào hàng đợi đồng bộ.",
            "alert_level": "normal",
            "voice_message": f"Lỗi kiểm tra xe {plate_number}",
            "barrier_action": "NONE",
            "webhook_result_bool": None,
            "action_result_raw": json.dumps({"error": str(e), "status": "PENDING_SYNC"}),
            "sync_status": "PENDING_SYNC",
            "sync_error": str(e),
            "standard_payload": standard_payload
        }

def is_webhook_event_enabled(camera: Optional[Camera], event_action: str) -> bool:
    """
    Kiểm tra xem camera này có được cấu hình bật sự kiện webhook (CREATE, UPDATE, DELETE, RESTORE) hay không.
    Mặc định: 'CREATE,UPDATE'.
    """
    if not camera:
        return False
    enabled_str = getattr(camera, "webhook_events", None) or "CREATE,UPDATE"
    enabled_list = [e.strip().upper() for e in str(enabled_str).split(",") if e.strip()]
    return event_action.strip().upper() in enabled_list

def dispatch_vehicle_log_update_webhook(
    log_item: VehicleLog,
    old_plate: str,
    old_view: str,
    old_type: str,
    updated_by: str,
    db
) -> Optional[Dict[str, Any]]:
    """
    Kích hoạt gọi Webhook khi có chỉnh sửa sự kiện biển số xe.
    Xác định ActionGroup dựa trên hướng chụp mới (front/rear).
    Kiểm tra xem camera có bật webhook cho sự kiện UPDATE không.
    """
    cam = db.query(Camera).filter(Camera.id == log_item.camera_id).first()
    if not cam:
        return None

    if not is_webhook_event_enabled(cam, "UPDATE"):
        logger.info(f"Camera #{cam.id} '{cam.camera_name}' không bật webhook cho sự kiện UPDATE. Bỏ qua gọi API.")
        return None

    # Xác định ActionGroup và hướng luồng nghiệp vụ dựa trên camera traffic_direction
    target_ag_id, effective_su_kien, effective_dir, should_skip, skip_reason = resolve_camera_traffic_flow(
        cam, log_item.vehicle_view
    )
    if should_skip:
        logger.info(f"Log #{log_item.id} được sửa, nhưng bỏ qua webhook theo quy tắc luồng xe: {skip_reason}")
        return None

    if not target_ag_id:
        logger.info(f"Log #{log_item.id} được sửa, nhưng camera #{cam.id} không cấu hình webhook cho hướng nghiệp vụ '{effective_su_kien}'. Bỏ qua gọi webhook.")
        return None

    action_group = db.query(ActionGroup).filter(ActionGroup.id == target_ag_id, ActionGroup.is_active == True).first()
    if not action_group:
        logger.info(f"Log #{log_item.id} được sửa, ActionGroup #{target_ag_id} không tồn tại hoặc đã bị tắt.")
        return None

    clean_new_plate = "".join(c for c in str(log_item.plate_number) if c.isalnum()).upper()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    extra_payload = {
        "event_action": "UPDATE",
        "log_id": log_item.id,
        "id_su_kien": log_item.id,
        "event_id": f"EVT_{log_item.detected_at.strftime('%Y%m%d_%H%M%S')}_{clean_new_plate}_L{log_item.id}",
        "old_plate_number": old_plate,
        "bien_so_cu": old_plate,
        "old_vehicle_view": old_view,
        "old_vehicle_type": old_type,
        "updated_by": updated_by,
        "updated_at": now_str,
        "su_kien": "SUA_SU_KIEN",
        "trip_su_kien": effective_su_kien,
        "direction": effective_dir
    }

    try:
        res = execute_action_webhook(
            action_group=action_group,
            camera=cam,
            plate_number=log_item.plate_number,
            vehicle_view=log_item.vehicle_view,
            confidence_score=log_item.confidence_score or 0.95,
            detected_at=log_item.detected_at,
            image_full_rel=log_item.image_full_path,
            image_plate_rel=log_item.image_plate_path,
            vehicle_type=log_item.vehicle_type or "car",
            event_action="UPDATE",
            extra_payload=extra_payload,
            db=db,
            log_id=log_item.id
        )
        return res
    except Exception as e:
        logger.error(f"Lỗi khi gọi UPDATE webhook cho Log #{log_item.id}: {e}")
        return None

def dispatch_vehicle_log_delete_webhook(
    log_item: VehicleLog,
    deleted_by: str,
    db
) -> Optional[Dict[str, Any]]:
    """
    Kích hoạt gọi Webhook khi xóa sự kiện biển số xe (Xóa mềm).
    Xác định ActionGroup dựa trên hướng chụp xe.
    Kiểm tra xem camera có bật webhook cho sự kiện DELETE không.
    """
    cam = db.query(Camera).filter(Camera.id == log_item.camera_id).first()
    if not cam:
        return None

    if not is_webhook_event_enabled(cam, "DELETE"):
        logger.info(f"Camera #{cam.id} '{cam.camera_name}' không bật webhook cho sự kiện DELETE. Bỏ qua gọi API.")
        return None

    # Xác định ActionGroup và hướng luồng nghiệp vụ dựa trên camera traffic_direction
    target_ag_id, effective_su_kien, effective_dir, should_skip, skip_reason = resolve_camera_traffic_flow(
        cam, log_item.vehicle_view
    )
    if should_skip:
        logger.info(f"Log #{log_item.id} bị xóa, nhưng bỏ qua webhook theo quy tắc luồng xe: {skip_reason}")
        return None

    if not target_ag_id:
        return None

    action_group = db.query(ActionGroup).filter(ActionGroup.id == target_ag_id, ActionGroup.is_active == True).first()
    if not action_group:
        return None

    clean_plate = "".join(c for c in str(log_item.plate_number) if c.isalnum()).upper()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    extra_payload = {
        "event_action": "DELETE",
        "log_id": log_item.id,
        "id_su_kien": log_item.id,
        "event_id": f"EVT_{log_item.detected_at.strftime('%Y%m%d_%H%M%S')}_{clean_plate}_L{log_item.id}",
        "deleted_by": deleted_by,
        "deleted_at": now_str,
        "action_reason": "SOFT_DELETE",
        "su_kien": "XOA_SU_KIEN",
        "trip_su_kien": effective_su_kien,
        "direction": effective_dir
    }

    try:
        return execute_action_webhook(
            action_group=action_group,
            camera=cam,
            plate_number=log_item.plate_number,
            vehicle_view=log_item.vehicle_view,
            confidence_score=log_item.confidence_score or 0.95,
            detected_at=log_item.detected_at,
            image_full_rel=log_item.image_full_path,
            image_plate_rel=log_item.image_plate_path,
            vehicle_type=log_item.vehicle_type or "car",
            event_action="DELETE",
            extra_payload=extra_payload,
            db=db,
            log_id=log_item.id
        )
    except Exception as e:
        logger.error(f"Lỗi khi gọi DELETE webhook cho Log #{log_item.id}: {e}")
        return None

def dispatch_vehicle_log_restore_webhook(
    log_item: VehicleLog,
    restored_by: str,
    db
) -> Optional[Dict[str, Any]]:
    """
    Kích hoạt gọi Webhook khi khôi phục sự kiện từ thùng rác.
    Xác định ActionGroup dựa trên hướng chụp xe.
    Kiểm tra xem camera có bật webhook cho sự kiện RESTORE không.
    """
    cam = db.query(Camera).filter(Camera.id == log_item.camera_id).first()
    if not cam:
        return None

    if not is_webhook_event_enabled(cam, "RESTORE"):
        logger.info(f"Camera #{cam.id} '{cam.camera_name}' không bật webhook cho sự kiện RESTORE. Bỏ qua gọi API.")
        return None

    # Xác định ActionGroup và hướng luồng nghiệp vụ dựa trên camera traffic_direction
    target_ag_id, effective_su_kien, effective_dir, should_skip, skip_reason = resolve_camera_traffic_flow(
        cam, log_item.vehicle_view
    )
    if should_skip:
        logger.info(f"Log #{log_item.id} được khôi phục, nhưng bỏ qua webhook theo quy tắc luồng xe: {skip_reason}")
        return None

    if not target_ag_id:
        return None

    action_group = db.query(ActionGroup).filter(ActionGroup.id == target_ag_id, ActionGroup.is_active == True).first()
    if not action_group:
        return None

    clean_plate = "".join(c for c in str(log_item.plate_number) if c.isalnum()).upper()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    extra_payload = {
        "event_action": "RESTORE",
        "log_id": log_item.id,
        "id_su_kien": log_item.id,
        "event_id": f"EVT_{log_item.detected_at.strftime('%Y%m%d_%H%M%S')}_{clean_plate}_L{log_item.id}",
        "restored_by": restored_by,
        "restored_at": now_str,
        "action_reason": "RESTORE_FROM_TRASH",
        "su_kien": "KHOI_PHUC_SU_KIEN",
        "trip_su_kien": effective_su_kien,
        "direction": effective_dir
    }

    try:
        return execute_action_webhook(
            action_group=action_group,
            camera=cam,
            plate_number=log_item.plate_number,
            vehicle_view=log_item.vehicle_view,
            confidence_score=log_item.confidence_score or 0.95,
            detected_at=log_item.detected_at,
            image_full_rel=log_item.image_full_path,
            image_plate_rel=log_item.image_plate_path,
            vehicle_type=log_item.vehicle_type or "car",
            event_action="RESTORE",
            extra_payload=extra_payload,
            db=db,
            log_id=log_item.id
        )
    except Exception as e:
        logger.error(f"Lỗi khi gọi RESTORE webhook cho Log #{log_item.id}: {e}")
        return None

class AsyncDiskWorker:
    def __init__(self, websocket_manager=None):
        self.work_queue = queue.Queue(maxsize=1000)
        self.running = False
        self.worker_thread = None
        self.websocket_manager = websocket_manager
        if websocket_manager:
            notification_service.set_websocket_manager(websocket_manager)
        self.api_executor = ThreadPoolExecutor(max_workers=5)

    def start(self):
        self.running = True
        self.worker_thread = threading.Thread(target=self._process_queue, daemon=True)
        self.worker_thread.start()
        logger.info("AsyncDiskWorker thread started with background API executor pool.")

    def stop(self):
        self.running = False
        if self.worker_thread:
            self.worker_thread.join(timeout=0.5)
        self.api_executor.shutdown(wait=False)
        logger.info("AsyncDiskWorker thread stopped.")

    def enqueue_event(self, event_data: dict):
        try:
            self.work_queue.put_nowait(event_data)
        except queue.Full:
            logger.error("Disk Worker queue is full! Dropping capture event.")

    def _process_queue(self):
        while self.running:
            try:
                event = self.work_queue.get(timeout=1.0)
                if event:
                    if event.get("need_api_ocr"):
                        self.api_executor.submit(self._async_vision_api_task, event)
                    else:
                        self._save_event(event)
                    self.work_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Error in AsyncDiskWorker process: {e}")

    def _async_vision_api_task(self, event: dict):
        ocr_engine_type = event.get("ocr_engine_type")
        api_settings = event.get("api_settings", {})
        vehicle_crop = event.get("vehicle_crop")
        plate_crop = event.get("plate_crop")
        full_frame = event.get("full_frame")

        if vehicle_crop is not None and vehicle_crop.size > 0:
            image_to_send = vehicle_crop
        elif plate_crop is not None and plate_crop.size > 0:
            image_to_send = plate_crop
        else:
            image_to_send = full_frame

        logger.info(f"Calling Vision API [{ocr_engine_type}] with vehicle image crop in background thread...")
        if ocr_engine_type == "lmstudio_api":
            base_url = api_settings.get("lmstudio_base_url", "http://localhost:1234/v1")
            model_name = api_settings.get("lmstudio_model_name", "default")
            api_key = api_settings.get("lmstudio_api_key", "")
            res = call_lmstudio_vision_api(image_to_send, base_url=base_url, model_name=model_name, api_key=api_key)
        elif ocr_engine_type == "openai_api":
            api_key = api_settings.get("openai_api_key", "")
            model_name = api_settings.get("openai_model_name", "gpt-4o-mini")
            res = call_openai_vision_api(image_to_send, api_key=api_key, model_name=model_name)
        elif ocr_engine_type == "gemini_api":
            api_key = api_settings.get("gemini_api_key", "")
            model_name = api_settings.get("gemini_model_name", "gemini-2.0-flash")
            res = call_gemini_vision_api(image_to_send, api_key=api_key, model_name=model_name)
        else:
            res = {"plate_number": "UNKNOWN", "vehicle_view": "unknown", "confidence_score": 0.0}

        event["plate_number"] = res.get("plate_number", "UNKNOWN")
        event["vehicle_view"] = res.get("vehicle_view", "unknown")
        event["vehicle_type"] = res.get("vehicle_type") or event.get("vehicle_type", "car")
        event["confidence_score"] = res.get("confidence_score", 0.9)

        logger.info(f"Vision API [{ocr_engine_type}] result: Plate={event['plate_number']}, View={event['vehicle_view']}, Type={event['vehicle_type']}")
        self._save_event(event)

    def _save_event(self, event: dict):
        camera_id = event["camera_id"]
        plate_number = event.get("plate_number", "UNKNOWN")
        vehicle_view = event.get("vehicle_view", "front")
        vehicle_type = event.get("vehicle_type", "car")
        confidence_score = event.get("confidence_score", 0.95)
        full_frame = event["full_frame"]
        plate_crop = event["plate_crop"]

        now = datetime.now()
        date_folder = now.strftime("%Y/%m/%d")
        cam_folder = f"cam_{camera_id}"

        target_dir = os.path.join(settings.CAPTURES_DIR, date_folder, cam_folder)
        os.makedirs(target_dir, exist_ok=True)

        timestamp_str = now.strftime("%Y%m%d_%H%M%S_%f")[:19]
        safe_plate = "".join([c for c in plate_number if c.isalnum()]) or "UNKNOWN"

        full_filename = f"{timestamp_str}_full_{safe_plate}.webp"
        plate_filename = f"{timestamp_str}_plate_{safe_plate}.webp"

        full_filepath = os.path.join(target_dir, full_filename)
        plate_filepath = os.path.join(target_dir, plate_filename)

        self._save_webp(full_frame, full_filepath, quality=settings.WEBP_QUALITY)
        self._save_webp(plate_crop, plate_filepath, quality=settings.WEBP_QUALITY)

        rel_full_path = os.path.normpath(os.path.join(date_folder, cam_folder, full_filename)).replace("\\", "/")
        rel_plate_path = os.path.normpath(os.path.join(date_folder, cam_folder, plate_filename)).replace("\\", "/")

        db = SessionLocal()
        try:
            cam = db.query(Camera).filter(Camera.id == camera_id).first()
            if not cam:
                first_cam = db.query(Camera).first()
                if first_cam:
                    cam = first_cam
                    camera_id = first_cam.id
                else:
                    cam = Camera(camera_name="Camera 1", rtsp_url="rtsp://localhost/stream", is_active=True, zone_code="TRUONGLAI", zone_name="Trường Lái")
                    db.add(cam)
                    db.commit()
                    db.refresh(cam)
                    camera_id = cam.id

            zone_code = cam.zone_code or "TRUONGLAI"
            zone_name = cam.zone_name or "Trường Lái"

            # Xác định ActionGroup và hướng luồng nghiệp vụ dựa trên camera traffic_direction
            target_ag_id, effective_su_kien, effective_dir, should_skip_webhook, skip_reason = resolve_camera_traffic_flow(
                cam, vehicle_view
            )

            if should_skip_webhook:
                action_status = "INFO"
                summary = "Bỏ qua Webhook (Quy tắc luồng làn xe)"
                logger.info(f"Skipping Webhook for {plate_number} on {cam.camera_name}: {skip_reason}")
                action_group = None
            else:
                action_group = db.query(ActionGroup).filter(ActionGroup.id == target_ag_id, ActionGroup.is_active == True).first() if target_ag_id else None

            action_status = action_status or "INFO"
            vehicle_classification = None
            barrier_action = "NONE"
            action_group_id = None
            action_group_name = None
            action_result_raw = None
            alert_level = "normal"
            summary = summary or None
            voice_message = None
            push_condition = "DEFAULT"
            webhook_result_bool = None
            sync_status = "NOT_APPLICABLE"
            sync_error = None
            standard_payload = None

            is_car = (str(vehicle_type).lower() == "car")
            is_moto = (str(vehicle_type).lower() == "motorcycle")

            # Chỉ gọi Webhook nếu có ActionGroup cấu hình cho góc chụp này, biển số hợp lệ, độ tin cậy >= 0.40 VÀ camera bật sự kiện CREATE
            if action_group and is_valid_license_plate(plate_number) and confidence_score >= 0.40 and is_webhook_event_enabled(cam, "CREATE"):
                push_condition = getattr(action_group, "push_condition", "DEFAULT") or "DEFAULT"
                apply_car = getattr(action_group, "apply_car", True)
                apply_moto = getattr(action_group, "apply_motorcycle", True)

                if is_car and not apply_car:
                    action_status = "INFO"
                    summary = "Bỏ qua Webhook (Không áp dụng Ô tô)"
                    logger.info(f"Skipping Webhook for {plate_number} ({vehicle_type}): ActionGroup [{action_group.name}] has apply_car=False")
                elif is_moto and not apply_moto:
                    action_status = "INFO"
                    summary = "Bỏ qua Webhook (Không áp dụng Xe máy)"
                    logger.info(f"Skipping Webhook for {plate_number} ({vehicle_type}): ActionGroup [{action_group.name}] has apply_motorcycle=False")
                else:
                    action_group_id = action_group.id
                    action_group_name = action_group.name
                    logger.info(f"Triggering Action Group [{action_group.name}] Webhook for Plate [{plate_number}] ({vehicle_type}, view={vehicle_view}, dir={effective_dir}) on {cam.camera_name}...")
                    webhook_res = execute_action_webhook(
                        action_group=action_group,
                        camera=cam,
                        plate_number=plate_number,
                        vehicle_view=vehicle_view,
                        vehicle_type=vehicle_type,
                        confidence_score=confidence_score,
                        detected_at=now,
                        image_full_rel=rel_full_path,
                        image_plate_rel=rel_plate_path,
                        event_action="CREATE",
                        extra_payload={
                            "su_kien": effective_su_kien,
                            "direction": effective_dir
                        }
                    )
                    action_status = webhook_res.get("action_status", "INFO")
                    vehicle_classification = webhook_res.get("vehicle_classification")
                    barrier_action = webhook_res.get("barrier_action", "NONE")
                    summary = webhook_res.get("summary")
                    alert_level = webhook_res.get("alert_level", "normal")
                    voice_message = webhook_res.get("voice_message")
                    action_result_raw = webhook_res.get("action_result_raw")
                    webhook_result_bool = webhook_res.get("webhook_result_bool")
                    sync_status = webhook_res.get("sync_status", "SYNCED")
                    sync_error = webhook_res.get("sync_error")
                    standard_payload = webhook_res.get("standard_payload")

            # Ghi vào cơ sở dữ liệu vehicle_logs
            log_entry = VehicleLog(
                camera_id=camera_id,
                plate_number=plate_number,
                vehicle_view=vehicle_view,
                vehicle_type=vehicle_type,
                confidence_score=confidence_score,
                image_full_path=rel_full_path,
                image_plate_path=rel_plate_path,
                zone_code=zone_code,
                action_group_id=action_group_id,
                action_group_name=action_group_name,
                action_status=action_status,
                vehicle_classification=vehicle_classification,
                action_result_raw=action_result_raw,
                alert_level=alert_level,
                summary=summary,
                detected_at=now,
                is_edited=False,
                sync_status=sync_status,
                sync_error=sync_error,
                last_sync_at=now if sync_status == "SYNCED" else None
            )
            db.add(log_entry)
            db.commit()
            db.refresh(log_entry)

            # Nếu webhook khi nhận diện bị hoãn/lỗi, lưu ngay vào hàng đợi outbox với ID bản ghi thực tế
            if sync_status == "PENDING_SYNC" and standard_payload and action_group:
                try:
                    enqueue_webhook_event(
                        db=db,
                        action_group_id=action_group.id,
                        log_id=log_entry.id,
                        camera_id=camera_id,
                        event_action="CREATE",
                        event_id=standard_payload.get("event_id", f"EVT_{now.strftime('%Y%m%d_%H%M%S')}_{plate_number}"),
                        plate_number=plate_number,
                        payload_dict=standard_payload,
                        initial_error=sync_error or "Lỗi kết nối khi nhận diện xe"
                    )
                except Exception as eq_err:
                    logger.error(f"Lỗi đưa sự kiện CREATE vào hàng đợi đồng bộ: {eq_err}")

            logger.info(f"Saved detection event ID {log_entry.id} ({plate_number} - {vehicle_type}) - Status: {action_status}, Classification: {vehicle_classification}, Sync: {sync_status}")

            # Định tuyến thông báo đa kênh (Web Realtime + Firebase Push)
            event_dispatch_payload = {
                "id": log_entry.id,
                "camera_id": camera_id,
                "camera_name": cam.camera_name,
                "camera_code": getattr(cam, "camera_code", None),
                "plate_number": plate_number,
                "vehicle_view": vehicle_view,
                "vehicle_type": vehicle_type,
                "confidence_score": confidence_score,
                "zone_code": zone_code,
                "zone_name": zone_name,
                "action_group_id": action_group_id,
                "action_group_name": action_group_name,
                "action_status": action_status,
                "vehicle_classification": vehicle_classification,
                "barrier_action": barrier_action,
                "alert_level": alert_level,
                "summary": summary,
                "voice_message": voice_message,
                "push_condition": push_condition,
                "webhook_result_bool": webhook_result_bool,
                "image_full_path": f"/static/captures/{rel_full_path}",
                "image_plate_path": f"/static/captures/{rel_plate_path}",
                "detected_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                "sync_status": sync_status,
                "sync_error": sync_error
            }

            notification_service.dispatch_event(event_dispatch_payload)

        except Exception as e:
            db.rollback()
            logger.error(f"Failed to INSERT vehicle_log to Database: {e}")
        finally:
            db.close()

    def _save_webp(self, cv2_img, filepath: str, quality: int = 85):
        if cv2_img is None or cv2_img.size == 0:
            return
        rgb_img = cv2.cvtColor(cv2_img, cv2.COLOR_BGR2RGB)
        pil_img = Image.fromarray(rgb_img)
        pil_img.save(filepath, format="WEBP", quality=quality)
