import logging
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base
from config import settings

logger = logging.getLogger(__name__)

Base = declarative_base()

def ensure_mysql_database_exists(host, port, user, password, db_name):
    """Tự động tạo Database MySQL anpr_db nếu chưa tồn tại"""
    try:
        import pymysql
        conn = pymysql.connect(host=host, port=port, user=user, password=password)
        cursor = conn.cursor()
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
        conn.commit()
        conn.close()
        logger.info(f"MySQL database `{db_name}` verified/created successfully.")
    except Exception as e:
        logger.warning(f"Could not auto-create MySQL database `{db_name}`: {e}")

def get_engine():
    """
    Khởi tạo kết nối cơ sở dữ liệu MySQL thật:
    - Chỉ kết nối duy nhất tới máy chủ MySQL theo cấu hình chính thức (settings.DB_HOST, settings.DB_PORT, settings.DB_NAME)
    - Không tự ý thử port / mật khẩu lạ
    - Tuyệt đối không fallback âm thầm sang SQLite
    - Nếu không kết nối được, báo lỗi chi tiết và ném ngoại lệ ConnectionError
    """
    # 1. Kiểm tra / tạo database MySQL nếu chưa tồn tại
    ensure_mysql_database_exists(
        settings.DB_HOST,
        settings.DB_PORT,
        settings.DB_USER,
        settings.DB_PASSWORD,
        settings.DB_NAME
    )

    # 2. Kết nối và kiểm tra tính sẵn sàng của MySQL
    db_url = f"mysql+pymysql://{settings.DB_USER}:{settings.DB_PASSWORD}@{settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}?charset=utf8mb4"
    try:
        engine = create_engine(
            db_url,
            pool_size=25,
            max_overflow=50,
            pool_timeout=15,
            pool_recycle=1800,
            pool_pre_ping=True,
            echo=False
        )
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info(f"Kết nối thành công tới CSDL MySQL thực tế [{settings.DB_NAME}] tại {settings.DB_HOST}:{settings.DB_PORT}")
        return engine
    except Exception as e:
        error_msg = (
            f"\n{'='*75}\n"
            f"❌ [LỖI KẾT NỐI CƠ SỞ DỮ LIỆU MYSQL THỰC TẾ]\n"
            f"Hệ thống chỉ sử dụng dữ liệu thật từ MySQL nhưng KHÔNG THỂ KẾT NỐI tới máy chủ MySQL!\n"
            f"  - Host: {settings.DB_HOST}\n"
            f"  - Port: {settings.DB_PORT}\n"
            f"  - Database: {settings.DB_NAME}\n"
            f"  - User: {settings.DB_USER}\n"
            f"  - Chi tiết lỗi: {e}\n"
            f"=> Vui lòng bật dịch vụ MySQL (XAMPP / MySQL Service) trên máy chủ và kiểm tra lại cấu hình tại config.py / .env!\n"
            f"{'='*75}\n"
        )
        logger.critical(error_msg)
        raise ConnectionError(error_msg) from e

engine = get_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    import models  # Ensure all models are registered
    Base.metadata.create_all(bind=engine)
    
    # Auto-migration columns check
    try:
        with engine.connect() as conn:
            # 1. Bảng cameras: detection_zone, zone_code, zone_name, camera_function, action_group_id, is_recording, action_group_front_id, action_group_rear_id, camera_code
            for col, col_type in [
                ("camera_code", "VARCHAR(100) NULL"),
                ("detection_zone", "TEXT NULL"),
                ("zone_code", "VARCHAR(50) DEFAULT 'TRUONGLAI'"),
                ("zone_name", "VARCHAR(100) DEFAULT 'Trường Lái'"),
                ("camera_function", "VARCHAR(50) DEFAULT 'ANPR'"),
                ("action_group_id", "INT NULL"),
                ("action_group_front_id", "INT NULL"),
                ("action_group_rear_id", "INT NULL"),
                ("traffic_direction", "VARCHAR(50) DEFAULT 'TWO_WAY'"),
                ("webhook_events", "VARCHAR(100) DEFAULT 'CREATE,UPDATE'"),
                ("is_recording", "BOOLEAN DEFAULT FALSE"),
                ("display_order", "INT DEFAULT 0")
            ]:
                try:
                    conn.execute(text(f"ALTER TABLE cameras ADD COLUMN {col} {col_type};"))
                    conn.commit()
                    logger.info(f"Added {col} column to cameras table.")
                except Exception:
                    pass

            try:
                conn.execute(text("CREATE INDEX idx_cameras_camera_code ON cameras (camera_code);"))
                conn.commit()
            except Exception:
                pass

            # Migrate dữ liệu cũ: nếu action_group_front_id chưa có thì sao chép từ action_group_id
            try:
                conn.execute(text("UPDATE cameras SET action_group_front_id = action_group_id WHERE action_group_front_id IS NULL AND action_group_id IS NOT NULL;"))
                conn.execute(text("UPDATE cameras SET action_group_rear_id = action_group_id WHERE action_group_rear_id IS NULL AND action_group_id IS NOT NULL;"))
                conn.execute(text("UPDATE cameras SET display_order = id WHERE display_order IS NULL OR display_order = 0;"))
                conn.commit()
            except Exception:
                pass

            # 2. Bảng vehicle_logs: summary, zone_code, action_group_id, action_group_name, action_status, action_result_raw, alert_level, vehicle_type, vehicle_classification, is_edited, original_plate_number, original_vehicle_view, updated_by, updated_at, is_deleted, deleted_at, deleted_by
            for col, col_type in [
                ("summary", "VARCHAR(250) NULL"),
                ("zone_code", "VARCHAR(50) NULL"),
                ("action_group_id", "INT NULL"),
                ("action_group_name", "VARCHAR(255) NULL"),
                ("action_status", "VARCHAR(50) NULL"),
                ("vehicle_classification", "VARCHAR(50) NULL"),
                ("action_result_raw", "TEXT NULL"),
                ("alert_level", "VARCHAR(20) DEFAULT 'normal'"),
                ("vehicle_type", "VARCHAR(20) DEFAULT 'car'"),
                ("is_edited", "BOOLEAN DEFAULT FALSE"),
                ("original_plate_number", "VARCHAR(50) NULL"),
                ("original_vehicle_view", "VARCHAR(20) NULL"),
                ("updated_by", "VARCHAR(100) NULL"),
                ("updated_at", "DATETIME NULL"),
                ("is_deleted", "BOOLEAN DEFAULT FALSE"),
                ("deleted_at", "DATETIME NULL"),
                ("deleted_by", "VARCHAR(100) NULL"),
                ("sync_status", "VARCHAR(30) DEFAULT 'SYNCED'"),
                ("last_sync_at", "DATETIME NULL"),
                ("sync_error", "TEXT NULL"),
                ("video_path", "VARCHAR(500) NULL")
            ]:
                try:
                    conn.execute(text(f"ALTER TABLE vehicle_logs ADD COLUMN {col} {col_type};"))
                    conn.commit()
                    logger.info(f"Added {col} column to vehicle_logs table.")
                except Exception:
                    pass

            # Tạo Index cho is_deleted và sync_status trong vehicle_logs
            try:
                conn.execute(text("CREATE INDEX idx_vehicle_logs_is_deleted ON vehicle_logs (is_deleted);"))
                conn.commit()
            except Exception:
                pass
            try:
                conn.execute(text("CREATE INDEX idx_vehicle_logs_sync_status ON vehicle_logs (sync_status);"))
                conn.commit()
            except Exception:
                pass

            # 3. Bảng action_groups: apply_car, apply_motorcycle, push_condition, auth_token, auth_type
            for col, col_type in [
                ("apply_car", "BOOLEAN DEFAULT TRUE"),
                ("apply_motorcycle", "BOOLEAN DEFAULT TRUE"),
                ("push_condition", "VARCHAR(50) DEFAULT 'DEFAULT'"),
                ("auth_token", "VARCHAR(500) NULL"),
                ("auth_type", "VARCHAR(50) DEFAULT 'BEARER'")
            ]:
                try:
                    conn.execute(text(f"ALTER TABLE action_groups ADD COLUMN {col} {col_type};"))
                    conn.commit()
                    logger.info(f"Added {col} column to action_groups table.")
                except Exception:
                    pass

            # 4. Bảng system_settings: lmstudio_base_url, lmstudio_model_name, lmstudio_api_key, fcm_server_key, fcm_project_id, video_storage_path, video_segment_minutes, video_retention_days, auto_cleanup_disk, event video clip
            for col, col_type in [
                ("lmstudio_base_url", "VARCHAR(500) DEFAULT 'http://localhost:1234/v1'"),
                ("lmstudio_model_name", "VARCHAR(100) DEFAULT 'default'"),
                ("lmstudio_api_key", "VARCHAR(500) NULL"),
                ("fcm_server_key", "VARCHAR(500) NULL"),
                ("fcm_project_id", "VARCHAR(100) NULL"),
                ("video_storage_path", "VARCHAR(500) DEFAULT 'storage/recordings'"),
                ("video_segment_minutes", "INT DEFAULT 5"),
                ("video_retention_days", "INT DEFAULT 15"),
                ("auto_cleanup_disk", "BOOLEAN DEFAULT TRUE"),
                ("enable_event_video_clip", "BOOLEAN DEFAULT TRUE"),
                ("event_video_pre_seconds", "INT DEFAULT 5"),
                ("event_video_post_seconds", "INT DEFAULT 5"),
                ("vehicle_lookup_api_url", "VARCHAR(500) DEFAULT 'http://localhost:9999/api2/take-plate-by-number'"),
                ("vehicle_lookup_api_token", "VARCHAR(255) DEFAULT 'truonglai_camera_secure_token_2026'")
            ]:
                try:
                    conn.execute(text(f"ALTER TABLE system_settings ADD COLUMN {col} {col_type};"))
                    conn.commit()
                    logger.info(f"Added {col} column to system_settings table.")
                except Exception:
                    pass

    except Exception as e:
        logger.warning(f"Database column migration note: {e}")

    # MySQL utf8mb4 conversion
    if engine.dialect.name == "mysql":
        try:
            with engine.connect() as conn:
                conn.execute(text(f"ALTER DATABASE `{settings.DB_NAME}` CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci;"))
                for table_name in ["cameras", "vehicle_logs", "system_settings", "users", "action_groups", "user_camera_permissions", "device_tokens", "video_recordings", "video_export_jobs", "webhook_sync_queue"]:
                    try:
                        conn.execute(text(f"ALTER TABLE `{table_name}` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"))
                    except Exception:
                        pass
                conn.commit()
                logger.info("Successfully verified utf8mb4_unicode_ci encoding for MySQL tables.")

                # Tự động dọn dẹp các bản ghi video zombie (status 'recording' cũ hơn 30 phút do khởi động lại đột ngột)
                try:
                    conn.execute(text("""
                        UPDATE video_recordings 
                        SET status = 'completed', 
                            end_time = IFNULL(end_time, DATE_ADD(start_time, INTERVAL IFNULL(duration_seconds, 300) SECOND))
                        WHERE status = 'recording' AND start_time < DATE_SUB(NOW(), INTERVAL 30 MINUTE);
                    """))
                    conn.commit()
                except Exception as clean_err:
                    logger.warning(f"Note on cleaning zombie recordings: {clean_err}")
        except Exception as e:
            logger.warning(f"Could not convert MySQL tables to utf8mb4: {e}")

    # Toàn bộ dữ liệu Action Groups và Users được quản lý thực tế từ CSDL MySQL
