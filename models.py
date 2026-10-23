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

class TrafficDirectionEnum(str, enum.Enum):
    TWO_WAY = "TWO_WAY"                  # Cổng 1 Camera: Hai chiều tự động (Trước = Đi ra, Sau = Về bãi)
    OUT_FRONT_ONLY = "OUT_FRONT_ONLY"    # Cổng 1 Làn Chung / Cổng Ra: Chiều RA chỉ bắt mặt trước (Bỏ qua đuôi xe vào)
    IN_FRONT_ONLY = "IN_FRONT_ONLY"      # Cổng 1 Làn Chung / Cổng Vào: Chiều VÀO chỉ bắt mặt trước (Bỏ qua đuôi xe ra)
    OUT_ALL = "OUT_ALL"                  # Cổng 2 Làn Riêng: Làn RA riêng biệt (Mọi góc chụp đều là xe ra)
    IN_ALL = "IN_ALL"                    # Cổng 2 Làn Riêng: Làn VÀO riêng biệt (Mọi góc chụp đều là xe vào)

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
    auth_token = Column(String(500), nullable=True, default=None)       # API Token / Bearer Token
    auth_type = Column(String(50), default="BEARER", nullable=False)    # BEARER, API_KEY, NONE
    description = Column(String(255), nullable=True, default=None)
    apply_car = Column(Boolean, default=True)                          # Áp dụng Webhook cho toàn bộ nhóm Xe Ô Tô (ô tô, tải, buýt)
    apply_motorcycle = Column(Boolean, default=True)                   # Áp dụng Webhook cho Xe Máy
    push_condition = Column(String(50), default="DEFAULT", nullable=False) # DEFAULT (Luôn gửi), WHEN_TRUE (Chỉ khi true), WHEN_FALSE (Chỉ khi false), NEVER (Tắt thông báo)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    cameras = relationship("Camera", foreign_keys="[Camera.action_group_id]", back_populates="action_group")

class Camera(Base):
    __tablename__ = "cameras"
    __table_args__ = {'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    camera_name = Column(String(255), nullable=False)
    camera_code = Column(String(100), nullable=True, index=True)  # Mã định danh camera đồng bộ API đối tác (e.g. cong_cantin, cong_san_cam_bien)
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
    action_group_front_id = Column(Integer, ForeignKey("action_groups.id", ondelete="SET NULL"), nullable=True)
    action_group_rear_id = Column(Integer, ForeignKey("action_groups.id", ondelete="SET NULL"), nullable=True)

    # 4. Hướng luồng giao thông của Camera (TWO_WAY, OUT_FRONT_ONLY, IN_FRONT_ONLY, OUT_ALL, IN_ALL)
    traffic_direction = Column(String(50), default=TrafficDirectionEnum.TWO_WAY.value, nullable=False)

    detection_zone = Column(String(2000), nullable=True, default=None)  # JSON lưu danh sách các điểm polygon vùng khoanh ROI
    webhook_events = Column(String(100), default="CREATE,UPDATE")        # Danh sách sự kiện gửi webhook (CREATE,UPDATE,DELETE,RESTORE)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    action_group = relationship("ActionGroup", foreign_keys=[action_group_id], back_populates="cameras")
    action_group_front = relationship("ActionGroup", foreign_keys=[action_group_front_id])
    action_group_rear = relationship("ActionGroup", foreign_keys=[action_group_rear_id])
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
    video_path = Column(String(500), nullable=True)                    # Đường dẫn video clip ngắn (-5s đến +5s)
    
    # Thông tin phân loại & hành động tại thời điểm ghi nhận
    zone_code = Column(String(50), nullable=True, index=True)
    action_group_id = Column(Integer, nullable=True)
    action_group_name = Column(String(255), nullable=True)
    action_status = Column(String(50), nullable=True, index=True)      # APPROVED, UNPLANNED, REJECTED, WARNING, INFO
    vehicle_classification = Column(String(50), nullable=True, index=True) # INTERNAL_VALID, INTERNAL_UNPLANNED, EXTERNAL_UNKNOWN...
    action_result_raw = Column(Text, nullable=True)                    # JSON chi tiết phản hồi từ Webhook API
    alert_level = Column(String(20), default="normal")                 # normal, warning, danger
    
    summary = Column(String(250), nullable=True, default=None)
    detected_at = Column(DateTime, default=datetime.utcnow, index=True)

    # Thông tin chỉnh sửa thủ công
    is_edited = Column(Boolean, default=False, index=True)
    original_plate_number = Column(String(50), nullable=True)
    original_vehicle_view = Column(String(20), nullable=True)
    updated_by = Column(String(100), nullable=True)
    updated_at = Column(DateTime, nullable=True)

    # Quản lý Xóa mềm & Thùng rác
    is_deleted = Column(Boolean, default=False, index=True)
    deleted_at = Column(DateTime, nullable=True, index=True)
    deleted_by = Column(String(100), nullable=True)

    # Quản lý Trạng thái Đồng bộ Webhook (Transactional Outbox)
    sync_status = Column(String(30), default="SYNCED", index=True)     # SYNCED, PENDING_SYNC, SYNC_FAILED, NOT_APPLICABLE
    last_sync_at = Column(DateTime, nullable=True)
    sync_error = Column(Text, nullable=True)

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
    enable_event_video_clip = Column(Boolean, default=True, nullable=True) # Lưu kèm video clip ngắn (-5s đến +5s)
    event_video_pre_seconds = Column(Integer, default=5, nullable=True)    # Số giây trước sự kiện
    event_video_post_seconds = Column(Integer, default=5, nullable=True)   # Số giây sau sự kiện

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

class WebhookSyncQueue(Base):
    """
    Bảng lưu trữ hàng đợi đồng bộ Webhook (Transactional Outbox Queue)
    Đảm bảo 100% không mất dữ liệu khi API đối tác bị mất kết nối, sập server hoặc chập chờn.
    Hỗ trợ phát lại tự động (Auto-Replay) theo FIFO và thử lại theo cơ chế Exponential Backoff.
    """
    __tablename__ = "webhook_sync_queue"
    __table_args__ = {'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}

    id = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    action_group_id = Column(Integer, ForeignKey("action_groups.id", ondelete="CASCADE"), nullable=False, index=True)
    log_id = Column(BigInteger, ForeignKey("vehicle_logs.id", ondelete="CASCADE"), nullable=True, index=True)
    camera_id = Column(Integer, ForeignKey("cameras.id", ondelete="SET NULL"), nullable=True, index=True)
    
    event_action = Column(String(20), nullable=False, index=True)       # CREATE, UPDATE, DELETE, RESTORE
    event_id = Column(String(100), nullable=False, index=True)          # EVT_... định danh duy nhất chống lặp
    plate_number = Column(String(50), nullable=True, index=True)
    payload_json = Column(Text, nullable=False)                          # Chuỗi JSON body chuẩn bị gửi
    
    status = Column(String(30), default="PENDING", index=True)           # PENDING, SYNCING, SUCCESS, FAILED, CANCELLED
    retry_count = Column(Integer, default=0)
    max_retries = Column(Integer, default=10)
    next_retry_at = Column(DateTime, default=datetime.utcnow, index=True)
    last_error = Column(Text, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    synced_at = Column(DateTime, nullable=True)

    action_group = relationship("ActionGroup")
    log = relationship("VehicleLog")
    camera = relationship("Camera")

