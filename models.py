from datetime import datetime
from sqlalchemy import Column, Integer, BigInteger, String, Boolean, Float, DateTime, ForeignKey, Text, Table, Enum as SQLEnum
from sqlalchemy.orm import relationship
import enum
from database import Base

class VehicleViewEnum(str, enum.Enum):
    FRONT = "front"
    REAR = "rear"
    UNKNOWN = "unknown"

class VehicleTypeEnum(str, enum.Enum):
    CAR = "car"                   # Xe ô tô (gồm ô tô con, xe tải, xe buýt, xe khách, container...)
    MOTORCYCLE = "motorcycle"     # Xe máy, mô tô 2 bánh
    UNKNOWN = "unknown"

class CameraFunctionEnum(str, enum.Enum):
    ANPR = "ANPR"                   # Camera đọc biển số (thực hiện nhận dạng vào - ra)
    SURVEILLANCE = "SURVEILLANCE"   # Camera quan sát (chỉ xem trực tiếp, không nhận dạng)

# Bảng liên kết nhiều - nhiều: Phân quyền Camera cho từng Tài khoản người dùng
user_camera_permissions = Table(
    "user_camera_permissions",
    Base.metadata,
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("camera_id", Integer, ForeignKey("cameras.id", ondelete="CASCADE"), primary_key=True),
    mysql_charset='utf8mb4',
    mysql_collate='utf8mb4_unicode_ci'
)

class ActionGroup(Base):
    """Nhóm camera theo hành động: kiểm tra đăng ký lịch, kiểm tra thuê xe, kiểm tra lấy đơn..."""
    __tablename__ = "action_groups"
    __table_args__ = {'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    name = Column(String(255), nullable=False)                         # e.g. "Kiểm tra đăng ký lịch"
    code = Column(String(100), unique=True, index=True, nullable=False) # e.g. "KIEM_TRA_DANG_KY_LICH"
    api_url = Column(String(500), nullable=False)                      # URL Webhook API để gọi sau khi nhận dạng
    http_method = Column(String(10), default="POST")                   # GET / POST
    headers_json = Column(String(1000), nullable=True, default=None)   # Custom HTTP headers (JSON string)
    description = Column(String(255), nullable=True, default=None)
    apply_car = Column(Boolean, default=True)                          # Áp dụng Webhook cho toàn bộ nhóm Xe Ô Tô (ô tô, tải, buýt)
    apply_motorcycle = Column(Boolean, default=True)                   # Áp dụng Webhook cho Xe Máy
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    cameras = relationship("Camera", back_populates="action_group")

class Camera(Base):
    __tablename__ = "cameras"
    __table_args__ = {'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    camera_name = Column(String(255), nullable=False)
    rtsp_url = Column(String(500), nullable=False)
    target_fps = Column(Integer, default=6)
    is_active = Column(Boolean, default=True)
    is_recording = Column(Boolean, default=False)  # Bật/Tắt lưu video riêng cho camera này
    
    # 1. Nhóm theo vùng (TRUONGLAI, KHO_BAI, TRU_SO...)
    zone_code = Column(String(50), default="TRUONGLAI", index=True)
    zone_name = Column(String(100), default="Trường Lái")
    
    # 2. Nhóm theo chức năng (ANPR: đọc biển số, SURVEILLANCE: quan sát)
    camera_function = Column(String(50), default=CameraFunctionEnum.ANPR.value)
    
    # 3. Nhóm theo hành động (Liên kết tới bảng action_groups)
    action_group_id = Column(Integer, ForeignKey("action_groups.id", ondelete="SET NULL"), nullable=True)

    detection_zone = Column(String(2000), nullable=True, default=None)  # JSON lưu danh sách các điểm polygon vùng khoanh ROI
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    action_group = relationship("ActionGroup", back_populates="cameras")
    logs = relationship("VehicleLog", back_populates="camera", cascade="all, delete-orphan")
    users = relationship("User", secondary=user_camera_permissions, back_populates="allowed_cameras")
    recordings = relationship("VideoRecording", back_populates="camera", cascade="all, delete-orphan")
    export_jobs = relationship("VideoExportJob", back_populates="camera", cascade="all, delete-orphan")

class VideoRecording(Base):
    """Lưu trữ metadata của các đoạn video phân đoạn (Continuous / Segmented Video Recording)"""
    __tablename__ = "video_recordings"
    __table_args__ = {'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}

    id = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    camera_id = Column(Integer, ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False, index=True)
    start_time = Column(DateTime, nullable=False, index=True)
    end_time = Column(DateTime, nullable=True, index=True)
    file_path = Column(String(500), nullable=False)                    # Đường dẫn file MP4 tương đối hoặc tuyệt đối
    file_size_mb = Column(Float, default=0.0)                          # Dung lượng MB
    duration_seconds = Column(Integer, default=0)                      # Thời lượng thực tế tính theo giây
    status = Column(String(30), default="completed", index=True)       # recording, completed, error
    created_at = Column(DateTime, default=datetime.utcnow)

    camera = relationship("Camera", back_populates="recordings")

class VideoExportJob(Base):
    """Lưu trữ lịch sử các đoạn video clip được trích xuất (Cắt video MP4 theo yêu cầu)"""
    __tablename__ = "video_export_jobs"
    __table_args__ = {'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}

    id = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    camera_id = Column(Integer, ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(255), nullable=True)                         # Tiêu đề hoặc ghi chú đoạn video trích xuất
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=False)
    output_file_path = Column(String(500), nullable=False)             # File MP4 kết quả
    file_size_mb = Column(Float, default=0.0)
    duration_seconds = Column(Integer, default=0)
    status = Column(String(30), default="completed", index=True)       # pending, processing, completed, failed
    error_message = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    camera = relationship("Camera", back_populates="export_jobs")

class VehicleLog(Base):
    __tablename__ = "vehicle_logs"
    __table_args__ = {'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}

    id = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    camera_id = Column(Integer, ForeignKey("cameras.id"), nullable=False)
    plate_number = Column(String(50), index=True, nullable=False)
    vehicle_view = Column(String(20), default=VehicleViewEnum.UNKNOWN.value)
    vehicle_type = Column(String(20), default=VehicleTypeEnum.CAR.value, index=True) # car, motorcycle, unknown
    confidence_score = Column(Float, default=0.0)
    image_full_path = Column(String(500), nullable=False)
    image_plate_path = Column(String(500), nullable=False)
    
    # Thông tin phân loại & hành động tại thời điểm ghi nhận
    zone_code = Column(String(50), nullable=True, index=True)
    action_group_id = Column(Integer, nullable=True)
    action_group_name = Column(String(255), nullable=True)
    action_status = Column(String(50), nullable=True, index=True)      # APPROVED, UNPLANNED, REJECTED, WARNING, INFO
    action_result_raw = Column(Text, nullable=True)                    # JSON chi tiết phản hồi từ Webhook API
    alert_level = Column(String(20), default="normal")                 # normal, warning, danger
    
    summary = Column(String(250), nullable=True, default=None)
    detected_at = Column(DateTime, default=datetime.utcnow, index=True)

    camera = relationship("Camera", back_populates="logs")

class DeviceToken(Base):
    """Quản lý FCM Token thiết bị Android / iOS để nhận thông báo đẩy"""
    __tablename__ = "device_tokens"
    __table_args__ = {'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}

    id = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    fcm_token = Column(String(255), nullable=False, index=True)
    device_name = Column(String(255), nullable=True)                   # e.g. "Samsung Galaxy S23", "Xiaomi Redmi Note"
    platform = Column(String(20), default="android")                  # android, ios, web
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="device_tokens")

class SystemSettings(Base):
    __tablename__ = "system_settings"
    __table_args__ = {'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}

    id = Column(Integer, primary_key=True, default=1)
    ocr_engine_type = Column(String(50), default="yolo_local")  # yolo_local, lmstudio_api, openai_api, gemini_api
    
    # Cấu hình LM Studio Model Local (VLM)
    lmstudio_base_url = Column(String(500), default="http://localhost:1234/v1")
    lmstudio_model_name = Column(String(100), default="default")
    lmstudio_api_key = Column(String(500), default="", nullable=True)

    # Cấu hình OpenAI Vision API
    openai_api_key = Column(String(500), default="")
    openai_model_name = Column(String(100), default="gpt-4o-mini")
    
    # Cấu hình Gemini Vision API
    gemini_api_key = Column(String(500), default="")
    gemini_model_name = Column(String(100), default="gemini-2.0-flash")
    
    # Cấu hình Firebase Cloud Messaging (FCM Push Notification)
    fcm_server_key = Column(String(500), default="", nullable=True)
    fcm_project_id = Column(String(100), default="", nullable=True)

    # Cấu hình Lưu trữ Video & Playback
    video_storage_path = Column(String(500), default="storage/recordings", nullable=True)
    video_segment_minutes = Column(Integer, default=5, nullable=True)      # Thời lượng phân đoạn video (5, 10, 15 phút)
    video_retention_days = Column(Integer, default=15, nullable=True)      # Tự động dọn dẹp sau N ngày
    auto_cleanup_disk = Column(Boolean, default=True, nullable=True)       # Tự động dọn dẹp khi ổ đĩa đầy

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class UserRoleEnum(str, enum.Enum):
    ADMIN = "admin"        # Toàn quyền cấu hình, camera, tài khoản, sửa/xóa log
    QUAN_LY = "quan_ly"    # Xem camera được phân quyền, xem sự kiện, sửa biển số, xóa sự kiện
    USER = "user"          # Chỉ xem camera và xem sự kiện được phân quyền

class User(Base):
    __tablename__ = "users"
    __table_args__ = {'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    username = Column(String(100), unique=True, index=True, nullable=False)
    full_name = Column(String(255), nullable=True)
    hashed_password = Column(String(255), nullable=False)
    role = Column(String(50), default=UserRoleEnum.USER.value, nullable=False)  # admin, quan_ly, user
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    allowed_cameras = relationship("Camera", secondary=user_camera_permissions, back_populates="users")
    device_tokens = relationship("DeviceToken", back_populates="user", cascade="all, delete-orphan")
