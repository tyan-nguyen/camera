import logging
from typing import List, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from database import get_db
from models import SearchShortcut, Camera, UserRoleEnum
from schemas import (
    SearchShortcutCreate,
    SearchShortcutUpdate,
    SearchShortcutResponse
)
from auth_utils import require_roles

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/search-shortcuts", tags=["Search Shortcuts"])

# Quyền quản lý lối tắt: ADMIN hoặc QUAN_LY
admin_or_manager = require_roles([UserRoleEnum.ADMIN.value, UserRoleEnum.QUAN_LY.value])

def _to_response(item: SearchShortcut) -> SearchShortcutResponse:
    cam_name = item.camera.camera_name if item.camera else None
    return SearchShortcutResponse(
        id=item.id,
        name=item.name,
        camera_id=item.camera_id,
        camera_name=cam_name,
        zone_code=item.zone_code,
        vehicle_type=item.vehicle_type,
        vehicle_view=item.vehicle_view,
        vehicle_classification=item.vehicle_classification,
        action_status=item.action_status,
        plate_number=item.plate_number,
        color=item.color or "blue",
        icon=item.icon or "fa-filter",
        order_index=item.order_index or 0,
        is_active=item.is_active,
        created_at=item.created_at,
        updated_at=item.updated_at
    )

@router.get("", response_model=List[SearchShortcutResponse])
def get_search_shortcuts(
    all: bool = False,
    db: Session = Depends(get_db)
):
    """
    Lấy danh sách các lối tắt tìm kiếm lịch sử.
    Nếu all=False (mặc định), chỉ trả về các lối tắt đang kích hoạt (is_active=True).
    Nếu all=True, trả về toàn bộ để phục vụ trang Cấu hình.
    """
    query = db.query(SearchShortcut)
    if not all:
        query = query.filter(SearchShortcut.is_active == True)
    items = query.order_by(SearchShortcut.order_index.asc(), SearchShortcut.id.asc()).all()
    return [_to_response(x) for x in items]

@router.get("/{shortcut_id}", response_model=SearchShortcutResponse)
def get_search_shortcut_by_id(shortcut_id: int, db: Session = Depends(get_db)):
    item = db.query(SearchShortcut).filter(SearchShortcut.id == shortcut_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Không tìm thấy lối tắt tìm kiếm.")
    return _to_response(item)

@router.post("", response_model=SearchShortcutResponse, status_code=status.HTTP_201_CREATED)
def create_search_shortcut(
    data: SearchShortcutCreate,
    db: Session = Depends(get_db),
    current_user=Depends(admin_or_manager)
):
    """Tạo mới lối tắt tìm kiếm lịch sử"""
    name = data.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Tên lối tắt không được để trống.")

    if data.camera_id:
        cam = db.query(Camera).filter(Camera.id == data.camera_id).first()
        if not cam:
            raise HTTPException(status_code=400, detail=f"Camera ID {data.camera_id} không tồn tại.")

    new_item = SearchShortcut(
        name=name,
        camera_id=data.camera_id,
        zone_code=data.zone_code.strip() if data.zone_code else None,
        vehicle_type=data.vehicle_type.strip() if data.vehicle_type else None,
        vehicle_view=data.vehicle_view.strip() if data.vehicle_view else None,
        vehicle_classification=data.vehicle_classification.strip() if data.vehicle_classification else None,
        action_status=data.action_status.strip() if data.action_status else None,
        plate_number=data.plate_number.strip().upper() if data.plate_number else None,
        color=data.color.strip() if data.color else "blue",
        icon=data.icon.strip() if data.icon else "fa-filter",
        order_index=data.order_index or 0,
        is_active=data.is_active
    )
    db.add(new_item)
    db.commit()
    db.refresh(new_item)
    return _to_response(new_item)

@router.put("/{shortcut_id}", response_model=SearchShortcutResponse)
def update_search_shortcut(
    shortcut_id: int,
    data: SearchShortcutUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(admin_or_manager)
):
    """Cập nhật cấu hình lối tắt tìm kiếm"""
    item = db.query(SearchShortcut).filter(SearchShortcut.id == shortcut_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Không tìm thấy lối tắt tìm kiếm.")

    if data.name is not None:
        name = data.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Tên lối tắt không được để trống.")
        item.name = name

    if data.camera_id is not None:
        if data.camera_id == 0 or data.camera_id == "":
            item.camera_id = None
        else:
            cam = db.query(Camera).filter(Camera.id == data.camera_id).first()
            if not cam:
                raise HTTPException(status_code=400, detail=f"Camera ID {data.camera_id} không tồn tại.")
            item.camera_id = data.camera_id

    if data.zone_code is not None:
        item.zone_code = data.zone_code.strip() if data.zone_code else None
    if data.vehicle_type is not None:
        item.vehicle_type = data.vehicle_type.strip() if data.vehicle_type else None
    if data.vehicle_view is not None:
        item.vehicle_view = data.vehicle_view.strip() if data.vehicle_view else None
    if data.vehicle_classification is not None:
        item.vehicle_classification = data.vehicle_classification.strip() if data.vehicle_classification else None
    if data.action_status is not None:
        item.action_status = data.action_status.strip() if data.action_status else None
    if data.plate_number is not None:
        item.plate_number = data.plate_number.strip().upper() if data.plate_number else None
    if data.color is not None:
        item.color = data.color.strip() if data.color else "blue"
    if data.icon is not None:
        item.icon = data.icon.strip() if data.icon else "fa-filter"
    if data.order_index is not None:
        item.order_index = data.order_index
    if data.is_active is not None:
        item.is_active = data.is_active

    item.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(item)
    return _to_response(item)

@router.delete("/{shortcut_id}", status_code=status.HTTP_200_OK)
def delete_search_shortcut(
    shortcut_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(admin_or_manager)
):
    """Xóa lối tắt tìm kiếm"""
    item = db.query(SearchShortcut).filter(SearchShortcut.id == shortcut_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Không tìm thấy lối tắt tìm kiếm.")

    db.delete(item)
    db.commit()
    return {"success": True, "message": "Đã xóa lối tắt tìm kiếm thành công."}

