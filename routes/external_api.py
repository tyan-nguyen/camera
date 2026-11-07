import os
import time
import base64
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from models import Camera

logger = logging.getLogger(__name__)

router = APIRouter()

# Reference tới Stream Manager toàn cục
stream_manager_ref = None


def set_stream_manager(sm):
    global stream_manager_ref
    stream_manager_ref = sm


def verify_external_api_key(
    request: Request,
    api_key: Optional[str] = Query(None, description="API Key xác thực (truyền qua query param hoặc Header X-API-Key)")
):
    """
    Xác thực API Key cho ứng dụng thứ ba (Yii2, Webhook, Phần mềm Desktop...).
    Hỗ trợ truyền khóa qua:
    1. Header: X-API-Key: <key>
    2. Header: Authorization: Bearer <key>
    3. Query Parameter: ?api_key=<key> (cần thiết cho thẻ <img> của Web Yii2)
    Nếu cấu hình EXTERNAL_API_KEY để trống -> Cho phép truy cập mở trong mạng nội bộ.
    """
    configured_key = (getattr(settings, "EXTERNAL_API_KEY", "") or "").strip()
    if not configured_key:
        return True

    client_key = None

    # 1. Kiểm tra Header X-API-Key
    header_key = request.headers.get("X-API-Key")
    if header_key and header_key.strip():
        client_key = header_key.strip()

    # 2. Kiểm tra Header Authorization: Bearer <key>
    if not client_key:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            client_key = auth_header.split(" ", 1)[1].strip()

    # 3. Kiểm tra Query param ?api_key=...
    if not client_key and api_key and api_key.strip():
        client_key = api_key.strip()

    if not client_key or client_key != configured_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Xác thực không hợp lệ. API Key không chính xác hoặc chưa được cung cấp qua Header 'X-API-Key' hoặc tham số '?api_key='.",
            headers={"WWW-Authenticate": "ApiKey"}
        )
    return True


def resolve_camera(camera_identifier: Optional[str], db: Session) -> Camera:
    """
    Tìm kiếm Camera linh hoạt theo:
    1. 'default' hoặc None -> Camera active đầu tiên trong hệ thống
    2. Số ID (1, 2, 3...)
    3. Mã camera (camera_code: CAM_01, CONG_VAO...)
    4. Tên camera (camera_name)
    """
    if not camera_identifier or camera_identifier.strip().lower() in ("default", "first", "0", ""):
        cam = db.query(Camera).filter(Camera.is_active == True).order_by(Camera.display_order.asc(), Camera.id.asc()).first()
        if not cam:
            cam = db.query(Camera).order_by(Camera.id.asc()).first()
        if not cam:
            raise HTTPException(status_code=404, detail="Hệ thống chưa có camera nào được cấu hình.")
        return cam

    clean_id = camera_identifier.strip()

    # Tìm theo ID số nguyên
    if clean_id.isdigit():
        cam = db.query(Camera).filter(Camera.id == int(clean_id)).first()
        if cam:
            return cam

    # Tìm theo mã camera (camera_code)
    cam = db.query(Camera).filter(func.lower(Camera.camera_code) == clean_id.lower()).first()
    if cam:
        return cam

    # Tìm theo tên camera tương đối
    cam = db.query(Camera).filter(Camera.camera_name.ilike(f"%{clean_id}%")).first()
    if cam:
        return cam

    raise HTTPException(
        status_code=404,
        detail=f"Không tìm thấy camera với mã hoặc ID '{clean_id}' trong hệ thống."
    )


def extract_snapshot_image(
    camera_id: int,
    camera_name: str,
    width: Optional[int] = None,
    quality: int = 80,
    overlay: bool = False,
    fallback_offline: bool = True
) -> bytes:
    """Lấy dữ liệu ảnh JPEG từ Stream Manager"""
    if not stream_manager_ref:
        raise HTTPException(status_code=503, detail="Dịch vụ Stream Manager chưa khởi động.")

    thread = stream_manager_ref.threads.get(camera_id)
    is_live = False
    if thread:
        is_live = (
            thread.latest_raw_frame is not None and 
            (time.time() - thread.last_frame_received_time <= 8.0)
        )

    if not is_live and not fallback_offline:
        raise HTTPException(
            status_code=503,
            detail=f"Camera '{camera_name}' (ID: {camera_id}) hiện đang offline hoặc mất luồng tín hiệu RTSP."
        )

    target_w = width if (width and 120 <= width <= 3840) else None
    target_q = max(30, min(95, quality)) if quality else 80

    jpeg_bytes = stream_manager_ref.get_camera_snapshot(
        camera_id=camera_id,
        width=target_w,
        quality=target_q,
        overlay=overlay
    )

    if not jpeg_bytes:
        raise HTTPException(status_code=404, detail=f"Không có dữ liệu hình ảnh cho camera #{camera_id}.")
    return jpeg_bytes


@router.get("/cameras", summary="Danh sách Camera hỗ trợ tích hợp")
def list_available_cameras(
    request: Request,
    db: Session = Depends(get_db),
    _authorized: bool = Depends(verify_external_api_key)
) -> List[Dict[str, Any]]:
    """
    Trả về danh sách camera đang có trong hệ thống kèm mã và URL chụp ảnh trực tiếp.
    Hỗ trợ ứng dụng Yii2 / bên ngoài tự động khám phá mã camera.
    """
    cams = db.query(Camera).order_by(Camera.display_order.asc(), Camera.id.asc()).all()
    base_url = str(request.base_url).rstrip("/")
    result = []
    for c in cams:
        result.append({
            "id": c.id,
            "camera_code": c.camera_code or f"CAM_{c.id}",
            "camera_name": c.camera_name,
            "zone_code": c.zone_code,
            "zone_name": c.zone_name,
            "camera_function": c.camera_function,
            "is_active": c.is_active,
            "capture_url": f"{base_url}/api/external/v1/cameras/{c.id}/capture",
            "capture_json_url": f"{base_url}/api/external/v1/cameras/{c.id}/capture-json"
        })
    return result


@router.get("/cameras/{camera_identifier}/capture", summary="Chụp ảnh Camera chỉ định (Hỗ trợ Binary JPEG hoặc JSON)")
def capture_camera_image(
    camera_identifier: str,
    request: Request,
    w: Optional[int] = Query(None, alias="width", description="Thu nhỏ chiều rộng ảnh (VD: 1280, 640)"),
    q: Optional[int] = Query(80, alias="quality", description="Chất lượng nén JPEG từ 30 đến 95"),
    overlay: Optional[int] = Query(0, description="1: Vẽ vùng ROI và nhãn camera; 0: Ảnh mộc nguyên bản (khuyên dùng)"),
    format: Optional[str] = Query("raw", description="'raw': Trả về file ảnh JPEG trực tiếp; 'json': Trả về Base64"),
    fallback_offline: Optional[bool] = Query(True, description="True: Trả về ảnh thông báo offline nếu mất mạng; False: Báo lỗi 503"),
    db: Session = Depends(get_db),
    _authorized: bool = Depends(verify_external_api_key)
):
    """
    API chụp ảnh tức thời từ camera chỉ định:
    - camera_identifier: ID camera (VD: 1) hoặc Mã camera (VD: CAM_01) hoặc 'default'.
    - Mặc định trả về Binary Image (Content-Type: image/jpeg) để Yii2 nhúng trực tiếp vào thẻ <img> hoặc file_get_contents().
    """
    cam = resolve_camera(camera_identifier, db)
    show_overlay = bool(overlay == 1)
    
    jpeg_bytes = extract_snapshot_image(
        camera_id=cam.id,
        camera_name=cam.camera_name,
        width=w,
        quality=q or 80,
        overlay=show_overlay,
        fallback_offline=fallback_offline if fallback_offline is not None else True
    )

    now_iso = datetime.now().isoformat()

    # Trường hợp yêu cầu dạng JSON Base64
    if format and format.strip().lower() == "json":
        base64_str = base64.b64encode(jpeg_bytes).decode("ascii")
        return {
            "success": True,
            "camera_id": cam.id,
            "camera_code": cam.camera_code or f"CAM_{cam.id}",
            "camera_name": cam.camera_name,
            "timestamp": now_iso,
            "content_type": "image/jpeg",
            "size_bytes": len(jpeg_bytes),
            "image_base64": base64_str
        }

    # Mặc định: Trả về ảnh nhị phân JPEG trực tiếp
    headers = {
        "Content-Type": "image/jpeg",
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
        "X-Camera-Id": str(cam.id),
        "X-Camera-Code": str(cam.camera_code or f"CAM_{cam.id}"),
        "X-Camera-Name": urllib_quote(cam.camera_name),
        "X-Capture-Timestamp": now_iso
    }
    return Response(content=jpeg_bytes, media_type="image/jpeg", headers=headers)


@router.get("/cameras/{camera_identifier}/capture-json", summary="Chụp ảnh Camera dạng JSON Base64")
def capture_camera_image_json(
    camera_identifier: str,
    w: Optional[int] = Query(None, alias="width", description="Thu nhỏ chiều rộng ảnh (VD: 1280, 640)"),
    q: Optional[int] = Query(80, alias="quality", description="Chất lượng nén JPEG từ 30 đến 95"),
    overlay: Optional[int] = Query(0, description="1: Vẽ vùng ROI; 0: Ảnh mộc nguyên bản"),
    fallback_offline: Optional[bool] = Query(True, description="True: Trả về ảnh offline; False: Báo lỗi 503"),
    db: Session = Depends(get_db),
    _authorized: bool = Depends(verify_external_api_key)
):
    """Shortcut chuyên dụng trả về chuỗi Base64 cùng thông tin camera dạng JSON"""
    cam = resolve_camera(camera_identifier, db)
    show_overlay = bool(overlay == 1)

    jpeg_bytes = extract_snapshot_image(
        camera_id=cam.id,
        camera_name=cam.camera_name,
        width=w,
        quality=q or 80,
        overlay=show_overlay,
        fallback_offline=fallback_offline if fallback_offline is not None else True
    )

    base64_str = base64.b64encode(jpeg_bytes).decode("ascii")
    return {
        "success": True,
        "camera_id": cam.id,
        "camera_code": cam.camera_code or f"CAM_{cam.id}",
        "camera_name": cam.camera_name,
        "timestamp": datetime.now().isoformat(),
        "content_type": "image/jpeg",
        "size_bytes": len(jpeg_bytes),
        "image_base64": base64_str
    }


@router.get("/capture", summary="Chụp ảnh nhanh (Mặc định lấy camera active đầu tiên hoặc chỉ định qua ?camera=)")
def capture_shortcut(
    camera: Optional[str] = Query(None, description="ID hoặc Mã camera (bỏ trống để lấy camera mặc định)"),
    w: Optional[int] = Query(None, alias="width", description="Thu nhỏ chiều rộng ảnh"),
    q: Optional[int] = Query(80, alias="quality", description="Chất lượng JPEG (30-95)"),
    overlay: Optional[int] = Query(0, description="1: Vẽ vùng ROI; 0: Ảnh mộc"),
    format: Optional[str] = Query("raw", description="'raw': Ảnh JPEG; 'json': Base64"),
    fallback_offline: Optional[bool] = Query(True, description="True: Ảnh offline; False: Báo lỗi 503"),
    db: Session = Depends(get_db),
    _authorized: bool = Depends(verify_external_api_key)
):
    """
    Endpoint rút gọn cho Yii2:
    - Gọi GET /api/external/v1/capture (tự lấy camera mặc định)
    - Hoặc GET /api/external/v1/capture?camera=1
    """
    cam_id_str = camera or "default"
    cam = resolve_camera(cam_id_str, db)
    show_overlay = bool(overlay == 1)

    jpeg_bytes = extract_snapshot_image(
        camera_id=cam.id,
        camera_name=cam.camera_name,
        width=w,
        quality=q or 80,
        overlay=show_overlay,
        fallback_offline=fallback_offline if fallback_offline is not None else True
    )

    now_iso = datetime.now().isoformat()

    if format and format.strip().lower() == "json":
        base64_str = base64.b64encode(jpeg_bytes).decode("ascii")
        return {
            "success": True,
            "camera_id": cam.id,
            "camera_code": cam.camera_code or f"CAM_{cam.id}",
            "camera_name": cam.camera_name,
            "timestamp": now_iso,
            "content_type": "image/jpeg",
            "size_bytes": len(jpeg_bytes),
            "image_base64": base64_str
        }

    headers = {
        "Content-Type": "image/jpeg",
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
        "X-Camera-Id": str(cam.id),
        "X-Camera-Code": str(cam.camera_code or f"CAM_{cam.id}"),
        "X-Camera-Name": urllib_quote(cam.camera_name),
        "X-Capture-Timestamp": now_iso
    }
    return Response(content=jpeg_bytes, media_type="image/jpeg", headers=headers)


def urllib_quote(text: str) -> str:
    """Helper encode header ASCII an toàn cho tiếng Việt"""
    if not text:
        return ""
    import urllib.parse
    return urllib.parse.quote(text)

