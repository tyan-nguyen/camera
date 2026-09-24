import time
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Response, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from database import get_db, SessionLocal
from models import Camera, UserRoleEnum, ActionGroup, User
from schemas import CameraCreate, CameraResponse, CameraUpdate
from auth_utils import require_roles, get_current_user_optional, get_current_user

router = APIRouter(prefix="/api/v1/cameras", tags=["Cameras"])

stream_manager_ref = None

def set_stream_manager(sm):
    global stream_manager_ref
    stream_manager_ref = sm

admin_only = require_roles([UserRoleEnum.ADMIN.value])

def build_camera_response(cam: Camera) -> CameraResponse:
    ag_name = cam.action_group.name if cam.action_group else None
    return CameraResponse(
        id=cam.id,
        camera_name=cam.camera_name,
        rtsp_url=cam.rtsp_url,
        target_fps=cam.target_fps,
        is_active=cam.is_active,
        zone_code=cam.zone_code or "TRUONGLAI",
        zone_name=cam.zone_name or "Trường Lái",
        camera_function=cam.camera_function or "ANPR",
        action_group_id=cam.action_group_id,
        action_group_name=ag_name,
        detection_zone=cam.detection_zone,
        created_at=cam.created_at,
        updated_at=cam.updated_at
    )

def check_camera_access(camera_id: int, user: Optional[User]) -> bool:
    """Kiểm tra quyền truy cập camera của user. Admin toàn quyền; user thường chỉ camera được cấp."""
    if not user:
        return True  # Fallback nếu chưa bật auth bắt buộc
    if user.role == UserRoleEnum.ADMIN.value:
        return True
    allowed_ids = [c.id for c in user.allowed_cameras] if user.allowed_cameras else []
    return camera_id in allowed_ids

@router.get("", response_model=List[CameraResponse])
def get_all_cameras(
    request: Request,
    zone_code: Optional[str] = None,
    camera_function: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """
    Lấy danh sách camera:
    - Tự động lọc theo phân quyền của User đang đăng nhập (Admin toàn quyền, User chỉ camera được cấp)
    - Hỗ trợ lọc theo Vùng (zone_code: TRUONGLAI, KHO_BAI...)
    - Hỗ trợ lọc theo Chức năng (camera_function: ANPR, SURVEILLANCE)
    """
    current_user = get_current_user_optional(request, db)
    query = db.query(Camera)

    # 1. Phân quyền User
    if current_user and current_user.role != UserRoleEnum.ADMIN.value:
        allowed_ids = [c.id for c in current_user.allowed_cameras] if current_user.allowed_cameras else []
        query = query.filter(Camera.id.in_(allowed_ids))

    # 2. Lọc theo vùng
    if zone_code and zone_code.strip() and zone_code.upper() != "ALL":
        query = query.filter(Camera.zone_code == zone_code.strip().upper())

    # 3. Lọc theo chức năng
    if camera_function and camera_function.strip() and camera_function.upper() != "ALL":
        query = query.filter(Camera.camera_function == camera_function.strip().upper())

    cams = query.order_by(Camera.id.asc()).all()
    return [build_camera_response(c) for c in cams]

@router.get("/zones")
def get_all_zones(db: Session = Depends(get_db)):
    """Lấy danh sách các Vùng (Zone) duy nhất đang có trong hệ thống"""
    zones = db.query(Camera.zone_code, Camera.zone_name).distinct().all()
    result = []
    seen = set()
    for zc, zn in zones:
        if zc and zc not in seen:
            seen.add(zc)
            result.append({
                "zone_code": zc,
                "zone_name": zn or zc
            })
    if not result:
        result = [
            {"zone_code": "TRUONGLAI", "zone_name": "Trường Lái"},
            {"zone_code": "KHO_BAI", "zone_name": "Kho Bãi"}
        ]
    return result

@router.get("/{camera_id}", response_model=CameraResponse)
def get_camera_by_id(camera_id: int, request: Request, db: Session = Depends(get_db)):
    current_user = get_current_user_optional(request, db)
    if not check_camera_access(camera_id, current_user):
        raise HTTPException(status_code=403, detail="Tài khoản của bạn không có quyền xem camera này.")

    cam = db.query(Camera).filter(Camera.id == camera_id).first()
    if not cam:
        raise HTTPException(status_code=404, detail="Không tìm thấy Camera.")
    return build_camera_response(cam)

@router.post("", response_model=CameraResponse, status_code=status.HTTP_201_CREATED)
def create_camera(cam_in: CameraCreate, db: Session = Depends(get_db), current_user=Depends(admin_only)):
    zone_code = (cam_in.zone_code or "TRUONGLAI").strip().upper()
    zone_name = cam_in.zone_name.strip() if cam_in.zone_name else ("Trường Lái" if zone_code == "TRUONGLAI" else "Kho Bãi")
    camera_function = (cam_in.camera_function or "ANPR").strip().upper()

    new_cam = Camera(
        camera_name=cam_in.camera_name.strip(),
        rtsp_url=cam_in.rtsp_url.strip(),
        target_fps=cam_in.target_fps,
        is_active=cam_in.is_active,
        zone_code=zone_code,
        zone_name=zone_name,
        camera_function=camera_function,
        action_group_id=cam_in.action_group_id,
        detection_zone=cam_in.detection_zone
    )
    db.add(new_cam)
    db.commit()
    db.refresh(new_cam)

    if stream_manager_ref and new_cam.is_active:
        import threading
        threading.Thread(
            target=stream_manager_ref.start_camera,
            kwargs=dict(
                camera_id=new_cam.id,
                camera_name=new_cam.camera_name,
                rtsp_url=new_cam.rtsp_url,
                target_fps=new_cam.target_fps,
                detection_zone=new_cam.detection_zone,
                camera_function=new_cam.camera_function
            ),
            daemon=True
        ).start()

    return build_camera_response(new_cam)

@router.put("/{camera_id}", response_model=CameraResponse)
def update_camera(camera_id: int, cam_in: CameraUpdate, db: Session = Depends(get_db), current_user=Depends(admin_only)):
    cam = db.query(Camera).filter(Camera.id == camera_id).first()
    if not cam:
        raise HTTPException(status_code=404, detail="Không tìm thấy Camera.")

    if cam_in.camera_name is not None:
        cam.camera_name = cam_in.camera_name.strip()
    if cam_in.rtsp_url is not None:
        cam.rtsp_url = cam_in.rtsp_url.strip()
    if cam_in.target_fps is not None:
        cam.target_fps = cam_in.target_fps
    if cam_in.is_active is not None:
        cam.is_active = cam_in.is_active
    if cam_in.zone_code is not None:
        cam.zone_code = cam_in.zone_code.strip().upper()
    if cam_in.zone_name is not None:
        cam.zone_name = cam_in.zone_name.strip()
    if cam_in.camera_function is not None:
        cam.camera_function = cam_in.camera_function.strip().upper()
    
    # Xử lý gán / hủy gán Nhóm Hành Động an toàn
    if "action_group_id" in cam_in.model_fields_set:
        if cam_in.action_group_id and cam_in.action_group_id > 0:
            ag = db.query(ActionGroup).filter(ActionGroup.id == cam_in.action_group_id).first()
            cam.action_group_id = ag.id if ag else None
        else:
            cam.action_group_id = None

    if cam_in.detection_zone is not None:
        cam.detection_zone = cam_in.detection_zone

    db.commit()
    db.refresh(cam)

    if stream_manager_ref:
        stream_manager_ref.update_camera_runtime(
            camera_id=cam.id,
            camera_name=cam.camera_name,
            rtsp_url=cam.rtsp_url,
            target_fps=cam.target_fps,
            detection_zone=cam.detection_zone,
            camera_function=cam.camera_function,
            is_active=cam.is_active
        )

    return build_camera_response(cam)

@router.delete("/{camera_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_camera(camera_id: int, db: Session = Depends(get_db), current_user=Depends(admin_only)):
    cam = db.query(Camera).filter(Camera.id == camera_id).first()
    if not cam:
        raise HTTPException(status_code=404, detail="Không tìm thấy Camera.")

    if stream_manager_ref:
        stream_manager_ref.stop_camera(camera_id)

    db.delete(cam)
    db.commit()
    return None

@router.get("/{camera_id}/snapshot")
def get_camera_snapshot(camera_id: int, request: Request):
    db = SessionLocal()
    try:
        current_user = get_current_user_optional(request, db)
        if not check_camera_access(camera_id, current_user):
            raise HTTPException(status_code=403, detail="Tài khoản của bạn không có quyền xem hình ảnh camera này.")
    finally:
        db.close()

    if not stream_manager_ref:
        raise HTTPException(status_code=503, detail="Stream Manager chưa sẵn sàng.")
    jpeg_bytes = stream_manager_ref.get_camera_snapshot(camera_id)
    if not jpeg_bytes:
        raise HTTPException(status_code=404, detail="Không có hình ảnh snapshot.")
    return Response(
        content=jpeg_bytes, 
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )

@router.get("/{camera_id}/stream")
def get_camera_stream(camera_id: int, request: Request):
    db = SessionLocal()
    try:
        current_user = get_current_user_optional(request, db)
        if not check_camera_access(camera_id, current_user):
            raise HTTPException(status_code=403, detail="Tài khoản của bạn không có quyền xem luồng camera này.")
    finally:
        db.close()

    if not stream_manager_ref:
        raise HTTPException(status_code=503, detail="Stream Manager chưa sẵn sàng.")

    def mjpeg_generator():
        while True:
            jpeg_bytes = stream_manager_ref.get_camera_snapshot(camera_id)
            if jpeg_bytes:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + jpeg_bytes + b'\r\n')
            time.sleep(0.08)

    return StreamingResponse(mjpeg_generator(), media_type="multipart/x-mixed-replace; boundary=frame")
