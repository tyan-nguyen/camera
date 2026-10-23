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
    # 1. Ưu tiên sử dụng SQLite nếu cấu hình USE_SQLITE = True
    if getattr(settings, "USE_SQLITE", False):
        logger.info(f"Using SQLite database: {settings.SQLITE_URL}")
        engine = create_engine(
            settings.SQLITE_URL,
            connect_args={"check_same_thread": False},
            echo=False
        )
        return engine

    # 2. Các port MySQL cần thử (ưu tiên settings.DB_PORT, dự phòng 3306 / 3308)
    ports_to_try = [settings.DB_PORT]
    if settings.DB_PORT == 3306:
        ports_to_try.append(3308)
    elif settings.DB_PORT == 3308:
        ports_to_try.append(3306)

    for port in ports_to_try:
        ensure_mysql_database_exists(settings.DB_HOST, port, settings.DB_USER, settings.DB_PASSWORD, settings.DB_NAME)

    # 3. Thử kết nối với Mật khẩu cấu hình và Port khả dụng
    passwords_to_try = [settings.DB_PASSWORD, "", "root"]
    for port in ports_to_try:
        for pwd in passwords_to_try:
            db_url = f"mysql+pymysql://{settings.DB_USER}:{pwd}@{settings.DB_HOST}:{port}/{settings.DB_NAME}?charset=utf8mb4"
            try:
                engine = create_engine(
                    db_url,
                    pool_size=25,
                    max_overflow=50,
                    pool_timeout=30,
                    pool_recycle=1800,
                    pool_pre_ping=True,
                    echo=False
                )
                with engine.connect() as conn:
                    pass
                logger.info(f"Connected successfully to MySQL v8 database [{settings.DB_NAME}] on {settings.DB_HOST}:{port}")
                return engine
            except Exception:
                continue

    if settings.USE_SQLITE_FALLBACK:
        logger.warning("Could not connect to MySQL v8 with any password. Falling back to SQLite local database.")
        engine = create_engine(
            settings.SQLITE_URL,
            connect_args={"check_same_thread": False},
            echo=False
        )
        return engine
    raise Exception("Failed to connect to MySQL database.")

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
                ("is_recording", "BOOLEAN DEFAULT FALSE")
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
                ("event_video_post_seconds", "INT DEFAULT 5")
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

    # Seed Default Action Groups
    try:
        db = SessionLocal()
        from models import ActionGroup
        ag_count = db.query(ActionGroup).count()
        if ag_count == 0:
            logger.info("Seeding default Action Groups (Kiểm tra kế hoạch, lịch thi, thuê xe, lấy đơn)...")
            default_ags = [
                ActionGroup(
                    name="Kiểm tra đi không kế hoạch",
                    code="KIEM_TRA_DI_KHONG_KE_HOACH",
                    api_url="https://qltl.nguyentrinh.com.vn/api2/kiem-tra-di-khong-ke-hoach?plate={plate}",
                    http_method="GET",
                    description="Kiểm tra xe ra vào ngoài lịch trình kế hoạch của Trường lái",
                    is_active=True
                ),
                ActionGroup(
                    name="Kiểm tra đăng ký lịch học/thi",
                    code="KIEM_TRA_DANG_KY_LICH",
                    api_url="https://qltl.nguyentrinh.com.vn/api2/kiem-tra-lich-thi",
                    http_method="POST",
                    description="Kiểm tra danh sách học viên và phương tiện đăng ký sát hạch",
                    is_active=True
                ),
                ActionGroup(
                    name="Kiểm tra thuê xe tập lái",
                    code="KIEM_TRA_THUE_XE",
                    api_url="https://qltl.nguyentrinh.com.vn/api2/kiem-tra-thue-xe",
                    http_method="POST",
                    description="Xác thực hợp đồng thuê xe tập lái sân sa hình",
                    is_active=True
                ),
                ActionGroup(
                    name="Kiểm tra lấy đơn / xuất nhập kho bãi",
                    code="KIEM_TRA_LAY_DON",
                    api_url="https://qltl.nguyentrinh.com.vn/api2/kiem-tra-lay-don",
                    http_method="POST",
                    description="Kiểm tra phiếu điều xe lấy hàng tại Kho bãi",
                    is_active=True
                )
            ]
            db.add_all(default_ags)
            db.commit()
            logger.info("Default Action Groups seeded successfully.")
        db.close()
    except Exception as e:
        logger.warning(f"Could not seed Action Groups: {e}")

    # Seed Default Users
    try:
        db = SessionLocal()
        from models import User, UserRoleEnum
        from auth_utils import hash_password
        user_count = db.query(User).count()
        if user_count == 0:
            logger.info("Seeding default user accounts (admin, quanly, user)...")
            default_users = [
                User(
                    username="admin",
                    full_name="Quản Trị Viên (Admin)",
                    hashed_password=hash_password("admin123"),
                    role=UserRoleEnum.ADMIN.value,
                    is_active=True
                ),
                User(
                    username="quanly",
                    full_name="Quản Lý Hệ Thống",
                    hashed_password=hash_password("quanly123"),
                    role=UserRoleEnum.QUAN_LY.value,
                    is_active=True
                ),
                User(
                    username="user",
                    full_name="Giám Sát Viên (User)",
                    hashed_password=hash_password("user123"),
                    role=UserRoleEnum.USER.value,
                    is_active=True
                )
            ]
            db.add_all(default_users)
            db.commit()
            logger.info("Default user accounts created: admin/admin123, quanly/quanly123, user/user123.")
        db.close()
    except Exception as e:
        logger.warning(f"Could not seed default users: {e}")
