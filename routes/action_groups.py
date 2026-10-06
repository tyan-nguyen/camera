import json
import logging
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from database import get_db
from models import ActionGroup, UserRoleEnum, Camera
from schemas import (
    ActionGroupCreate, 
    ActionGroupUpdate, 
    ActionGroupResponse, 
    ActionGroupTestRequest, 
    ActionGroupTestResponse
)
from auth_utils import require_roles, get_current_user
from disk_worker import execute_action_webhook
from datetime import datetime

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/action-groups", tags=["Action Groups"])

admin_only = require_roles([UserRoleEnum.ADMIN.value])

@router.get("", response_model=List[ActionGroupResponse])
def get_all_action_groups(db: Session = Depends(get_db)):
    """Lấy danh sách tất cả các nhóm hành động"""
    return db.query(ActionGroup).order_by(ActionGroup.id.asc()).all()

@router.get("/{group_id}", response_model=ActionGroupResponse)
def get_action_group_by_id(group_id: int, db: Session = Depends(get_db)):
    ag = db.query(ActionGroup).filter(ActionGroup.id == group_id).first()
    if not ag:
        raise HTTPException(status_code=404, detail="Không tìm thấy nhóm hành động.")
    return ag

@router.post("", response_model=ActionGroupResponse, status_code=status.HTTP_201_CREATED)
def create_action_group(
    ag_in: ActionGroupCreate, 
    db: Session = Depends(get_db), 
    current_user=Depends(admin_only)
):
    clean_code = ag_in.code.strip().upper()
    existing = db.query(ActionGroup).filter(ActionGroup.code == clean_code).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Mã nhóm hành động '{clean_code}' đã tồn tại.")

    new_ag = ActionGroup(
        name=ag_in.name.strip(),
        code=clean_code,
        api_url=ag_in.api_url.strip(),
        http_method=(ag_in.http_method or "POST").upper(),
        headers_json=ag_in.headers_json,
        description=ag_in.description,
        apply_car=ag_in.apply_car if ag_in.apply_car is not None else True,
        apply_motorcycle=ag_in.apply_motorcycle if ag_in.apply_motorcycle is not None else True,
        push_condition=(ag_in.push_condition or "DEFAULT").strip().upper(),
        is_active=ag_in.is_active
    )
    db.add(new_ag)
    db.commit()
    db.refresh(new_ag)
    return new_ag

@router.put("/{group_id}", response_model=ActionGroupResponse)
def update_action_group(
    group_id: int, 
    ag_in: ActionGroupUpdate, 
    db: Session = Depends(get_db), 
    current_user=Depends(admin_only)
):
    ag = db.query(ActionGroup).filter(ActionGroup.id == group_id).first()
    if not ag:
        raise HTTPException(status_code=404, detail="Không tìm thấy nhóm hành động.")

    if ag_in.name is not None:
        ag.name = ag_in.name.strip()
    if ag_in.code is not None:
        clean_code = ag_in.code.strip().upper()
        existing = db.query(ActionGroup).filter(ActionGroup.code == clean_code, ActionGroup.id != group_id).first()
        if existing:
            raise HTTPException(status_code=400, detail=f"Mã nhóm '{clean_code}' đã bị trùng.")
        ag.code = clean_code
    if ag_in.api_url is not None:
        ag.api_url = ag_in.api_url.strip()
    if ag_in.http_method is not None:
        ag.http_method = ag_in.http_method.upper()
    if ag_in.headers_json is not None:
        ag.headers_json = ag_in.headers_json
    if ag_in.description is not None:
        ag.description = ag_in.description
    if ag_in.apply_car is not None:
        ag.apply_car = ag_in.apply_car
    if ag_in.apply_motorcycle is not None:
        ag.apply_motorcycle = ag_in.apply_motorcycle
    if ag_in.push_condition is not None:
        ag.push_condition = ag_in.push_condition.strip().upper()
    if ag_in.is_active is not None:
        ag.is_active = ag_in.is_active

    db.commit()
    db.refresh(ag)
    return ag

@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_action_group(
    group_id: int, 
    db: Session = Depends(get_db), 
    current_user=Depends(admin_only)
):
    ag = db.query(ActionGroup).filter(ActionGroup.id == group_id).first()
    if not ag:
        raise HTTPException(status_code=404, detail="Không tìm thấy nhóm hành động.")

    # Cập nhật các camera đang gán nhóm này về NULL
    db.query(Camera).filter(Camera.action_group_id == group_id).update({Camera.action_group_id: None})
    db.delete(ag)
    db.commit()
    return None

@router.post("/{group_id}/test", response_model=ActionGroupTestResponse)
def test_action_group_webhook(
    group_id: int,
    test_req: ActionGroupTestRequest,
    db: Session = Depends(get_db),
    current_user=Depends(admin_only)
):
    """Kiểm tra gọi thử nghiệm Webhook của nhóm hành động với biển số mẫu"""
    ag = db.query(ActionGroup).filter(ActionGroup.id == group_id).first()
    if not ag:
        raise HTTPException(status_code=404, detail="Không tìm thấy nhóm hành động.")

    dummy_cam = Camera(
        id=999,
        camera_name="Camera Thử Nghiệm",
        zone_code="TEST_ZONE",
        zone_name="Khu Vực Test",
        rtsp_url="rtsp://localhost/test"
    )

    now = datetime.now()
    res = execute_action_webhook(
        action_group=ag,
        camera=dummy_cam,
        plate_number=test_req.plate_number,
        vehicle_view=test_req.vehicle_view,
        vehicle_type=test_req.vehicle_type,
        confidence_score=0.98,
        detected_at=now,
        image_full_rel="test_full.webp",
        image_plate_rel="test_plate.webp"
    )

    raw = res.get("action_result_raw", "")
    parsed = None
    try:
        parsed = json.loads(raw) if raw else None
    except Exception:
        pass

    return ActionGroupTestResponse(
        success=res.get("action_status") != "ERROR",
        status_code=200 if res.get("action_status") != "ERROR" else 500,
        raw_response=raw or "",
        parsed_result=parsed if isinstance(parsed, dict) else None,
        normalized_status=res.get("action_status", "INFO"),
        summary=res.get("summary"),
        message=res.get("message"),
        alert_level=res.get("alert_level", "normal")
    )
