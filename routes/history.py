from typing import Optional
from datetime import datetime
from fastapi import APIRouter, Depends, Query, HTTPException, Request, status
from sqlalchemy.orm import Session
from sqlalchemy import desc

from database import get_db
from models import VehicleLog, Camera, UserRoleEnum
from schemas import PaginatedVehicleLogs, VehicleLogResponse, VehicleLogUpdate
from auth_utils import get_current_user_optional, require_roles

router = APIRouter(prefix="/api/v1/vehicle-logs", tags=["Vehicle Logs History"])

manager_or_admin = require_roles([UserRoleEnum.ADMIN.value, UserRoleEnum.QUAN_LY.value])

@router.get("", response_model=PaginatedVehicleLogs)
def search_vehicle_logs(
    request: Request,
    plate_number: Optional[str] = Query(None, description="Biển số cần tìm kiếm"),
    camera_id: Optional[str] = Query(None, description="ID camera"),
    zone_code: Optional[str] = Query(None, description="Mã vùng (TRUONGLAI, KHO_BAI...)"),
    action_group_id: Optional[int] = Query(None, description="ID nhóm hành động"),
    action_status: Optional[str] = Query(None, description="Trạng thái hành động (APPROVED, UNPLANNED, REJECTED, WARNING...)"),
    vehicle_view: Optional[str] = Query(None, description="Góc nhìn phương tiện (front, rear, unknown)"),
    vehicle_type: Optional[str] = Query(None, description="Loại phương tiện (car, motorcycle)"),
    start_date: Optional[str] = Query(None, description="Thời gian bắt đầu (YYYY-MM-DD HH:MM:SS)"),
    end_date: Optional[str] = Query(None, description="Thời gian kết thúc (YYYY-MM-DD HH:MM:SS)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db)
):
    current_user = get_current_user_optional(request, db)
    query = db.query(VehicleLog)

    # 1. Phân quyền User: Không phải admin chỉ thấy sự kiện của camera được cấp
    if current_user and current_user.role != UserRoleEnum.ADMIN.value:
        allowed_ids = [c.id for c in current_user.allowed_cameras] if current_user.allowed_cameras else []
        query = query.filter(VehicleLog.camera_id.in_(allowed_ids))

    # 2. Bộ lọc theo biển số
    if plate_number and plate_number.strip():
        clean_plate = plate_number.strip().upper()
        query = query.filter(VehicleLog.plate_number.like(f"%{clean_plate}%"))

    # 3. Bộ lọc theo Camera ID
    if camera_id and camera_id.strip() and camera_id.strip().upper() != "ALL":
        try:
            cid = int(camera_id.strip())
            query = query.filter(VehicleLog.camera_id == cid)
        except ValueError:
            pass

    # 4. Bộ lọc theo Vùng
    if zone_code and zone_code.strip() and zone_code.strip().upper() != "ALL":
        query = query.filter(VehicleLog.zone_code == zone_code.strip().upper())

    # 5. Bộ lọc theo Nhóm Hành Động
    if action_group_id and action_group_id > 0:
        query = query.filter(VehicleLog.action_group_id == action_group_id)

    # 6. Bộ lọc theo Trạng thái Hành động
    if action_status and action_status.strip() and action_status.strip().upper() != "ALL":
        query = query.filter(VehicleLog.action_status == action_status.strip().upper())

    # 7. Bộ lọc theo Góc nhìn (Mặt trước / Mặt sau)
    if vehicle_view and vehicle_view.strip() and vehicle_view.strip().lower() != "all":
        clean_view = vehicle_view.strip().lower()
        query = query.filter(VehicleLog.vehicle_view == clean_view)

    # 8. Bộ lọc theo Loại phương tiện (Ô tô / Xe máy)
    if vehicle_type and vehicle_type.strip() and vehicle_type.strip().lower() != "all":
        clean_type = vehicle_type.strip().lower()
        query = query.filter(VehicleLog.vehicle_type == clean_type)

    # 9. Bộ lọc theo Thời gian
    if start_date:
        try:
            dt_start = datetime.strptime(start_date, "%Y-%m-%d %H:%M:%S")
            query = query.filter(VehicleLog.detected_at >= dt_start)
        except ValueError:
            pass

    if end_date:
        try:
            dt_end = datetime.strptime(end_date, "%Y-%m-%d %H:%M:%S")
            query = query.filter(VehicleLog.detected_at <= dt_end)
        except ValueError:
            pass

    total = query.count()
    logs = query.order_by(desc(VehicleLog.detected_at)).offset((page - 1) * page_size).limit(page_size).all()

    # Dynamic camera name mapping
    cam_dict = {c.id: c.camera_name for c in db.query(Camera).all()}

    response_items = []
    for item in logs:
        res = VehicleLogResponse.model_validate(item)
        res.camera_name = cam_dict.get(item.camera_id, f"Cam #{item.camera_id}")
        res.image_full_path = f"/static/captures/{item.image_full_path}"
        res.image_plate_path = f"/static/captures/{item.image_plate_path}"
        response_items.append(res)

    return PaginatedVehicleLogs(
        total=total,
        page=page,
        page_size=page_size,
        items=response_items
    )

@router.put("/{log_id}", response_model=VehicleLogResponse)
def update_vehicle_log(
    log_id: int,
    payload: VehicleLogUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(manager_or_admin)
):
    """Chỉnh sửa thông tin biển số xe hoặc trạng thái (Quyền Quản Lý / Admin)"""
    log_item = db.query(VehicleLog).filter(VehicleLog.id == log_id).first()
    if not log_item:
        raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi nhận dạng.")
    
    if payload.plate_number is not None:
        log_item.plate_number = payload.plate_number.strip().upper()
    if payload.vehicle_view is not None:
        log_item.vehicle_view = payload.vehicle_view.strip().lower()
    if payload.vehicle_type is not None:
        log_item.vehicle_type = payload.vehicle_type.strip().lower()
    if payload.summary is not None:
        log_item.summary = payload.summary.strip() if payload.summary.strip() else None
    if payload.action_status is not None:
        log_item.action_status = payload.action_status.strip().upper()
    if payload.alert_level is not None:
        log_item.alert_level = payload.alert_level.strip().lower()

    db.commit()
    db.refresh(log_item)

    cam = db.query(Camera).filter(Camera.id == log_item.camera_id).first()
    res = VehicleLogResponse.model_validate(log_item)
    res.camera_name = cam.camera_name if cam else f"Cam #{log_item.camera_id}"
    res.image_full_path = f"/static/captures/{log_item.image_full_path}"
    res.image_plate_path = f"/static/captures/{log_item.image_plate_path}"
    return res

@router.delete("/{log_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_vehicle_log(
    log_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(manager_or_admin)
):
    """Xóa sự kiện (Quyền Quản Lý / Admin)"""
    log_item = db.query(VehicleLog).filter(VehicleLog.id == log_id).first()
    if not log_item:
        raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi nhận dạng.")
    
    db.delete(log_item)
    db.commit()
    return None
