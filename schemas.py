from datetime import datetime
from typing import Optional, List, Any, Dict
from pydantic import BaseModel, ConfigDict

# ==========================================
# 1. Action Group Schemas (Nhóm hành động & Webhook)
# ==========================================
class ActionGroupBase(BaseModel):
    name: str
    code: str
    api_url: str
    http_method: str = "POST"
    headers_json: Optional[str] = None
    description: Optional[str] = None
    is_active: bool = True

class ActionGroupCreate(ActionGroupBase):
    pass

class ActionGroupUpdate(BaseModel):
    name: Optional[str] = None
    code: Optional[str] = None
    api_url: Optional[str] = None
    http_method: Optional[str] = None
    headers_json: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None

class ActionGroupResponse(ActionGroupBase):
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

class ActionGroupTestRequest(BaseModel):
    plate_number: str = "51A-12345"
    vehicle_view: str = "front"

class ActionGroupTestResponse(BaseModel):
    success: bool
    status_code: int
    raw_response: str
    parsed_result: Optional[Dict[str, Any]] = None
    normalized_status: str = "INFO"
    summary: Optional[str] = None
    message: Optional[str] = None
    alert_level: str = "normal"

# ==========================================
# 2. Camera Schemas (Phân loại Vùng, Chức năng, Hành động)
# ==========================================
class CameraBase(BaseModel):
    camera_name: str
    rtsp_url: str
    target_fps: int = 6
    is_active: bool = True
    is_recording: bool = False             # Bật/Tắt lưu video cho camera này
    zone_code: str = "TRUONGLAI"           # TRUONGLAI, KHO_BAI, TRU_SO...
    zone_name: str = "Trường Lái"
    camera_function: str = "ANPR"          # ANPR (Đọc biển số), SURVEILLANCE (Quan sát)
    action_group_id: Optional[int] = None
    detection_zone: Optional[str] = None

class CameraCreate(CameraBase):
    pass

class CameraUpdate(BaseModel):
    camera_name: Optional[str] = None
    rtsp_url: Optional[str] = None
    target_fps: Optional[int] = None
    is_active: Optional[bool] = None
    is_recording: Optional[bool] = None
    zone_code: Optional[str] = None
    zone_name: Optional[str] = None
    camera_function: Optional[str] = None
    action_group_id: Optional[int] = None
    detection_zone: Optional[str] = None

class CameraResponse(CameraBase):
    id: int
    action_group_name: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

# ==========================================
# 3. Vehicle Log Schemas (Sự kiện nhận diện)
# ==========================================
class VehicleLogBase(BaseModel):
    camera_id: int
    plate_number: str
    vehicle_view: str = "unknown"
    confidence_score: float = 0.0
    image_full_path: str
    image_plate_path: str
    zone_code: Optional[str] = None
    action_group_id: Optional[int] = None
    action_group_name: Optional[str] = None
    action_status: Optional[str] = None
    action_result_raw: Optional[str] = None
    alert_level: str = "normal"
    summary: Optional[str] = None

class VehicleLogCreate(VehicleLogBase):
    pass

class VehicleLogResponse(VehicleLogBase):
    id: int
    detected_at: datetime
    camera_name: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

class PaginatedVehicleLogs(BaseModel):
    total: int
    page: int
    page_size: int
    items: List[VehicleLogResponse]

class VehicleLogUpdate(BaseModel):
    plate_number: Optional[str] = None
    vehicle_view: Optional[str] = None
    summary: Optional[str] = None
    action_status: Optional[str] = None
    alert_level: Optional[str] = None

# ==========================================
# 4. Device Token Schemas (FCM Push Notification)
# ==========================================
class DeviceTokenCreate(BaseModel):
    fcm_token: str
    device_name: Optional[str] = "Android Device"
    platform: str = "android"

class DeviceTokenResponse(BaseModel):
    id: int
    user_id: int
    fcm_token: str
    device_name: Optional[str]
    platform: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

class DeviceTestPushRequest(BaseModel):
    title: str = "Thử nghiệm Thông báo ANPR"
    body: str = "Hệ thống kết nối Firebase Cloud Messaging (FCM) thành công!"

# ==========================================
# 5. System Settings Schemas
# ==========================================
class SystemSettingsBase(BaseModel):
    ocr_engine_type: str = "yolo_local"  # yolo_local, openai_api, gemini_api
    openai_api_key: Optional[str] = ""
    openai_model_name: Optional[str] = "gpt-4o-mini"
    gemini_api_key: Optional[str] = ""
    gemini_model_name: Optional[str] = "gemini-2.0-flash"
    fcm_server_key: Optional[str] = ""
    fcm_project_id: Optional[str] = ""
    video_storage_path: Optional[str] = "storage/recordings"
    video_segment_minutes: Optional[int] = 5
    video_retention_days: Optional[int] = 15
    auto_cleanup_disk: Optional[bool] = True

class SystemSettingsUpdate(BaseModel):
    ocr_engine_type: Optional[str] = None
    openai_api_key: Optional[str] = None
    openai_model_name: Optional[str] = None
    gemini_api_key: Optional[str] = None
    gemini_model_name: Optional[str] = None
    fcm_server_key: Optional[str] = None
    fcm_project_id: Optional[str] = None
    video_storage_path: Optional[str] = None
    video_segment_minutes: Optional[int] = None
    video_retention_days: Optional[int] = None
    auto_cleanup_disk: Optional[bool] = None

class SystemSettingsResponse(SystemSettingsBase):
    id: int
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

# ==========================================
# 6. Video Recording & Playback Schemas
# ==========================================
class VideoRecordingResponse(BaseModel):
    id: int
    camera_id: int
    camera_name: Optional[str] = None
    start_time: datetime
    end_time: Optional[datetime] = None
    file_path: str
    file_size_mb: float
    duration_seconds: int
    status: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

class TimelineBlock(BaseModel):
    id: int
    start_time: str
    end_time: str
    start_seconds: float     # Số giây tính từ 00:00:00 của ngày được chọn
    end_seconds: float
    duration_seconds: int
    file_size_mb: float

class TimelineEvent(BaseModel):
    id: int
    detected_at: str
    seconds_in_day: float    # Vị trí giây trong ngày
    plate_number: str
    action_status: Optional[str] = None
    alert_level: str = "normal"
    image_full_url: str

class PlaybackTimelineResponse(BaseModel):
    camera_id: int
    camera_name: str
    date: str                # YYYY-MM-DD
    total_recordings: int
    total_events: int
    blocks: List[TimelineBlock]
    events: List[TimelineEvent]

class VideoExportRequest(BaseModel):
    camera_id: int
    start_time: str          # YYYY-MM-DD HH:MM:SS
    end_time: str            # YYYY-MM-DD HH:MM:SS
    title: Optional[str] = None

class VideoExportResponse(BaseModel):
    id: int
    camera_id: int
    title: Optional[str] = None
    start_time: datetime
    end_time: datetime
    output_file_path: str
    download_url: str
    file_size_mb: float
    duration_seconds: int
    status: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

# ==========================================
# 6. User & Permission Schemas
# ==========================================
class UserLoginRequest(BaseModel):
    username: str
    password: str

class UserCreate(BaseModel):
    username: str
    full_name: Optional[str] = None
    password: str
    role: str = "user"  # admin, quan_ly, user
    is_active: bool = True
    assigned_camera_ids: Optional[List[int]] = []

class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    password: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    assigned_camera_ids: Optional[List[int]] = None

class UserChangePassword(BaseModel):
    old_password: str
    new_password: str

class UserResponse(BaseModel):
    id: int
    username: str
    full_name: Optional[str] = None
    role: str
    is_active: bool
    assigned_camera_ids: List[int] = []
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
