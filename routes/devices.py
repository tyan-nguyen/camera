import logging
from datetime import datetime
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from database import get_db
from models import DeviceToken, User, UserRoleEnum
from schemas import DeviceTokenCreate, DeviceTokenResponse, DeviceTestPushRequest
from auth_utils import get_current_user, require_roles
from push_service import send_fcm_push

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/devices", tags=["Device Tokens & Push"])

@router.post("/register", response_model=DeviceTokenResponse)
def register_device_token(
    device_in: DeviceTokenCreate, 
    db: Session = Depends(get_db), 
    current_user: User = Depends(get_current_user)
):
    """Đăng ký hoặc cập nhật FCM Token của thiết bị gán với User đang đăng nhập"""
    token_str = device_in.fcm_token.strip()
    if not token_str:
        raise HTTPException(status_code=400, detail="FCM Token không được để trống.")

    device = db.query(DeviceToken).filter(DeviceToken.fcm_token == token_str).first()
    if device:
        device.user_id = current_user.id
        device.device_name = device_in.device_name or device.device_name
        device.platform = device_in.platform or device.platform
        device.is_active = True
        device.updated_at = datetime.utcnow()
    else:
        device = DeviceToken(
            user_id=current_user.id,
            fcm_token=token_str,
            device_name=device_in.device_name or "Android Device",
            platform=device_in.platform or "android",
            is_active=True
        )
        db.add(device)

    db.commit()
    db.refresh(device)
    logger.info(f"Registered FCM Device Token for user '{current_user.username}' (Device: {device.device_name})")
    return device

@router.post("/unregister")
def unregister_device_token(
    device_in: DeviceTokenCreate, 
    db: Session = Depends(get_db), 
    current_user: User = Depends(get_current_user)
):
    """Hủy đăng ký FCM Token khi đăng xuất ứng dụng"""
    token_str = device_in.fcm_token.strip()
    device = db.query(DeviceToken).filter(
        DeviceToken.fcm_token == token_str,
        DeviceToken.user_id == current_user.id
    ).first()
    if device:
        device.is_active = False
        device.updated_at = datetime.utcnow()
        db.commit()
        logger.info(f"Unregistered FCM Device Token for user '{current_user.username}'")
    return {"message": "Đã hủy đăng ký thiết bị thành công."}

@router.get("", response_model=List[DeviceTokenResponse])
def get_all_devices(
    db: Session = Depends(get_db), 
    current_user: User = Depends(require_roles([UserRoleEnum.ADMIN.value]))
):
    """Danh sách tất cả thiết bị đã đăng ký nhận thông báo (Admin only)"""
    return db.query(DeviceToken).order_by(DeviceToken.updated_at.desc()).all()

@router.post("/test-push")
def send_test_push_notification(
    payload: DeviceTestPushRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Gửi thông báo đẩy thử nghiệm tới các thiết bị của tài khoản đang đăng nhập"""
    tokens = [d.fcm_token for d in current_user.device_tokens if d.is_active]
    if not tokens:
        raise HTTPException(
            status_code=400, 
            detail="Tài khoản của bạn chưa có thiết bị nào đăng ký nhận thông báo (FCM Token)."
        )

    res = send_fcm_push(
        tokens=tokens,
        title=payload.title,
        body=payload.body,
        data={"type": "test", "sent_by": current_user.username}
    )
    return {
        "success": res.get("success", False),
        "target_tokens_count": len(tokens),
        "fcm_response": res
    }
