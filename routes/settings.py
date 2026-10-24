import json
import urllib.request
import urllib.parse
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from schemas import SystemSettingsResponse, SystemSettingsUpdate, TestVisionRequest, TestVehicleLookupRequest
from settings_manager import settings_manager
from models import UserRoleEnum
from auth_utils import require_roles
from vision_api import test_vision_connection

router = APIRouter(prefix="/api/v1/settings", tags=["System Settings"])

admin_only = require_roles([UserRoleEnum.ADMIN.value])

@router.get("", response_model=SystemSettingsResponse)
def get_system_settings():
    current = settings_manager.get_settings()
    return SystemSettingsResponse(
        id=1,
        ocr_engine_type=current.get("ocr_engine_type", "yolo_local"),
        lmstudio_base_url=current.get("lmstudio_base_url", "http://localhost:1234/v1"),
        lmstudio_model_name=current.get("lmstudio_model_name", "default"),
        lmstudio_api_key=current.get("lmstudio_api_key", ""),
        openai_api_key=current.get("openai_api_key", ""),
        openai_model_name=current.get("openai_model_name", "gpt-4o-mini"),
        gemini_api_key=current.get("gemini_api_key", ""),
        gemini_model_name=current.get("gemini_model_name", "gemini-2.0-flash"),
        fcm_server_key=current.get("fcm_server_key", ""),
        fcm_project_id=current.get("fcm_project_id", ""),
        video_storage_path=current.get("video_storage_path", "storage/recordings"),
        video_segment_minutes=current.get("video_segment_minutes", 5),
        video_retention_days=current.get("video_retention_days", 15),
        auto_cleanup_disk=current.get("auto_cleanup_disk", True),
        enable_event_video_clip=current.get("enable_event_video_clip", True),
        event_video_pre_seconds=current.get("event_video_pre_seconds", 5),
        event_video_post_seconds=current.get("event_video_post_seconds", 5),
        vehicle_lookup_api_url=current.get("vehicle_lookup_api_url", "http://localhost:9999/api2/take-plate-by-number"),
        vehicle_lookup_api_token=current.get("vehicle_lookup_api_token", "truonglai_camera_secure_token_2026"),
        updated_at=datetime.utcnow()
    )

@router.put("", response_model=SystemSettingsResponse)
def update_system_settings(settings_in: SystemSettingsUpdate, current_user=Depends(admin_only)):
    update_data = settings_in.model_dump(exclude_unset=True)
    updated = settings_manager.update_settings(update_data)
    return SystemSettingsResponse(
        id=1,
        ocr_engine_type=updated.get("ocr_engine_type", "yolo_local"),
        lmstudio_base_url=updated.get("lmstudio_base_url", "http://localhost:1234/v1"),
        lmstudio_model_name=updated.get("lmstudio_model_name", "default"),
        lmstudio_api_key=updated.get("lmstudio_api_key", ""),
        openai_api_key=updated.get("openai_api_key", ""),
        openai_model_name=updated.get("openai_model_name", "gpt-4o-mini"),
        gemini_api_key=updated.get("gemini_api_key", ""),
        gemini_model_name=updated.get("gemini_model_name", "gemini-2.0-flash"),
        fcm_server_key=updated.get("fcm_server_key", ""),
        fcm_project_id=updated.get("fcm_project_id", ""),
        video_storage_path=updated.get("video_storage_path", "storage/recordings"),
        video_segment_minutes=updated.get("video_segment_minutes", 5),
        video_retention_days=updated.get("video_retention_days", 15),
        auto_cleanup_disk=updated.get("auto_cleanup_disk", True),
        enable_event_video_clip=updated.get("enable_event_video_clip", True),
        event_video_pre_seconds=updated.get("event_video_pre_seconds", 5),
        event_video_post_seconds=updated.get("event_video_post_seconds", 5),
        vehicle_lookup_api_url=updated.get("vehicle_lookup_api_url", "http://localhost:9999/api2/take-plate-by-number"),
        vehicle_lookup_api_token=updated.get("vehicle_lookup_api_token", "truonglai_camera_secure_token_2026"),
        updated_at=datetime.utcnow()
    )

@router.post("/test-vision")
def test_vision_api_endpoint(req: TestVisionRequest, current_user=Depends(admin_only)):
    """Kiểm tra kết nối tới mô hình AI Vision (LM Studio, OpenAI, Gemini)"""
    res = test_vision_connection(
        engine_type=req.engine_type,
        base_url=req.base_url or "http://localhost:1234/v1",
        model_name=req.model_name or "default",
        api_key=req.api_key or ""
    )
    return res

@router.post("/test-vehicle-lookup")
def test_vehicle_lookup_api(req: TestVehicleLookupRequest, current_user=Depends(admin_only)):
    """Kiểm tra kết nối thử nghiệm tới API tra cứu biển số xe theo mã sát hạch (Yii2 / Server trường lái)"""
    api_url = (req.api_url or "").strip()
    api_token = (req.api_token or "").strip()
    test_number = (req.test_number or "01").strip()

    if not api_url:
        return {"success": False, "message": "Vui lòng nhập URL Endpoint của API tra cứu."}

    delimiter = "&" if "?" in api_url else "?"
    query_str = urllib.parse.urlencode({"number": test_number, "token": api_token})
    target_url = f"{api_url}{delimiter}{query_str}"

    try:
        req_obj = urllib.request.Request(
            target_url,
            headers={"User-Agent": "CameraANPR-System/1.0", "Accept": "application/json"}
        )
        with urllib.request.urlopen(req_obj, timeout=4.0) as response:
            body_text = response.read().decode("utf-8").strip()

            try:
                parsed_data = json.loads(body_text)
            except Exception:
                parsed_data = body_text.strip('"')

            if parsed_data and str(parsed_data).lower() != "null":
                return {
                    "success": True,
                    "plate_number": str(parsed_data).strip().upper(),
                    "message": f"Kết nối API thành công! Mã xe [{test_number}] trả về biển số: {parsed_data}"
                }
            else:
                return {
                    "success": True,
                    "plate_number": None,
                    "message": f"Kết nối API thành công! API hoạt động tốt nhưng mã xe [{test_number}] trả về: null (không có biển số)."
                }
    except urllib.error.HTTPError as he:
        if he.code == 401:
            return {"success": False, "message": "Kết nối thất bại (401 Unauthorized): Token bảo mật không chính xác."}
        elif he.code == 404:
            return {"success": False, "message": f"Kết nối thất bại (404 Not Found): Đường dẫn API không tồn tại ({api_url})."}
        return {"success": False, "message": f"Lỗi phản hồi từ API (HTTP {he.code}): {he.reason}"}
    except urllib.error.URLError as ue:
        return {"success": False, "message": f"Không thể kết nối đến máy chủ API: {ue.reason}"}
    except Exception as e:
        return {"success": False, "message": f"Lỗi không xác định khi gọi API: {str(e)}"}
