import threading
import logging
from typing import Dict
from database import SessionLocal
from models import SystemSettings

logger = logging.getLogger(__name__)

class DynamicSettingsManager:
    """Quản lý cấu hình hệ thống động (Hot-reload configuration)"""
    _instance = None
    _lock = threading.RLock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(DynamicSettingsManager, cls).__new__(cls)
                cls._instance._init_cache()
            return cls._instance

    def _init_cache(self):
        self.cache: Dict = {
            "ocr_engine_type": "yolo_local",
            "lmstudio_base_url": "http://localhost:1234/v1",
            "lmstudio_model_name": "default",
            "lmstudio_api_key": "",
            "openai_api_key": "",
            "openai_model_name": "gpt-4o-mini",
            "gemini_api_key": "",
            "gemini_model_name": "gemini-2.0-flash",
            "fcm_server_key": "",
            "fcm_project_id": "",
            "video_storage_path": "storage/recordings",
            "video_segment_minutes": 5,
            "video_retention_days": 15,
            "auto_cleanup_disk": True
        }
        self.reload_from_db()

    def reload_from_db(self):
        try:
            db = SessionLocal()
            try:
                settings_obj = db.query(SystemSettings).filter(SystemSettings.id == 1).first()
                if not settings_obj:
                    logger.info("Initializing default SystemSettings in DB...")
                    settings_obj = SystemSettings(
                        id=1,
                        ocr_engine_type="yolo_local",
                        lmstudio_base_url="http://localhost:1234/v1",
                        lmstudio_model_name="default",
                        lmstudio_api_key="",
                        openai_api_key="",
                        openai_model_name="gpt-4o-mini",
                        gemini_api_key="",
                        gemini_model_name="gemini-2.0-flash",
                        fcm_server_key="",
                        fcm_project_id="",
                        video_storage_path="storage/recordings",
                        video_segment_minutes=5,
                        video_retention_days=15,
                        auto_cleanup_disk=True
                    )
                    db.add(settings_obj)
                    db.commit()
                    db.refresh(settings_obj)

                with self._lock:
                    self.cache = {
                        "ocr_engine_type": settings_obj.ocr_engine_type,
                        "lmstudio_base_url": getattr(settings_obj, "lmstudio_base_url", None) or "http://localhost:1234/v1",
                        "lmstudio_model_name": getattr(settings_obj, "lmstudio_model_name", None) or "default",
                        "lmstudio_api_key": getattr(settings_obj, "lmstudio_api_key", None) or "",
                        "openai_api_key": settings_obj.openai_api_key or "",
                        "openai_model_name": settings_obj.openai_model_name or "gpt-4o-mini",
                        "gemini_api_key": settings_obj.gemini_api_key or "",
                        "gemini_model_name": settings_obj.gemini_model_name or "gemini-2.0-flash",
                        "fcm_server_key": settings_obj.fcm_server_key or "",
                        "fcm_project_id": settings_obj.fcm_project_id or "",
                        "video_storage_path": settings_obj.video_storage_path or "storage/recordings",
                        "video_segment_minutes": settings_obj.video_segment_minutes if settings_obj.video_segment_minutes is not None else 5,
                        "video_retention_days": settings_obj.video_retention_days if settings_obj.video_retention_days is not None else 15,
                        "auto_cleanup_disk": settings_obj.auto_cleanup_disk if settings_obj.auto_cleanup_disk is not None else True
                    }
                logger.info(f"System settings loaded. Active OCR Engine: [{self.cache['ocr_engine_type']}]")
            except Exception as inner_e:
                logger.warning(f"SystemSettings table not ready yet ({inner_e}). Using default settings.")
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"Database session creation deferred ({e}).")

    def get_settings(self) -> Dict:
        with self._lock:
            return self.cache.copy()

    def update_settings(self, new_settings: Dict) -> Dict:
        db = SessionLocal()
        try:
            settings_obj = db.query(SystemSettings).filter(SystemSettings.id == 1).first()
            if not settings_obj:
                settings_obj = SystemSettings(id=1)
                db.add(settings_obj)

            if "ocr_engine_type" in new_settings and new_settings["ocr_engine_type"]:
                settings_obj.ocr_engine_type = new_settings["ocr_engine_type"]
            if "lmstudio_base_url" in new_settings and new_settings["lmstudio_base_url"] is not None:
                settings_obj.lmstudio_base_url = new_settings["lmstudio_base_url"]
            if "lmstudio_model_name" in new_settings and new_settings["lmstudio_model_name"] is not None:
                settings_obj.lmstudio_model_name = new_settings["lmstudio_model_name"]
            if "lmstudio_api_key" in new_settings and new_settings["lmstudio_api_key"] is not None:
                settings_obj.lmstudio_api_key = new_settings["lmstudio_api_key"]
            if "openai_api_key" in new_settings and new_settings["openai_api_key"] is not None:
                settings_obj.openai_api_key = new_settings["openai_api_key"]
            if "openai_model_name" in new_settings and new_settings["openai_model_name"] is not None:
                settings_obj.openai_model_name = new_settings["openai_model_name"]
            if "gemini_api_key" in new_settings and new_settings["gemini_api_key"] is not None:
                settings_obj.gemini_api_key = new_settings["gemini_api_key"]
            if "gemini_model_name" in new_settings and new_settings["gemini_model_name"] is not None:
                settings_obj.gemini_model_name = new_settings["gemini_model_name"]
            if "fcm_server_key" in new_settings and new_settings["fcm_server_key"] is not None:
                settings_obj.fcm_server_key = new_settings["fcm_server_key"]
            if "fcm_project_id" in new_settings and new_settings["fcm_project_id"] is not None:
                settings_obj.fcm_project_id = new_settings["fcm_project_id"]
            if "video_storage_path" in new_settings and new_settings["video_storage_path"] is not None:
                settings_obj.video_storage_path = new_settings["video_storage_path"]
            if "video_segment_minutes" in new_settings and new_settings["video_segment_minutes"] is not None:
                settings_obj.video_segment_minutes = int(new_settings["video_segment_minutes"])
            if "video_retention_days" in new_settings and new_settings["video_retention_days"] is not None:
                settings_obj.video_retention_days = int(new_settings["video_retention_days"])
            if "auto_cleanup_disk" in new_settings and new_settings["auto_cleanup_disk"] is not None:
                settings_obj.auto_cleanup_disk = bool(new_settings["auto_cleanup_disk"])

            db.commit()
            db.refresh(settings_obj)

            with self._lock:
                self.cache = {
                    "ocr_engine_type": settings_obj.ocr_engine_type,
                    "lmstudio_base_url": getattr(settings_obj, "lmstudio_base_url", None) or "http://localhost:1234/v1",
                    "lmstudio_model_name": getattr(settings_obj, "lmstudio_model_name", None) or "default",
                    "lmstudio_api_key": getattr(settings_obj, "lmstudio_api_key", None) or "",
                    "openai_api_key": settings_obj.openai_api_key or "",
                    "openai_model_name": settings_obj.openai_model_name or "gpt-4o-mini",
                    "gemini_api_key": settings_obj.gemini_api_key or "",
                    "gemini_model_name": settings_obj.gemini_model_name or "gemini-2.0-flash",
                    "fcm_server_key": settings_obj.fcm_server_key or "",
                    "fcm_project_id": settings_obj.fcm_project_id or "",
                    "video_storage_path": settings_obj.video_storage_path or "storage/recordings",
                    "video_segment_minutes": settings_obj.video_segment_minutes if settings_obj.video_segment_minutes is not None else 5,
                    "video_retention_days": settings_obj.video_retention_days if settings_obj.video_retention_days is not None else 15,
                    "auto_cleanup_disk": settings_obj.auto_cleanup_disk if settings_obj.auto_cleanup_disk is not None else True
                }
            logger.info(f"System settings updated (Hot-reloaded). Active OCR: [{self.cache['ocr_engine_type']}], Video Path: [{self.cache['video_storage_path']}]")
            return self.cache.copy()
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to update system settings: {e}")
            raise e
        finally:
            db.close()

settings_manager = DynamicSettingsManager()
