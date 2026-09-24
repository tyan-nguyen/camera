import os
import shutil
import time
import logging
from datetime import datetime, timedelta
from config import settings

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
                        shutil.rmtree(day_path)
                        purged_count += 1
                        logger.info(f"Purged expired capture directory: {day_path}")
                except ValueError:
                    continue

    logger.info(f"Disk Auto-cleanup task completed. Total directories purged: {purged_count}")

if __name__ == "__main__":
    purge_old_captures(retention_days=30)
