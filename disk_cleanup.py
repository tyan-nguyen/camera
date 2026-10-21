import os
import shutil
import time
import logging
import threading
from datetime import datetime, timedelta
from config import settings
from database import SessionLocal
from models import VideoRecording, VideoExportJob
from settings_manager import settings_manager

logger = logging.getLogger(__name__)


def get_recordings_base_dir() -> str:
    """Lấy đường dẫn thư mục lưu trữ video theo cấu hình hệ thống (hỗ trợ ổ đĩa tùy biến D:/...)"""
    sys_cfg = settings_manager.get_settings()
    custom_path = sys_cfg.get("video_storage_path", "storage/recordings").strip()
    if os.path.isabs(custom_path):
        base_dir = custom_path
    else:
        base_dir = os.path.join(settings.BASE_DIR, custom_path)
    os.makedirs(base_dir, exist_ok=True)
    return base_dir


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

    if purged_count > 0:
        logger.info(f"Disk Auto-cleanup (captures) completed. Total directories purged: {purged_count}")


def purge_old_recordings(retention_days: int = 15):
    """
    Quét và xóa các thư mục video recording YYYY-MM-DD cũ hơn số ngày retention_days quy định
    và dọn dẹp các bản ghi CSDL tương ứng.
    """
    recordings_dir = get_recordings_base_dir()
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
        if purged_folders > 0 or deleted_rows > 0:
            logger.info(f"Video Auto-cleanup completed. Purged {purged_folders} directories, deleted {deleted_rows} DB records.")
    except Exception as e:
        logger.error(f"Error purging old VideoRecording DB rows: {e}")
        db.rollback()
    finally:
        db.close()


def check_and_emergency_cleanup_disk(threshold_percent: float = 90.0, min_free_gb: float = 10.0):
    """
    Kiểm tra dung lượng ổ đĩa chứa thư mục video. Nếu tỷ lệ đã dùng >= threshold_percent
    hoặc dung lượng trống < min_free_gb, tự động xóa các ngày video cũ nhất theo FIFO
    để giải phóng ổ đĩa khẩn cấp, chống sập hệ thống.
    """
    sys_cfg = settings_manager.get_settings()
    auto_cleanup = sys_cfg.get("auto_cleanup_disk", True)
    if not auto_cleanup:
        return

    recordings_dir = get_recordings_base_dir()
    if not os.path.exists(recordings_dir):
        return

    try:
        total, used, free = shutil.disk_usage(recordings_dir)
        used_percent = (used / total) * 100
        free_gb = free / (1024 ** 3)

        if used_percent < threshold_percent and free_gb >= min_free_gb:
            return

        logger.warning(
            f"Disk space critical: Used {used_percent:.1f}%, Free {free_gb:.1f} GB "
            f"(Threshold: {threshold_percent}%, Min free: {min_free_gb} GB). Running emergency FIFO video purge..."
        )

        # Thu thập danh sách toàn bộ các ngày có video trên mọi camera
        date_folders = []
        for cam_folder in os.listdir(recordings_dir):
            cam_path = os.path.join(recordings_dir, cam_folder)
            if not os.path.isdir(cam_path):
                continue

            for date_str in os.listdir(cam_path):
                date_path = os.path.join(cam_path, date_str)
                if not os.path.isdir(date_path):
                    continue
                try:
                    dt = datetime.strptime(date_str, "%Y-%m-%d")
                    date_folders.append((dt, date_str, date_path))
                except ValueError:
                    continue

        # Sắp xếp theo ngày tăng dần (ngày cũ nhất xóa trước)
        date_folders.sort(key=lambda x: x[0])

        db = SessionLocal()
        purged_count = 0
        try:
            for dt, date_str, date_path in date_folders:
                total, used, free = shutil.disk_usage(recordings_dir)
                used_percent = (used / total) * 100
                free_gb = free / (1024 ** 3)
                if used_percent < (threshold_percent - 5.0) and free_gb >= (min_free_gb + 5.0):
                    break

                shutil.rmtree(date_path, ignore_errors=True)
                purged_count += 1
                logger.info(f"Emergency purged oldest video folder: {date_path}")

                day_start = datetime.combine(dt.date(), datetime.min.time())
                day_end = datetime.combine(dt.date(), datetime.max.time())
                db.query(VideoRecording).filter(
                    VideoRecording.start_time >= day_start,
                    VideoRecording.start_time <= day_end
                ).delete()
                db.commit()
        except Exception as e:
            logger.error(f"Error during emergency disk cleanup: {e}")
            db.rollback()
        finally:
            db.close()

        if purged_count > 0:
            logger.info(f"Emergency disk cleanup completed. Purged {purged_count} folder(s). New free: {free_gb:.1f} GB.")
    except Exception as e:
        logger.error(f"Failed to check disk usage: {e}")


def run_all_cleanup():
    """Chạy toàn bộ các quy trình dọn dẹp đĩa (retention days + emergency disk check)"""
    sys_cfg = settings_manager.get_settings()
    retention_days = sys_cfg.get("video_retention_days", 15)
    try:
        retention_days = int(retention_days)
    except Exception:
        retention_days = 15

    purge_old_captures(retention_days=30)
    purge_old_recordings(retention_days=retention_days)
    check_and_emergency_cleanup_disk()


class DiskCleanupScheduler:
    """Worker chạy ngầm định kỳ kiểm tra và dọn dẹp đĩa tự động"""
    def __init__(self, interval_seconds: int = 3600):
        self.interval_seconds = interval_seconds
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None

    def start(self):
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run_loop, daemon=True, name="DiskCleanupWorker")
        self.thread.start()
        logger.info("Disk Auto-cleanup scheduler started (1h cycle).")

    def stop(self):
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)
        logger.info("Disk Auto-cleanup scheduler stopped.")

    def _run_loop(self):
        # Chạy 1 lần sau 30 giây khi khởi động server
        if not self.stop_event.wait(timeout=30):
            try:
                run_all_cleanup()
            except Exception as e:
                logger.error(f"Initial disk cleanup note: {e}")

        while not self.stop_event.is_set():
            if self.stop_event.wait(timeout=self.interval_seconds):
                break
            try:
                run_all_cleanup()
            except Exception as e:
                logger.error(f"Periodic disk cleanup note: {e}")


disk_cleanup_scheduler = DiskCleanupScheduler(interval_seconds=3600)

if __name__ == "__main__":
    run_all_cleanup()
