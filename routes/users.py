from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from database import get_db
from models import User, UserRoleEnum, Camera
from schemas import UserCreate, UserUpdate, UserResponse
from auth_utils import hash_password, require_roles

router = APIRouter(prefix="/api/v1/users", tags=["User Management"])

admin_required = require_roles([UserRoleEnum.ADMIN.value])

def build_user_response(user: User) -> UserResponse:
    assigned_ids = [c.id for c in user.allowed_cameras] if user.allowed_cameras else []
    return UserResponse(
        id=user.id,
        username=user.username,
        full_name=user.full_name,
        role=user.role,
        is_active=user.is_active,
        assigned_camera_ids=assigned_ids,
        created_at=user.created_at,
        updated_at=user.updated_at
    )

@router.get("", response_model=List[UserResponse])
def get_all_users(db: Session = Depends(get_db), current_user: User = Depends(admin_required)):
    users = db.query(User).order_by(User.id.asc()).all()
    return [build_user_response(u) for u in users]

@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create_user(user_in: UserCreate, db: Session = Depends(get_db), current_user: User = Depends(admin_required)):
    username = user_in.username.strip().lower()
    if not username:
        raise HTTPException(status_code=400, detail="Tên đăng nhập không được để trống.")
    
    existing = db.query(User).filter(User.username == username).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Tên đăng nhập '{username}' đã tồn tại trong hệ thống.")
    
    if user_in.role not in [UserRoleEnum.ADMIN.value, UserRoleEnum.QUAN_LY.value, UserRoleEnum.USER.value]:
        raise HTTPException(status_code=400, detail="Vai trò không hợp lệ (admin, quan_ly, user).")
    
    new_user = User(
        username=username,
        full_name=user_in.full_name,
        hashed_password=hash_password(user_in.password),
        role=user_in.role,
        is_active=user_in.is_active
    )

    if user_in.assigned_camera_ids:
        cams = db.query(Camera).filter(Camera.id.in_(user_in.assigned_camera_ids)).all()
        new_user.allowed_cameras = cams

    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return build_user_response(new_user)

@router.put("/{user_id}", response_model=UserResponse)
def update_user(user_id: int, user_in: UserUpdate, db: Session = Depends(get_db), current_user: User = Depends(admin_required)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản người dùng.")
    
    if user_in.full_name is not None:
        user.full_name = user_in.full_name
    if user_in.role is not None:
        if user_in.role not in [UserRoleEnum.ADMIN.value, UserRoleEnum.QUAN_LY.value, UserRoleEnum.USER.value]:
            raise HTTPException(status_code=400, detail="Vai trò không hợp lệ.")
        if user.id == current_user.id and user_in.role != UserRoleEnum.ADMIN.value:
            admin_count = db.query(User).filter(User.role == UserRoleEnum.ADMIN.value, User.is_active == True).count()
            if admin_count <= 1:
                raise HTTPException(status_code=400, detail="Không thể hạ quyền của tài khoản Admin duy nhất.")
        user.role = user_in.role
    if user_in.is_active is not None:
        if user.id == current_user.id and not user_in.is_active:
            raise HTTPException(status_code=400, detail="Bạn không thể tự khóa tài khoản của chính mình.")
        user.is_active = user_in.is_active
    if user_in.password and user_in.password.strip():
        user.hashed_password = hash_password(user_in.password.strip())
    
    if user_in.assigned_camera_ids is not None:
        cams = db.query(Camera).filter(Camera.id.in_(user_in.assigned_camera_ids)).all()
        user.allowed_cameras = cams

    db.commit()
    db.refresh(user)
    return build_user_response(user)

@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: int, db: Session = Depends(get_db), current_user: User = Depends(admin_required)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản người dùng.")
    
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="Bạn không thể tự xóa tài khoản của chính mình.")
    
    db.delete(user)
    db.commit()
    return None
