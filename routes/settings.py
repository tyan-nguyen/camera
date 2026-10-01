from fastapi import APIRouter, Depends, HTTPException
from schemas import SystemSettingsResponse, SystemSettingsUpdate, TestVisionRequest
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
        updated_at="2026-08-19T00:00:00"
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
        updated_at="2026-08-19T00:00:00"
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
