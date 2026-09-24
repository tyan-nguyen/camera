import os

# Cấu hình FFmpeg timeout 10 giây (10.000.000 microsecond) để hỗ trợ camera IP/RTSP qua mạng WAN/Internet và codec H.265
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|stimeout;10000000|timeout;10000000"

class Settings:
    APP_NAME: str = "ANPR 5-Camera Monitoring System"
    DEBUG: bool = True

    # Database Settings (Chuyển sang MySQL v8 anpr_db)
    USE_SQLITE: bool = False
    SQLITE_URL: str = "sqlite:///./anpr_local.db"
    USE_SQLITE_FALLBACK: bool = True

    # MySQL v8 Settings
    DB_HOST: str = os.getenv("DB_HOST", "localhost")
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

    # AI Pipeline Settings
    YOLO_MODEL_PATH: str = os.path.join(BASE_DIR, "weights", "yolov8n_multitask.pt")
    TARGET_VRAM_MB: int = 3500
    WEBP_QUALITY: int = 85

settings = Settings()

os.makedirs(settings.CAPTURES_DIR, exist_ok=True)
