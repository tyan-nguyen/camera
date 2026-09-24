from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from database import get_db
from models import User, UserRoleEnum, Camera
from schemas import UserLoginRequest, UserResponse, UserChangePassword
from auth_utils import verify_password, hash_password, create_access_token, get_current_user, TOKEN_EXPIRE_SECONDS
from routes.users import build_user_response

router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])

@router.post("/login")
def login(login_data: UserLoginRequest, response: Response, db: Session = Depends(get_db)):
    username = login_data.username.strip().lower()
    user = db.query(User).filter(User.username == username).first()
    
    if not user or not verify_password(login_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Tên đăng nhập hoặc mật khẩu không chính xác."
        )
    
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tài khoản này đã bị khóa. Vui lòng liên hệ quản trị viên."
        )
    
    token = create_access_token(user)
    
    # Set Cookie for browser requests
    response.set_cookie(
        key="access_token",
        value=token,
        max_age=TOKEN_EXPIRE_SECONDS,
        httponly=False,
        samesite="lax",
        path="/"
    )
    
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": build_user_response(user)
    }

@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(key="access_token", path="/")
    return {"message": "Đã đăng xuất thành công."}

@router.get("/me", response_model=UserResponse)
def get_current_user_profile(current_user: User = Depends(get_current_user)):
    return build_user_response(current_user)

@router.put("/change-password")
def change_password(payload: UserChangePassword, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not verify_password(payload.old_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Mật khẩu cũ không chính xác."
        )
    
    if len(payload.new_password.strip()) < 4:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Mật khẩu mới phải có tối thiểu 4 ký tự."
        )
    
    current_user.hashed_password = hash_password(payload.new_password)
    db.commit()
    return {"message": "Đổi mật khẩu thành công!"}
