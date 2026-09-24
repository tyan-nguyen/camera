import logging
from typing import Dict, Any, List, Set
from database import SessionLocal
from models import User, Camera, DeviceToken, UserRoleEnum, user_camera_permissions
from push_service import send_fcm_push

logger = logging.getLogger(__name__)

class UnifiedNotificationService:
    """
    Bộ định tuyến thông báo đa kênh hợp nhất:
    - Tra cứu danh sách User có quyền với Camera phát sinh sự kiện (Admin + User được phân quyền).
    - Bắn Realtime WebSocket tới WebApp.
    - Bắn Push Notification qua Firebase FCM tới các thiết bị Android / iOS của User có quyền.
    """
    def __init__(self, websocket_manager=None):
        self.websocket_manager = websocket_manager

    def set_websocket_manager(self, ws_mgr):
        self.websocket_manager = ws_mgr

    def get_authorized_user_ids_for_camera(self, camera_id: int) -> Set[int]:
        """Lấy danh sách ID người dùng có quyền nhận sự kiện từ camera_id"""
        db = SessionLocal()
        authorized_ids = set()
        try:
            # 1. Admin luôn có quyền với 100% camera
            admin_users = db.query(User.id).filter(User.role == UserRoleEnum.ADMIN.value, User.is_active == True).all()
            for u in admin_users:
                authorized_ids.add(u[0])

            # 2. User / Quản lý được cấp quyền riêng cho camera này
            assigned_users = db.query(user_camera_permissions.c.user_id).filter(
                user_camera_permissions.c.camera_id == camera_id
            ).all()
            for u in assigned_users:
                authorized_ids.add(u[0])
        except Exception as e:
            logger.error(f"Error fetching authorized users for camera {camera_id}: {e}")
        finally:
            db.close()
        return authorized_ids

    def get_device_tokens_for_users(self, user_ids: Set[int]) -> List[str]:
        """Lấy danh sách FCM Token của các User được chỉ định"""
        if not user_ids:
            return []
        db = SessionLocal()
        tokens = []
        try:
            device_rows = db.query(DeviceToken.fcm_token).filter(
                DeviceToken.user_id.in_(list(user_ids)),
                DeviceToken.is_active == True
            ).all()
            tokens = [d[0] for d in device_rows if d[0]]
        except Exception as e:
            logger.error(f"Error fetching device tokens for users {user_ids}: {e}")
        finally:
            db.close()
        return tokens

    def dispatch_event(self, event_data: Dict[str, Any]):
        """
        Phát tán sự kiện ra các kênh:
        1. Gửi WebSocket tới WebApp (kèm danh sách authorized_user_ids để Web lọc theo phiên)
        2. Gửi Push Notification qua Firebase FCM tới thiết bị Android / iOS của User có quyền
        """
        camera_id = event_data.get("camera_id")
        camera_name = event_data.get("camera_name", f"Camera #{camera_id}")
        plate_number = event_data.get("plate_number", "UNKNOWN")
        zone_name = event_data.get("zone_name", "Khu vực")
        action_status = event_data.get("action_status", "INFO")
        summary = event_data.get("summary") or event_data.get("message") or "Phát hiện phương tiện ra/vào"
        alert_level = event_data.get("alert_level", "normal")

        # 1. Tra cứu quyền truy cập
        authorized_user_ids = self.get_authorized_user_ids_for_camera(camera_id) if camera_id else set()
        event_data["authorized_user_ids"] = list(authorized_user_ids)

        # 2. Bắn WebSocket Real-time
        if self.websocket_manager:
            ws_payload = {
                "event": "vehicle_detected",
                "data": event_data
            }
            self.websocket_manager.broadcast_sync(ws_payload)

        # 3. Bắn Firebase FCM Push Notification
        if authorized_user_ids:
            tokens = self.get_device_tokens_for_users(authorized_user_ids)
            if tokens:
                # Tiêu đề & Nội dung Push
                if alert_level == "danger" or action_status == "REJECTED":
                    title = f"🚨 CẢNH BÁO: {plate_number}"
                elif alert_level == "warning" or action_status == "UNPLANNED":
                    title = f"⚠️ CHÚ Ý: {plate_number}"
                else:
                    title = f"🚗 Nhận diện: {plate_number}"

                body = f"{camera_name} ({zone_name}) - {summary}"

                push_data = {
                    "event_id": str(event_data.get("id", "")),
                    "camera_id": str(camera_id),
                    "camera_name": str(camera_name),
                    "plate_number": str(plate_number),
                    "zone_code": str(event_data.get("zone_code", "")),
                    "zone_name": str(zone_name),
                    "action_status": str(action_status),
                    "alert_level": str(alert_level),
                    "summary": str(summary),
                    "image_full_path": str(event_data.get("image_full_path", "")),
                    "image_plate_path": str(event_data.get("image_plate_path", "")),
                    "detected_at": str(event_data.get("detected_at", ""))
                }

                logger.info(f"Routing FCM Push to {len(tokens)} token(s) of authorized users: {authorized_user_ids}")
                send_fcm_push(tokens, title=title, body=body, data=push_data)

notification_service = UnifiedNotificationService()
