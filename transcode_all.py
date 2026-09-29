import os
import sys
import subprocess
import logging
from config import settings
from settings_manager import settings_manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Transcoder")

def get_storage_dir():
    sys_cfg = settings_manager.get_settings()
    custom_path = sys_cfg.get("video_storage_path", "storage/recordings").strip()
    if os.path.isabs(custom_path):
        base_dir = custom_path
    else:
        base_dir = os.path.join(settings.BASE_DIR, custom_path)
    return base_dir

def main():
    logger.info("=== BẮT ĐẦU QUÉT VÀ CHUYỂN ĐỔI TOÀN BỘ VIDEO SANG CHUẨN H.264 FASTSTART ===")
    
    # 1. Kiểm tra FFmpeg binary
    try:
        import imageio_ffmpeg
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        logger.info(f"Đã tìm thấy FFmpeg binary: {ffmpeg_exe}")
    except Exception:
        ffmpeg_exe = "ffmpeg"
        logger.info("Dùng lệnh 'ffmpeg' hệ thống.")

    # 2. Tìm thư mục lưu trữ video
    recordings_dir = get_storage_dir()
    logger.info(f"Thư mục quét video: {recordings_dir}")
    if not os.path.exists(recordings_dir):
        logger.error("Thư mục lưu trữ video không tồn tại!")
        return

    # 3. Quét toàn bộ file .mp4
    files_to_convert = []
    for root, dirs, files in os.walk(recordings_dir):
        for f in files:
            if f.endswith('.mp4') and not f.endswith('_h264tmp.mp4'):
                full_path = os.path.join(root, f)
                files_to_convert.append(full_path)

    logger.info(f"Tìm thấy tổng cộng {len(files_to_convert)} file video MP4.")
    success_count = 0
    skipped_count = 0

    import cv2
    for idx, file_path in enumerate(files_to_convert, 1):
        try:
            # Kiểm tra codec hiện tại
            cap = cv2.VideoCapture(file_path)
            fourcc_int = int(cap.get(cv2.CAP_PROP_FOURCC)) if cap.isOpened() else 0
            fourcc_str = ''.join([chr((fourcc_int >> 8 * i) & 0xFF) for i in range(4)]).lower()
            cap.release()

            if fourcc_str in ['avc1', 'h264']:
                logger.info(f"[{idx}/{len(files_to_convert)}] Đã chuẩn H.264 (Bỏ qua): {os.path.basename(file_path)}")
                skipped_count += 1
                continue

            logger.info(f"[{idx}/{len(files_to_convert)}] Đang chuyển đổi {os.path.basename(file_path)} (Codec cũ: {fourcc_str or 'Unknown'})...")
            temp_path = file_path.replace(".mp4", "_h264tmp.mp4")
            cmd = [
                ffmpeg_exe, "-y",
                "-i", file_path,
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                temp_path
            ]
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if res.returncode == 0 and os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
                os.replace(temp_path, file_path)
                logger.info(f" -> Thành công! File mới: {os.path.getsize(file_path)} bytes")
                success_count += 1
            else:
                if os.path.exists(temp_path):
                    try: os.remove(temp_path)
                    except Exception: pass
                logger.warning(f" -> Chuyển đổi thất bại cho {file_path}")
        except Exception as e:
            logger.error(f"Lỗi khi xử lý {file_path}: {e}")

    logger.info(f"\n=== HOÀN TẤT: Đã chuyển đổi {success_count} file, Bỏ qua {skipped_count} file đã chuẩn H.264 ===")

if __name__ == "__main__":
    main()
