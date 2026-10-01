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

def execute_action_webhook(
    action_group: ActionGroup,
    camera: Camera,
    plate_number: str,
    vehicle_view: str,
    confidence_score: float,
    detected_at: datetime,
    image_full_rel: str,
    image_plate_rel: str
) -> Dict[str, Any]:
    """
    Gọi Webhook API của Nhóm Hành Động và chuẩn hóa kết quả trả về
    """
    clean_plate = "".join(c for c in str(plate_number) if c.isalnum()).upper()
    api_url = action_group.api_url.strip()
    http_method = (action_group.http_method or "POST").upper()

    # Payload chuẩn
    standard_payload = {
        "event_id": f"EVT_{detected_at.strftime('%Y%m%d_%H%M%S')}_{clean_plate}",
        "camera_id": camera.id,
        "camera_name": camera.camera_name,
        "zone_code": camera.zone_code or "TRUONGLAI",
        "zone_name": camera.zone_name or "Trường Lái",
        "action_group_code": action_group.code,
        "action_group_name": action_group.name,
        "plate_number": plate_number,
        "vehicle_view": vehicle_view,
        "confidence_score": confidence_score,
        "detected_at": detected_at.strftime("%Y-%m-%d %H:%M:%S"),
        "image_full_url": f"/static/captures/{image_full_rel}",
        "image_plate_url": f"/static/captures/{image_plate_rel}"
    }

    headers = {
        "User-Agent": "Mozilla/5.0 (ANPR-NguyenTrinh/2.0)",
        "Content-Type": "application/json"
    }
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
            
            req = urllib.request.Request(target_url, headers=headers, method="GET")
        else:
            target_url = api_url
            post_data = json.dumps(standard_payload).encode("utf-8")
            req = urllib.request.Request(target_url, data=post_data, headers=headers, method="POST")

        with urllib.request.urlopen(req, timeout=6) as response:
            resp_body = response.read().decode("utf-8").strip()
            
            # 1. Parse nếu trả về boolean thuần ("true" / "false")
            if resp_body.lower() == "true":
                return {
                    "action_status": "UNPLANNED",
                    "summary": "Xe đi không lập kế hoạch",
                    "message": f"Phương tiện {plate_number} đi ngoài kế hoạch quy định",
                    "alert_level": "warning",
                    "voice_message": f"Cảnh báo xe {plate_number} đi không kế hoạch",
                    "action_result_raw": resp_body
                }
            elif resp_body.lower() == "false":
                return {
                    "action_status": "APPROVED",
                    "summary": "Xe có kế hoạch hợp lệ",
                    "message": f"Phương tiện {plate_number} có kế hoạch hợp lệ",
                    "alert_level": "normal",
                    "voice_message": f"Xe {plate_number} hợp lệ",
                    "action_result_raw": resp_body
                }

            # 2. Parse nếu trả về JSON chuẩn
            try:
                parsed = json.loads(resp_body)
                if isinstance(parsed, dict):
                    action_status = parsed.get("action_status") or parsed.get("status") or ("APPROVED" if parsed.get("success") else "WARNING")
                    summary = parsed.get("summary") or parsed.get("message")
                    message = parsed.get("message") or summary or f"Kết quả từ {action_group.name}"
                    alert_level = parsed.get("alert_level") or ("danger" if action_status in ["REJECTED", "ERROR"] else ("warning" if action_status in ["UNPLANNED", "WARNING"] else "normal"))
                    voice_msg = parsed.get("voice_message") or message
                    return {
                        "action_status": str(action_status).upper(),
                        "summary": str(summary) if summary else None,
                        "message": str(message),
                        "alert_level": str(alert_level).lower(),
                        "voice_message": str(voice_msg),
                        "action_result_raw": resp_body
                    }
                elif isinstance(parsed, bool):
                    if parsed is True:
                        return {
                            "action_status": "UNPLANNED",
                            "summary": "Xe đi không lập kế hoạch",
                            "message": f"Phương tiện {plate_number} đi ngoài kế hoạch",
                            "alert_level": "warning",
                            "voice_message": f"Cảnh báo xe {plate_number} đi không kế hoạch",
                            "action_result_raw": resp_body
                        }
                    else:
                        return {
                            "action_status": "APPROVED",
                            "summary": "Xe có kế hoạch hợp lệ",
                            "message": f"Phương tiện {plate_number} có kế hoạch hợp lệ",
                            "alert_level": "normal",
                            "voice_message": f"Xe {plate_number} hợp lệ",
                            "action_result_raw": resp_body
                        }
            except Exception:
                pass

            return {
                "action_status": "INFO",
                "summary": resp_body[:200],
                "message": resp_body,
                "alert_level": "normal",
                "voice_message": f"Nhận diện xe {plate_number}",
                "action_result_raw": resp_body
            }

    except Exception as e:
        logger.warning(f"Error calling Action Webhook [{action_group.name}] for plate {plate_number}: {e}")
        return {
            "action_status": "ERROR",
            "summary": "Lỗi kết nối Webhook",
            "message": f"Không thể kết nối API {action_group.name}: {e}",
            "alert_level": "warning",
            "voice_message": f"Lỗi kiểm tra xe {plate_number}",
            "action_result_raw": json.dumps({"error": str(e)})
        }

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
        event["confidence_score"] = res.get("confidence_score", 0.9)

        logger.info(f"Vision API [{ocr_engine_type}] result: Plate={event['plate_number']}, View={event['vehicle_view']}")
        self._save_event(event)

    def _save_event(self, event: dict):
        camera_id = event["camera_id"]
        plate_number = event.get("plate_number", "UNKNOWN")
        vehicle_view = event.get("vehicle_view", "front")
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
            action_group = db.query(ActionGroup).filter(ActionGroup.id == cam.action_group_id, ActionGroup.is_active == True).first() if cam.action_group_id else None

            action_status = "INFO"
            action_group_id = None
            action_group_name = None
            action_result_raw = None
            alert_level = "normal"
            summary = None
            voice_message = None

            # Chỉ gọi Webhook nếu là xe mặt trước (front) hoặc hợp lệ VÀ độ tin cậy >= 0.40
            if is_valid_license_plate(plate_number) and confidence_score >= 0.40:
                if action_group:
                    action_group_id = action_group.id
                    action_group_name = action_group.name
                    logger.info(f"Triggering Action Group [{action_group.name}] Webhook for Plate [{plate_number}] on {cam.camera_name}...")
                    webhook_res = execute_action_webhook(
                        action_group=action_group,
                        camera=cam,
                        plate_number=plate_number,
                        vehicle_view=vehicle_view,
                        confidence_score=confidence_score,
                        detected_at=now,
                        image_full_rel=rel_full_path,
                        image_plate_rel=rel_plate_path
                    )
                    action_status = webhook_res.get("action_status", "INFO")
                    summary = webhook_res.get("summary")
                    alert_level = webhook_res.get("alert_level", "normal")
                    voice_message = webhook_res.get("voice_message")
                    action_result_raw = webhook_res.get("action_result_raw")
                else:
                    # Mặc định: Nếu thuộc vùng Trường Lái, kiểm tra kế hoạch bằng API mặc định
                    if zone_code == "TRUONGLAI" and str(vehicle_view).lower() == "front":
                        # Tra cứu Action Group mặc định kiểm tra đi không kế hoạch
                        default_ag = db.query(ActionGroup).filter(ActionGroup.code == "KIEM_TRA_DI_KHONG_KE_HOACH").first()
                        if default_ag:
                            webhook_res = execute_action_webhook(
                                action_group=default_ag,
                                camera=cam,
                                plate_number=plate_number,
                                vehicle_view=vehicle_view,
                                confidence_score=confidence_score,
                                detected_at=now,
                                image_full_rel=rel_full_path,
                                image_plate_rel=rel_plate_path
                            )
                            action_status = webhook_res.get("action_status", "INFO")
                            summary = webhook_res.get("summary")
                            alert_level = webhook_res.get("alert_level", "normal")
                            voice_message = webhook_res.get("voice_message")
                            action_result_raw = webhook_res.get("action_result_raw")
                            action_group_id = default_ag.id
                            action_group_name = default_ag.name

            # Ghi vào cơ sở dữ liệu vehicle_logs
            log_entry = VehicleLog(
                camera_id=camera_id,
                plate_number=plate_number,
                vehicle_view=vehicle_view,
                confidence_score=confidence_score,
                image_full_path=rel_full_path,
                image_plate_path=rel_plate_path,
                zone_code=zone_code,
                action_group_id=action_group_id,
                action_group_name=action_group_name,
                action_status=action_status,
                action_result_raw=action_result_raw,
                alert_level=alert_level,
                summary=summary,
                detected_at=now
            )
            db.add(log_entry)
            db.commit()
            db.refresh(log_entry)
            logger.info(f"Saved detection event ID {log_entry.id} ({plate_number}) - Status: {action_status}, Zone: {zone_code}")

            # Định tuyến thông báo đa kênh (Web Realtime + Firebase Push)
            event_dispatch_payload = {
                "id": log_entry.id,
                "camera_id": camera_id,
                "camera_name": cam.camera_name,
                "plate_number": plate_number,
                "vehicle_view": vehicle_view,
                "confidence_score": confidence_score,
                "zone_code": zone_code,
                "zone_name": zone_name,
                "action_group_id": action_group_id,
                "action_group_name": action_group_name,
                "action_status": action_status,
                "alert_level": alert_level,
                "summary": summary,
                "voice_message": voice_message,
                "image_full_path": f"/static/captures/{rel_full_path}",
                "image_plate_path": f"/static/captures/{rel_plate_path}",
                "detected_at": now.strftime("%Y-%m-%d %H:%M:%S")
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
