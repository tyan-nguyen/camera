import json
import logging
import urllib.request
import urllib.error
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

def get_fcm_server_key_from_db() -> str:
    """Lấy FCM Server Key từ Database SystemSettings"""
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

def send_fcm_push(
    tokens: List[str], 
    title: str, 
    body: str, 
    data: Optional[Dict[str, Any]] = None,
    server_key: Optional[str] = None
) -> Dict[str, Any]:
    """
    Gửi thông báo đẩy FCM tới danh sách registration_ids của thiết bị Android / iOS
    Sử dụng endpoint FCM REST API trực tiếp không cần cài đặt thêm thư viện nặng
    """
    if not tokens:
        return {"success": False, "message": "No device tokens provided."}

    fcm_key = server_key or get_fcm_server_key_from_db()
    if not fcm_key:
        logger.info("[FCM PUSH] FCM Server Key is not configured in Settings. Skipping FCM push notification.")
        return {"success": False, "message": "FCM Server Key is not configured."}

    # Lọc token hợp lệ (loại bỏ token rỗng/trùng lặp)
    unique_tokens = list(set([t.strip() for t in tokens if t and len(t.strip()) > 10]))
    if not unique_tokens:
        return {"success": False, "message": "No valid device tokens."}

    payload = {
        "registration_ids": unique_tokens,
        "priority": "high",
        "notification": {
            "title": title,
            "body": body,
            "sound": "default",
            "android_channel_id": "anpr_high_importance_channel",
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
            logger.info(f"[FCM PUSH] Sent to {len(unique_tokens)} device(s). Success: {result.get('success', 0)}, Failure: {result.get('failure', 0)}")
            return {"success": True, "result": result}
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8") if e.fp else str(e)
        logger.error(f"[FCM PUSH HTTP Error] Status {e.code}: {err_msg}")
        return {"success": False, "error": f"HTTP {e.code}: {err_msg}"}
    except Exception as e:
        logger.error(f"[FCM PUSH Error] {e}")
        return {"success": False, "error": str(e)}
