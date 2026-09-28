import os
import shutil
import time
import logging
from datetime import datetime, timedelta
from config import settings
from database import SessionLocal
from models import VideoRecording, VideoExportJob
from settings_manager import settings_manager

logger = logging.getLogger(__name__)

def purge_old_captures(retention_days: int = 30):
    """
    Quét và xóa các thư mục lưu ảnh YYYY/MM/DD cũ hơn số ngày retention_days quy định
    """
    captures_dir = settings.CAPTURES_DIR
    if not os.path.exists(captures_dir):
        return

    cutoff_date = datetime.now() - timedelta(days=retention_days)
    logger.info(f"Running Disk Auto-cleanup scan. Purging captures older than {cutoff_date.strftime('%Y-%m-%d')} ({retention_days} days retention)...")

    purged_count = 0
    # Cấu trúc thư mục: storage/captures/YYYY/MM/DD/
    for year_str in os.listdir(captures_dir):
        year_path = os.path.join(captures_dir, year_str)
        if not os.path.isdir(year_path) or not year_str.isdigit():
            continue
        
        for month_str in os.listdir(year_path):
            month_path = os.path.join(year_path, month_str)
            if not os.path.isdir(month_path) or not month_str.isdigit():
                continue

            for day_str in os.listdir(month_path):
                day_path = os.path.join(month_path, day_str)
                if not os.path.isdir(day_path) or not day_str.isdigit():
                    continue

                try:
                    folder_date = datetime(int(year_str), int(month_str), int(day_str))
                    if folder_date < cutoff_date:
                        shutil.rmtree(day_path, ignore_errors=True)
                        purged_count += 1
                        logger.info(f"Purged expired capture directory: {day_path}")
                except ValueError:
                    continue

    logger.info(f"Disk Auto-cleanup (captures) completed. Total directories purged: {purged_count}")

def purge_old_recordings(retention_days: int = 15):
    """
    Quét và xóa các thư mục video recording YYYY-MM-DD cũ hơn số ngày retention_days quy định
    và dọn dẹp các bản ghi CSDL tương ứng.
    """
    sys_cfg = settings_manager.get_settings()
    custom_path = sys_cfg.get("video_storage_path", "storage/recordings").strip()
    if os.path.isabs(custom_path):
        recordings_dir = custom_path
    else:
        recordings_dir = os.path.join(settings.BASE_DIR, custom_path)

    if not os.path.exists(recordings_dir):
        return

    cutoff_date = datetime.now() - timedelta(days=retention_days)
    logger.info(f"Running Video Auto-cleanup scan. Purging recordings older than {cutoff_date.strftime('%Y-%m-%d')} ({retention_days} days retention)...")

    purged_folders = 0
    # Cấu trúc thư mục: storage/recordings/camera_{id}/YYYY-MM-DD/
    for cam_folder in os.listdir(recordings_dir):
        cam_path = os.path.join(recordings_dir, cam_folder)
        if not os.path.isdir(cam_path):
            continue

        for date_str in os.listdir(cam_path):
            date_path = os.path.join(cam_path, date_str)
            if not os.path.isdir(date_path):
                continue

            try:
                folder_date = datetime.strptime(date_str, "%Y-%m-%d")
                if folder_date < cutoff_date:
                    shutil.rmtree(date_path, ignore_errors=True)
                    purged_folders += 1
                    logger.info(f"Purged expired video recording directory: {date_path}")
            except ValueError:
                continue

    # Xóa các bản ghi CSDL cũ
    db = SessionLocal()
    try:
        deleted_rows = db.query(VideoRecording).filter(VideoRecording.start_time < cutoff_date).delete()
        db.commit()
        logger.info(f"Video Auto-cleanup completed. Purged {purged_folders} directories, deleted {deleted_rows} DB records.")
    except Exception as e:
        logger.error(f"Error purging old VideoRecording DB rows: {e}")
        db.rollback()
    finally:
        db.close()

def run_all_cleanup():
    sys_cfg = settings_manager.get_settings()
    retention_days = sys_cfg.get("video_retention_days", 15)
    purge_old_captures(retention_days=30)
    purge_old_recordings(retention_days=retention_days)

if __name__ == "__main__":
    run_all_cleanup()
