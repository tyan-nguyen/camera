import hashlib
import hmac
import json
import base64
import time
import secrets
from typing import Optional, List, Dict, Any
from fastapi import Request, HTTPException, status, Depends
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from models import User, UserRoleEnum

# Secret key for HMAC token signing
AUTH_SECRET_KEY = getattr(settings, "AUTH_SECRET_KEY", "ANPR_SECRET_KEY_SECURE_AUTH_2026_NT_SUPER")
TOKEN_EXPIRE_SECONDS = 7 * 24 * 3600  # 7 days

def hash_password(password: str, salt: Optional[str] = None) -> str:
    """Mã hóa mật khẩu bằng PBKDF2-HMAC-SHA256 với Salt ngẫu nhiên"""
    if not salt:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        'sha256',
        password.encode('utf-8'),
        salt.encode('utf-8'),
        100000
    )
    return f"{salt}${dk.hex()}"

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Xác thực mật khẩu người dùng nhập"""
    try:
        if not hashed_password or '$' not in hashed_password:
            return False
        salt, hash_val = hashed_password.split('$', 1)
        expected = hash_password(plain_password, salt=salt)
        return hmac.compare_digest(expected, hashed_password)
    except Exception:
        return False

def create_access_token(user: User) -> str:
    """Tạo JWT-like HMAC Token có chứa user_id, username, role và expire_at"""
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "exp": int(time.time()) + TOKEN_EXPIRE_SECONDS
    }
    payload_json = json.dumps(payload, separators=(',', ':'))
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode('utf-8')).decode('utf-8').rstrip('=')
    
    signature = hmac.new(
        AUTH_SECRET_KEY.encode('utf-8'),
        payload_b64.encode('utf-8'),
        hashlib.sha256
    ).digest()
    sig_b64 = base64.urlsafe_b64encode(signature).decode('utf-8').rstrip('=')
    
    return f"{payload_b64}.{sig_b64}"

def decode_access_token(token: str) -> Optional[Dict[str, Any]]:
    """Giải mã và kiểm tra tính hợp lệ của Token"""
    try:
        if not token or '.' not in token:
            return None
        payload_b64, sig_b64 = token.split('.', 1)
        
        # Verify signature
        expected_sig = hmac.new(
            AUTH_SECRET_KEY.encode('utf-8'),
            payload_b64.encode('utf-8'),
            hashlib.sha256
        ).digest()
        
        # Pad base64 for decoding
        padded_sig = sig_b64 + '=' * ((4 - len(sig_b64) % 4) % 4)
        received_sig = base64.urlsafe_b64decode(padded_sig.encode('utf-8'))
        
        if not hmac.compare_digest(expected_sig, received_sig):
            return None
        
        padded_payload = payload_b64 + '=' * ((4 - len(payload_b64) % 4) % 4)
        payload_str = base64.urlsafe_b64decode(padded_payload.encode('utf-8')).decode('utf-8')
        payload = json.loads(payload_str)
        
        # Check expiration
        if payload.get("exp", 0) < int(time.time()):
            return None
        
        return payload
    except Exception:
        return None

def extract_token_from_request(request: Request) -> Optional[str]:
    """Lấy token từ Cookie hoặc Header Authorization"""
    # 1. Check Authorization header: Bearer <token>
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header.split(" ", 1)[1].strip()
    
    # 2. Check Cookie: access_token
    cookie_token = request.cookies.get("access_token")
    if cookie_token:
        return cookie_token.strip()
    
    # 3. Check Query parameter: ?token= (useful for live image stream/SSE)
    query_token = request.query_params.get("token")
    if query_token:
        return query_token.strip()
    
    return None

def get_current_user_optional(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    """Lấy thông tin người dùng đang đăng nhập nếu có, không báo lỗi nếu chưa đăng nhập"""
    token = extract_token_from_request(request)
    if not token:
        return None
    payload = decode_access_token(token)
    if not payload:
        return None
    user_id = payload.get("sub")
    if not user_id:
        return None
    user = db.query(User).filter(User.id == int(user_id), User.is_active == True).first()
    return user

def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Dependency bắt buộc phải đăng nhập (trả về 401 nếu chưa đăng nhập)"""
    user = get_current_user_optional(request, db)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Phiên đăng nhập đã hết hạn hoặc không hợp lệ. Vui lòng đăng nhập lại.",
            headers={"WWW-Authenticate": "Bearer"}
        )
    return user

def require_roles(allowed_roles: List[str]):
    """Dependency kiểm tra phân quyền tài khoản (trả về 403 nếu không đủ quyền)"""
    def role_checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Tài khoản của bạn ({current_user.role}) không có quyền thực hiện thao tác này."
            )
        return current_user
    return role_checker
