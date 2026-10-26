import os
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Triệt tiêu toàn bộ log cảnh báo/lỗi rác của FFmpeg và OpenCV trên Command Prompt (AV_LOG_QUIET)
os.environ["OPENCV_FFMPEG_LOGLEVEL"] = "-8"
os.environ["OPENCV_LOG_LEVEL"] = "ERROR"
os.environ["AV_LOG_FORCE_NOCOLOR"] = "1"

# Cấu hình FFmpeg RTSP qua TCP, bộ đệm jitter buffer 2048 gói để triệt tiêu lỗi bad cseq khi mạng chập chờn
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|buffer_size;2048000|max_delay;500000|reorder_queue_size;2048|stimeout;5000000|timeout;5000000"

class Settings:
    APP_NAME: str = "ANPR 5-Camera Monitoring System"
    DEBUG: bool = True

    # Database Settings (Chuyển sang MySQL v8 anpr_db)
    USE_SQLITE: bool = os.getenv("USE_SQLITE", "False").lower() in ("true", "1", "yes")
    SQLITE_URL: str = "sqlite:///./anpr_local.db"
    USE_SQLITE_FALLBACK: bool = True

    # MySQL v8 Settings
    DB_HOST: str = os.getenv("DB_HOST", "127.0.0.1")
    DB_PORT: int = int(os.getenv("DB_PORT", 3306))
    DB_USER: str = os.getenv("DB_USER", "root")
    DB_PASSWORD: str = os.getenv("DB_PASSWORD", "")
    DB_NAME: str = os.getenv("DB_NAME", "anpr_db")

    @property
    def DATABASE_URL(self) -> str:
        return f"mysql+pymysql://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}?charset=utf8mb4"

    # Storage Paths
    BASE_DIR: str = os.path.dirname(os.path.abspath(__file__))
    STORAGE_DIR: str = os.path.join(BASE_DIR, "storage")
    CAPTURES_DIR: str = os.path.join(STORAGE_DIR, "captures")
    RECORDINGS_DIR: str = os.path.join(STORAGE_DIR, "recordings")
    EXPORTS_DIR: str = os.path.join(STORAGE_DIR, "exports")

    # AI Pipeline Settings
    YOLO_MODEL_PATH: str = os.path.join(BASE_DIR, "weights", "yolov8n_multitask.pt")
    TARGET_VRAM_MB: int = 3500
    WEBP_QUALITY: int = 85

    # Event Video Clip Settings (-5s to +5s)
    ENABLE_EVENT_VIDEO_CLIP: bool = True
    EVENT_VIDEO_PRE_SECONDS: int = 5
    EVENT_VIDEO_POST_SECONDS: int = 5

settings = Settings()

os.makedirs(settings.CAPTURES_DIR, exist_ok=True)
os.makedirs(settings.RECORDINGS_DIR, exist_ok=True)
os.makedirs(settings.EXPORTS_DIR, exist_ok=True)
