import os
import json
import logging
import urllib.request
import urllib.error
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

_firebase_app_initialized = False

def get_fcm_server_key_from_db() -> str:
    """Lấy FCM Server Key hoặc Service Account JSON từ Database SystemSettings"""
    try:
        from database import SessionLocal
        from models import SystemSettings
        db = SessionLocal()
        settings_row = db.query(SystemSettings).first()
        key = (settings_row.fcm_server_key or "").strip() if settings_row else ""
        db.close()
        return key
    except Exception as e:
        logger.warning(f"Error fetching FCM Server Key from DB: {e}")
        return ""

def _init_firebase_admin(fcm_key: Optional[str] = None) -> bool:
    global _firebase_app_initialized
    if _firebase_app_initialized:
        return True

    try:
        import firebase_admin
        from firebase_admin import credentials

        if firebase_admin._apps:
            _firebase_app_initialized = True
            return True

        # 1. Kiểm tra các file service account phổ biến trong project
        possible_files = [
            "firebase_service_account.json",
            "service_account.json",
            os.path.join("storage", "firebase_service_account.json"),
            "serviceAccountKey.json"
        ]
        
        # Nếu fcm_key là đường dẫn file
        if fcm_key and os.path.exists(fcm_key):
            cred = credentials.Certificate(fcm_key)
            firebase_admin.initialize_app(cred)
            _firebase_app_initialized = True
            logger.info(f"Firebase Admin SDK initialized with file: {fcm_key}")
            return True

        for pf in possible_files:
            if os.path.exists(pf):
                cred = credentials.Certificate(pf)
                firebase_admin.initialize_app(cred)
                _firebase_app_initialized = True
                logger.info(f"Firebase Admin SDK initialized with file: {pf}")
                return True

        # 2. Kiểm tra nếu fcm_key chứa nội dung JSON của Service Account
        if fcm_key and fcm_key.startswith("{") and "private_key" in fcm_key:
            cert_dict = json.loads(fcm_key)
            cred = credentials.Certificate(cert_dict)
            firebase_admin.initialize_app(cred)
            _firebase_app_initialized = True
            logger.info("Firebase Admin SDK initialized with JSON credentials from DB.")
            return True

    except Exception as e:
        logger.warning(f"Could not initialize Firebase Admin SDK: {e}")

    return False

def send_fcm_push(
    tokens: List[str], 
    title: str, 
    body: str, 
    data: Optional[Dict[str, Any]] = None,
    server_key: Optional[str] = None
) -> Dict[str, Any]:
    """
    Gửi thông báo đẩy FCM tới danh sách token thiết bị Android / iOS
    Tự động ưu tiên Firebase Cloud Messaging API v1 (HTTP v1 - Service Account),
    và fallback sang Legacy FCM API nếu sử dụng Server Key cũ.
    """
    if not tokens:
        return {"success": False, "message": "No device tokens provided."}

    # Lọc token hợp lệ (loại bỏ token rỗng/trùng lặp)
    unique_tokens = list(set([t.strip() for t in tokens if t and len(t.strip()) > 10]))
    if not unique_tokens:
        return {"success": False, "message": "No valid device tokens."}

    fcm_key = server_key or get_fcm_server_key_from_db()

    # --- CÁCH 1: GỬI QUA FIREBASE ADMIN SDK (HTTP v1 - CHUẨN MỚI NHẤT CỦA GOOGLE) ---
    if _init_firebase_admin(fcm_key):
        try:
            from firebase_admin import messaging
            
            str_data = {str(k): str(v) for k, v in (data or {}).items()}
            message = messaging.MulticastMessage(
                tokens=unique_tokens,
                notification=messaging.Notification(
                    title=title,
                    body=body,
                ),
                data=str_data,
                android=messaging.AndroidConfig(
                    priority="high",
                    notification=messaging.AndroidNotification(
                        channel_id="anpr_alerts_channel",
                        sound="default",
                        click_action="FLUTTER_NOTIFICATION_CLICK"
                    )
                ),
                apns=messaging.APNSConfig(
                    payload=messaging.APNSPayload(
                        aps=messaging.Aps(sound="default")
                    )
                )
            )
            response = messaging.send_each_for_multicast(message)
            logger.info(f"[FCM HTTP v1] Sent to {len(unique_tokens)} device(s). Success: {response.success_count}, Failure: {response.failure_count}")
            return {
                "success": response.success_count > 0,
                "success_count": response.success_count,
                "failure_count": response.failure_count,
                "provider": "firebase_admin_v1"
            }
        except Exception as e:
            logger.error(f"[FCM HTTP v1 Error] {e}")

    # --- CÁCH 2: FALLBACK GỬI QUA LEGACY REST API (NẾU CÓ KEY AAAA...) ---
    if fcm_key and fcm_key.startswith("AAAA"):
        payload = {
            "registration_ids": unique_tokens,
            "priority": "high",
            "notification": {
                "title": title,
                "body": body,
                "sound": "default",
                "android_channel_id": "anpr_alerts_channel",
                "click_action": "FLUTTER_NOTIFICATION_CLICK"
            },
            "data": {str(k): str(v) for k, v in (data or {}).items()}
        }

        url = "https://fcm.googleapis.com/fcm/send"
        headers = {
            "Authorization": f"key={fcm_key}",
            "Content-Type": "application/json"
        }

        try:
            req = urllib.request.Request(
                url, 
                data=json.dumps(payload).encode("utf-8"), 
                headers=headers, 
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                resp_body = resp.read().decode("utf-8")
                result = json.loads(resp_body)
                logger.info(f"[FCM Legacy] Sent to {len(unique_tokens)} device(s). Success: {result.get('success', 0)}, Failure: {result.get('failure', 0)}")
                return {"success": True, "result": result, "provider": "legacy"}
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode("utf-8") if e.fp else str(e)
            logger.error(f"[FCM Legacy HTTP Error] Status {e.code}: {err_msg}")
            return {"success": False, "error": f"HTTP {e.code}: {err_msg}"}
        except Exception as e:
            logger.error(f"[FCM Legacy Error] {e}")
            return {"success": False, "error": str(e)}

    return {
        "success": False, 
        "message": "Chưa cấu hình Firebase Service Account JSON. Vui lòng tải file service account từ Firebase Console và đặt vào thư mục dự án."
    }
