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
admin_only = require_roles([UserRoleEnum.ADMIN.value])

@router.get("", response_model=PaginatedVehicleLogs)
def search_vehicle_logs(
    request: Request,
    plate_number: Optional[str] = Query(None, description="Biển số cần tìm kiếm"),
    camera_id: Optional[str] = Query(None, description="ID camera"),
    zone_code: Optional[str] = Query(None, description="Mã vùng (TRUONGLAI, KHO_BAI...)"),
    action_group_id: Optional[int] = Query(None, description="ID nhóm hành động"),
    action_status: Optional[str] = Query(None, description="Trạng thái hành động (APPROVED, UNPLANNED, REJECTED, WARNING...)"),
    vehicle_classification: Optional[str] = Query(None, description="Phân loại đối tượng xe (INTERNAL_VALID, INTERNAL_UNPLANNED, EXTERNAL_UNKNOWN...)"),
    vehicle_view: Optional[str] = Query(None, description="Góc nhìn phương tiện (front, rear, unknown)"),
    vehicle_type: Optional[str] = Query(None, description="Loại phương tiện (car, motorcycle)"),
    start_date: Optional[str] = Query(None, description="Thời gian bắt đầu (YYYY-MM-DD HH:MM:SS)"),
    end_date: Optional[str] = Query(None, description="Thời gian kết thúc (YYYY-MM-DD HH:MM:SS)"),
    is_deleted: bool = Query(False, description="Xem bản ghi trong thùng rác nếu True"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db)
):
    current_user = get_current_user_optional(request, db)
    query = db.query(VehicleLog)

    # 0. Lọc Thùng rác (Xóa mềm vs Chưa xóa)
    if is_deleted:
        if not current_user or current_user.role != UserRoleEnum.ADMIN.value:
            raise HTTPException(status_code=403, detail="Chỉ Quản trị viên (Admin) mới có quyền truy cập Thùng rác.")
        query = query.filter(VehicleLog.is_deleted == True)
    else:
        query = query.filter(VehicleLog.is_deleted == False)

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

    # 6.1. Bộ lọc theo Phân loại Đối tượng Xe (Nội bộ có kế hoạch / không kế hoạch / xe ngoài)
    if vehicle_classification and vehicle_classification.strip() and vehicle_classification.strip().upper() != "ALL":
        query = query.filter(VehicleLog.vehicle_classification == vehicle_classification.strip().upper())

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

from disk_worker import (
    dispatch_vehicle_log_update_webhook,
    dispatch_vehicle_log_delete_webhook,
    dispatch_vehicle_log_restore_webhook
)
from websocket_manager import websocket_manager

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
    
    old_plate = log_item.plate_number
    old_view = log_item.vehicle_view
    old_type = log_item.vehicle_type

    # Lưu vết bản ghi gốc trước lần sửa đầu tiên
    if not getattr(log_item, "is_edited", False):
        log_item.original_plate_number = old_plate
        log_item.original_vehicle_view = old_view

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
    if payload.vehicle_classification is not None:
        log_item.vehicle_classification = payload.vehicle_classification.strip().upper()

    log_item.is_edited = True
    log_item.updated_at = datetime.utcnow()
    log_item.updated_by = current_user.username if current_user else "admin"

    # Gọi Webhook đồng bộ cập nhật tới đối tác (nếu có cấu hình webhook cho hướng chụp)
    webhook_res = dispatch_vehicle_log_update_webhook(
        log_item=log_item,
        old_plate=old_plate,
        old_view=old_view,
        old_type=old_type,
        updated_by=log_item.updated_by,
        db=db
    )
    if webhook_res:
        if webhook_res.get("sync_status") == "PENDING_SYNC":
            log_item.sync_status = "PENDING_SYNC"
            log_item.sync_error = webhook_res.get("message")
        else:
            log_item.sync_status = "SYNCED"
            log_item.last_sync_at = datetime.utcnow()
            log_item.sync_error = None
            if webhook_res.get("vehicle_classification"):
                log_item.vehicle_classification = webhook_res["vehicle_classification"]
            if webhook_res.get("action_status"):
                log_item.action_status = webhook_res["action_status"]
            if webhook_res.get("summary"):
                log_item.summary = webhook_res["summary"]
            if webhook_res.get("alert_level"):
                log_item.alert_level = webhook_res["alert_level"]
            if webhook_res.get("action_result_raw"):
                log_item.action_result_raw = webhook_res["action_result_raw"]

    db.commit()
    db.refresh(log_item)

    cam = db.query(Camera).filter(Camera.id == log_item.camera_id).first()
    res = VehicleLogResponse.model_validate(log_item)
    res.camera_name = cam.camera_name if cam else f"Cam #{log_item.camera_id}"
    res.image_full_path = f"/static/captures/{log_item.image_full_path}"
    res.image_plate_path = f"/static/captures/{log_item.image_plate_path}"

    # Phát thông báo WebSocket tới tất cả client Web
    try:
        ws_payload = {
            "event": "vehicle_updated",
            "data": {
                "id": log_item.id,
                "camera_id": log_item.camera_id,
                "camera_name": res.camera_name,
                "plate_number": log_item.plate_number,
                "vehicle_view": log_item.vehicle_view,
                "vehicle_type": log_item.vehicle_type,
                "confidence_score": log_item.confidence_score,
                "zone_code": log_item.zone_code,
                "action_status": log_item.action_status,
                "vehicle_classification": log_item.vehicle_classification,
                "alert_level": log_item.alert_level,
                "summary": log_item.summary,
                "detected_at": log_item.detected_at.strftime("%Y-%m-%d %H:%M:%S"),
                "is_edited": True,
                "original_plate_number": log_item.original_plate_number,
                "updated_by": log_item.updated_by,
                "sync_status": log_item.sync_status
            }
        }
        websocket_manager.broadcast_sync(ws_payload)
    except Exception:
        pass

    return res

@router.get("/trash/count")
def get_trash_count(
    db: Session = Depends(get_db),
    current_user = Depends(admin_only)
):
    """Lấy số lượng sự kiện đang ở trong thùng rác (Chỉ Admin)"""
    count = db.query(VehicleLog).filter(VehicleLog.is_deleted == True).count()
    return {"count": count}

@router.delete("/trash/empty")
def empty_trash_bin(
    db: Session = Depends(get_db),
    current_user = Depends(admin_only)
):
    """Xóa vĩnh viễn toàn bộ sự kiện trong thùng rác khỏi CSDL (Chỉ Admin, KHÔNG gọi webhook)"""
    deleted_count = db.query(VehicleLog).filter(VehicleLog.is_deleted == True).delete()
    db.commit()
    return {"success": True, "deleted_count": deleted_count}

@router.delete("/{log_id}")
def delete_vehicle_log(
    log_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(manager_or_admin)
):
    """Xóa lần 1 (Xóa mềm / Soft Delete): Đánh dấu đã xóa, chuyển vào thùng rác & gọi Webhook DELETE"""
    log_item = db.query(VehicleLog).filter(VehicleLog.id == log_id).first()
    if not log_item:
        raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi nhận dạng.")
    
    log_item.is_deleted = True
    log_item.deleted_at = datetime.utcnow()
    log_item.deleted_by = current_user.username if current_user else "admin"
    db.commit()
    db.refresh(log_item)

    # Kích hoạt gọi Webhook DELETE sang API (nếu camera có bật sự kiện DELETE)
    try:
        dispatch_vehicle_log_delete_webhook(log_item, log_item.deleted_by, db)
    except Exception:
        pass

    # Phát thông báo WebSocket tới tất cả client
    try:
        websocket_manager.broadcast_sync({
            "event": "vehicle_deleted",
            "data": {
                "id": log_id,
                "plate_number": log_item.plate_number
            }
        })
    except Exception:
        pass

    return {"success": True, "message": f"Đã chuyển sự kiện xe {log_item.plate_number} vào thùng rác."}

@router.post("/{log_id}/restore", response_model=VehicleLogResponse)
def restore_vehicle_log(
    log_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(admin_only)
):
    """Khôi phục sự kiện từ thùng rác & gọi Webhook RESTORE (Chỉ Admin)"""
    log_item = db.query(VehicleLog).filter(VehicleLog.id == log_id).first()
    if not log_item:
        raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi nhận dạng.")

    log_item.is_deleted = False
    log_item.deleted_at = None
    log_item.deleted_by = None
    db.commit()
    db.refresh(log_item)

    # Kích hoạt gọi Webhook RESTORE sang API để đối soát lại kế hoạch mới nhất
    try:
        webhook_res = dispatch_vehicle_log_restore_webhook(log_item, current_user.username if current_user else "admin", db)
        if webhook_res:
            if webhook_res.get("sync_status") == "PENDING_SYNC":
                log_item.sync_status = "PENDING_SYNC"
                log_item.sync_error = webhook_res.get("message")
            else:
                log_item.sync_status = "SYNCED"
                log_item.last_sync_at = datetime.utcnow()
                log_item.sync_error = None
                if webhook_res.get("vehicle_classification"):
                    log_item.vehicle_classification = webhook_res["vehicle_classification"]
                if webhook_res.get("action_status"):
                    log_item.action_status = webhook_res["action_status"]
                if webhook_res.get("summary"):
                    log_item.summary = webhook_res["summary"]
                if webhook_res.get("alert_level"):
                    log_item.alert_level = webhook_res["alert_level"]
                if webhook_res.get("action_result_raw"):
                    log_item.action_result_raw = webhook_res["action_result_raw"]
            db.commit()
            db.refresh(log_item)
    except Exception:
        pass

    cam = db.query(Camera).filter(Camera.id == log_item.camera_id).first()
    res = VehicleLogResponse.model_validate(log_item)
    res.camera_name = cam.camera_name if cam else f"Cam #{log_item.camera_id}"
    res.image_full_path = f"/static/captures/{log_item.image_full_path}"
    res.image_plate_path = f"/static/captures/{log_item.image_plate_path}"

    # Phát thông báo WebSocket vehicle_restored
    try:
        websocket_manager.broadcast_sync({
            "event": "vehicle_restored",
            "data": {
                "id": log_item.id,
                "camera_id": log_item.camera_id,
                "camera_name": res.camera_name,
                "plate_number": log_item.plate_number,
                "action_status": log_item.action_status,
                "vehicle_classification": log_item.vehicle_classification,
                "summary": log_item.summary,
                "sync_status": log_item.sync_status,
                "detected_at": log_item.detected_at.strftime("%Y-%m-%d %H:%M:%S")
            }
        })
    except Exception:
        pass

    return res

@router.delete("/{log_id}/permanent", status_code=status.HTTP_204_NO_CONTENT)
def permanent_delete_vehicle_log(
    log_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(admin_only)
):
    """Xóa vĩnh viễn sự kiện khỏi CSDL (Chỉ Admin, KHÔNG gọi webhook nữa)"""
    log_item = db.query(VehicleLog).filter(VehicleLog.id == log_id).first()
    if not log_item:
        raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi nhận dạng.")

    db.delete(log_item)
    db.commit()
    return None
