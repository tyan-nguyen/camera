import os
import time
import queue
import threading
import logging
import cv2
import numpy as np
import subprocess
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple, Union

from config import settings
from database import SessionLocal
from models import VideoRecording, Camera
from settings_manager import settings_manager

logger = logging.getLogger(__name__)

# Bộ nhớ các file đã xác thực chuẩn H.264
_h264_ready_files = set()


def get_storage_base_dir() -> str:
    """Lấy đường dẫn thư mục lưu trữ video theo cấu hình hệ thống (hỗ trợ ổ đĩa tùy biến D:/...)"""
    sys_cfg = settings_manager.get_settings()
    custom_path = sys_cfg.get("video_storage_path", "storage/recordings").strip()
    if os.path.isabs(custom_path):
        base_dir = custom_path
    else:
        base_dir = os.path.join(settings.BASE_DIR, custom_path)
    os.makedirs(base_dir, exist_ok=True)
    return base_dir


def _async_convert_to_h264_faststart(file_path: str, recording_id: Optional[int], camera_name: str, target_fps: int = 6):
    """Fallback: Chuyển đổi ngầm sang định dạng H.264 AVC (yuv420p + faststart) khi phải dùng codec cũ"""
    def _worker():
        if not file_path or not os.path.exists(file_path):
            return
        try:
            import imageio_ffmpeg
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            ffmpeg_exe = "ffmpeg"

        gop_size = max(6, int(target_fps * 2))
        temp_path = f"{file_path}.h264tmp_{os.getpid()}_{threading.get_ident()}.mp4"
        cmd = [
            ffmpeg_exe, "-y",
            "-i", file_path,
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "23",
            "-g", str(gop_size),
            "-keyint_min", str(max(2, int(gop_size / 2))),
            "-sc_threshold", "0",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            temp_path
        ]
        creationflags = 0
        if os.name == 'nt' and hasattr(subprocess, 'CREATE_NO_WINDOW'):
            creationflags = subprocess.CREATE_NO_WINDOW

        try:
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags)
            if res.returncode == 0 and os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
                os.replace(temp_path, file_path)
                new_size_mb = round(os.path.getsize(file_path) / (1024 * 1024), 2)
                _h264_ready_files.add(file_path)
                if recording_id:
                    db = SessionLocal()
                    try:
                        rec = db.query(VideoRecording).filter(VideoRecording.id == recording_id).first()
                        if rec:
                            rec.file_size_mb = new_size_mb
                            db.commit()
                    except Exception:
                        db.rollback()
                    finally:
                        db.close()
                logger.info(f"[{camera_name}] Optimized fallback video segment to H.264: {file_path}")
            else:
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"[{camera_name}] Fallback H.264 conversion note: {e}")
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass

    threading.Thread(target=_worker, daemon=True, name=f"H264Transcode-{os.path.basename(file_path)}").start()


class FFmpegPipeVideoWriter:
    """
    Trình ghi video chuẩn H.264 (yuv420p + faststart) trực tiếp bằng FFmpeg qua stdin pipe.
    Loại bỏ hoàn toàn bước transcode ngầm, video sẵn sàng phát ngay 0 giây chờ đợi.
    """
    def __init__(self, file_path: str, width: int, height: int, fps: float):
        self.file_path = file_path
        self.width = width
        self.height = height
        self.fps = max(1.0, float(fps))
        self.process: Optional[subprocess.Popen] = None
        self.is_opened = False
        self._start_ffmpeg()

    def _start_ffmpeg(self):
        try:
            import imageio_ffmpeg
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            ffmpeg_exe = "ffmpeg"

        gop_size = max(6, int(self.fps * 2))  # Keyframe mỗi 2 giây để tua tức thì
        cmd = [
            ffmpeg_exe, "-y",
            "-f", "rawvideo",
            "-vcodec", "rawvideo",
            "-s", f"{self.width}x{self.height}",
            "-pix_fmt", "bgr24",
            "-r", str(self.fps),
            "-i", "-",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "23",
            "-g", str(gop_size),
            "-keyint_min", str(max(2, int(gop_size / 2))),
            "-sc_threshold", "0",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            self.file_path
        ]

        creationflags = 0
        if os.name == 'nt' and hasattr(subprocess, 'CREATE_NO_WINDOW'):
            creationflags = subprocess.CREATE_NO_WINDOW

        try:
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags
            )
            self.is_opened = True
        except Exception as e:
            logger.error(f"Failed to start FFmpeg pipe writer for {self.file_path}: {e}")
            self.process = None
            self.is_opened = False

    def isOpened(self) -> bool:
        return self.is_opened and self.process is not None and self.process.poll() is None

    def write(self, frame: np.ndarray):
        if not self.isOpened():
            return
        try:
            self.process.stdin.write(frame.tobytes())
        except (BrokenPipeError, OSError) as e:
            logger.warning(f"FFmpeg pipe broken for {self.file_path}: {e}")
            self.is_opened = False

    def release(self):
        if self.process is not None:
            try:
                if self.process.stdin:
                    self.process.stdin.close()
                self.process.wait(timeout=4.0)
            except subprocess.TimeoutExpired:
                logger.warning(f"FFmpeg process timeout on release, terminating: {self.file_path}")
                self.process.kill()
            except Exception as e:
                logger.warning(f"Error releasing FFmpeg writer: {e}")
            finally:
                self.process = None
                self.is_opened = False


class CameraVideoRecorder:
    """
    Worker ghi hình phân đoạn cho từng Camera cụ thể (Segmented MP4 Recording).
    Ghi trực tiếp H.264 qua FFmpeg Pipe; hỗ trợ Idle Watchdog và loại bỏ phân đoạn rác.
    """
    def __init__(self, camera_id: int, camera_name: str, target_fps: int = 6):
        self.camera_id = camera_id
        self.camera_name = camera_name
        self.target_fps = target_fps if target_fps > 0 else 6
        
        self.frame_queue: queue.Queue = queue.Queue(maxsize=120)  # Bộ đệm tối đa 120 frame (~20 giây)
        self.stop_requested = threading.Event()
        self.rotate_requested = threading.Event()
        self.rotation_done_event = threading.Event()
        self.worker_thread = threading.Thread(target=self._run_writer_loop, daemon=True)
        
        # State ghi hình
        self.writer: Optional[Union[FFmpegPipeVideoWriter, cv2.VideoWriter]] = None
        self.is_ffmpeg_writer: bool = False
        self.current_recording_id: Optional[int] = None
        self.current_file_path: Optional[str] = None
        self.segment_start_time: Optional[datetime] = None
        self.last_frame_time: float = 0.0
        self.frame_size: Optional[Tuple[int, int]] = None
        self.frame_count: int = 0
        self.is_active: bool = False

    def request_rotation(self) -> threading.Event:
        """Kích hoạt chốt file segment hiện tại và mở file mới ngay lập tức"""
        self.rotation_done_event.clear()
        self.rotate_requested.set()
        return self.rotation_done_event

    def start(self):
        self.is_active = True
        self.stop_requested.clear()
        self.worker_thread = threading.Thread(target=self._run_writer_loop, daemon=True, name=f"RecWorker-Cam{self.camera_id}")
        self.worker_thread.start()
        logger.info(f"[{self.camera_name}] (ID: {self.camera_id}) Video recording worker started.")

    def push_frame(self, frame: np.ndarray):
        if not self.is_active or self.stop_requested.is_set() or frame is None:
            return
        try:
            self.frame_queue.put_nowait(frame.copy())
        except queue.Full:
            # Drop frame cũ nhất nếu đĩa ghi chậm để bảo vệ RAM
            try:
                _ = self.frame_queue.get_nowait()
                self.frame_queue.put_nowait(frame.copy())
            except Exception:
                pass

    def _get_segment_duration_seconds(self) -> int:
        sys_cfg = settings_manager.get_settings()
        mins = sys_cfg.get("video_segment_minutes", 5)
        try:
            mins = int(mins)
        except Exception:
            mins = 5
        mins = max(1, min(60, mins))
        return mins * 60

    def _open_new_segment(self, frame_w: int, frame_h: int) -> bool:
        self._close_current_segment()
        
        now = datetime.now()
        base_dir = get_storage_base_dir()
        
        date_str = now.strftime("%Y-%m-%d")
        cam_folder = os.path.join(base_dir, f"camera_{self.camera_id}", date_str)
        os.makedirs(cam_folder, exist_ok=True)
        
        filename = f"cam_{self.camera_id}_{now.strftime('%Y%m%d_%H%M%S')}.mp4"
        file_path = os.path.join(cam_folder, filename)
        
        # 1. Thử ghi trực tiếp H.264 qua FFmpeg Pipe
        writer = FFmpegPipeVideoWriter(file_path, frame_w, frame_h, float(self.target_fps))
        is_ffmpeg = True

        if not writer.isOpened():
            logger.warning(f"[{self.camera_name}] FFmpeg pipe writer failed, falling back to cv2.VideoWriter mp4v")
            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            writer = cv2.VideoWriter(file_path, fourcc, float(self.target_fps), (frame_w, frame_h))
            is_ffmpeg = False
            
            if not writer.isOpened():
                logger.error(f"[{self.camera_name}] Failed to open both FFmpeg and OpenCV VideoWriter for {file_path}")
                return False

        self.writer = writer
        self.is_ffmpeg_writer = is_ffmpeg
        self.current_file_path = file_path
        self.segment_start_time = now
        self.last_frame_time = time.time()
        self.frame_size = (frame_w, frame_h)
        self.frame_count = 0
        
        # Lưu bản ghi ban đầu vào CSDL
        db = SessionLocal()
        try:
            try:
                rel_path = os.path.relpath(file_path, settings.BASE_DIR).replace("\\", "/")
            except Exception:
                rel_path = file_path.replace("\\", "/")

            rec = VideoRecording(
                camera_id=self.camera_id,
                start_time=now,
                end_time=None,
                file_path=rel_path,
                file_size_mb=0.0,
                duration_seconds=0,
                status="recording"
            )
            db.add(rec)
            db.commit()
            db.refresh(rec)
            self.current_recording_id = rec.id
            logger.info(f"[{self.camera_name}] Started new video segment: {filename} (ID: {rec.id}, Engine: {'FFmpeg H.264' if is_ffmpeg else 'OpenCV'})")
        except Exception as e:
            logger.error(f"[{self.camera_name}] Failed to insert VideoRecording record into DB: {e}")
            db.rollback()
        finally:
            db.close()
            
        return True

    def _close_current_segment(self):
        if self.writer is not None:
            try:
                self.writer.release()
            except Exception as e:
                logger.warning(f"[{self.camera_name}] Error releasing writer: {e}")
            self.writer = None

        closed_file_path = self.current_file_path
        closed_rec_id = self.current_recording_id
        is_ffmpeg = self.is_ffmpeg_writer

        if closed_rec_id and closed_file_path and self.segment_start_time:
            file_size_mb = 0.0
            file_exists = os.path.exists(closed_file_path)
            if file_exists:
                file_size_bytes = os.path.getsize(closed_file_path)
                file_size_mb = round(file_size_bytes / (1024 * 1024), 2)
            else:
                file_size_bytes = 0

            # Xử lý dọn dẹp phân đoạn rác (< 10KB hoặc 0 frame do camera ngắt ngay lập tức)
            if self.frame_count == 0 or file_size_bytes < 10240:
                logger.warning(f"[{self.camera_name}] Segment has 0 frames or <10KB, discarding empty file and DB record.")
                if file_exists:
                    try:
                        os.remove(closed_file_path)
                    except Exception:
                        pass
                db = SessionLocal()
                try:
                    db.query(VideoRecording).filter(VideoRecording.id == closed_rec_id).delete()
                    db.commit()
                except Exception as e:
                    db.rollback()
                finally:
                    db.close()
            else:
                # Tính thời lượng thực tế chính xác dựa trên số frame đã ghi
                actual_duration = max(1, int(round(self.frame_count / self.target_fps)))
                end_time = self.segment_start_time + timedelta(seconds=actual_duration)

                db = SessionLocal()
                try:
                    rec = db.query(VideoRecording).filter(VideoRecording.id == closed_rec_id).first()
                    if rec:
                        rec.end_time = end_time
                        rec.file_size_mb = file_size_mb
                        rec.duration_seconds = actual_duration
                        rec.status = "completed"
                        db.commit()
                        logger.info(f"[{self.camera_name}] Closed video segment ID {closed_rec_id}: {actual_duration}s ({self.frame_count} frames), {file_size_mb} MB")
                except Exception as e:
                    logger.error(f"[{self.camera_name}] Error updating VideoRecording record: {e}")
                    db.rollback()
                finally:
                    db.close()

                if is_ffmpeg:
                    _h264_ready_files.add(closed_file_path)
                else:
                    # Nếu dùng OpenCV fallback, convert ngầm sang H.264
                    _async_convert_to_h264_faststart(closed_file_path, closed_rec_id, self.camera_name, self.target_fps)

        self.current_recording_id = None
        self.current_file_path = None
        self.segment_start_time = None
        self.frame_count = 0
        self.rotation_done_event.set()

    def _run_writer_loop(self):
        while not self.stop_requested.is_set() or not self.frame_queue.empty():
            # Nếu có yêu cầu rotate và file đang mở, chốt file ngay
            if self.rotate_requested.is_set() and self.writer is not None:
                self.rotate_requested.clear()
                self._close_current_segment()
                self.rotation_done_event.set()
                continue

            try:
                frame = self.frame_queue.get(timeout=0.3)
            except queue.Empty:
                # Idle Watchdog: Nếu quá 8 giây không có frame mới (camera offline/mất kết nối),
                # tự động chốt file để tránh tạo phân đoạn dài ảo
                if self.writer is not None and (time.time() - self.last_frame_time > 8.0):
                    logger.info(f"[{self.camera_name}] Idle timeout (>8s no frames). Auto-closing segment.")
                    self._close_current_segment()
                continue

            self.last_frame_time = time.time()
            h, w = frame.shape[:2]
            current_time = datetime.now()
            max_segment_sec = self._get_segment_duration_seconds()

            need_new_segment = (
                self.writer is None or 
                self.frame_size != (w, h) or
                self.rotate_requested.is_set() or
                (self.segment_start_time and (current_time - self.segment_start_time).total_seconds() >= max_segment_sec)
            )

            if need_new_segment:
                had_rot = self.rotate_requested.is_set()
                self.rotate_requested.clear()
                success = self._open_new_segment(w, h)
                if had_rot:
                    self.rotation_done_event.set()
                if not success:
                    time.sleep(0.5)
                    continue

            # Ghi frame vào file
            try:
                self.writer.write(frame)
                self.frame_count += 1
            except Exception as e:
                logger.error(f"[{self.camera_name}] Error writing frame to video file: {e}")

        # Khi vòng lặp kết thúc, đóng file segment hiện tại
        self._close_current_segment()
        self.rotation_done_event.set()
        logger.info(f"[{self.camera_name}] Video recording worker loop stopped.")

    def stop(self):
        if not self.is_active:
            return
        self.is_active = False
        self.stop_requested.set()
        if self.worker_thread.is_alive():
            self.worker_thread.join(timeout=3.0)
        self._close_current_segment()
        logger.info(f"[{self.camera_name}] (ID: {self.camera_id}) Video recorder stopped.")


class VideoRecorderManager:
    """
    Bộ quản lý tập trung toàn bộ các luồng ghi hình video của các Camera trong hệ thống.
    """
    def __init__(self):
        self.recorders: Dict[int, CameraVideoRecorder] = {}
        self.lock = threading.Lock()

    def start_recorder(self, camera_id: int, camera_name: str, target_fps: int = 6):
        with self.lock:
            existing = self.recorders.get(camera_id)
            if existing:
                if existing.is_active:
                    return
                existing.stop()
            
            recorder = CameraVideoRecorder(camera_id=camera_id, camera_name=camera_name, target_fps=target_fps)
            recorder.start()
            self.recorders[camera_id] = recorder
            logger.info(f"Started video recording for Camera [{camera_name}] (ID: {camera_id})")

    def stop_recorder(self, camera_id: int):
        with self.lock:
            recorder = self.recorders.pop(camera_id, None)
            if recorder:
                recorder.stop()
                logger.info(f"Stopped video recording for Camera ID: {camera_id}")

    def rotate_camera_segment(self, camera_id: int, wait_timeout: float = 2.0) -> bool:
        """Kích hoạt hoàn tất file video hiện tại và đợi chốt file có header moov để xem được ngay"""
        with self.lock:
            recorder = self.recorders.get(camera_id)
        if recorder and recorder.is_active:
            evt = recorder.request_rotation()
            return evt.wait(timeout=wait_timeout)
        return False

    def push_frame(self, camera_id: int, frame: np.ndarray):
        recorder = self.recorders.get(camera_id)
        if recorder and recorder.is_active:
            recorder.push_frame(frame)

    def is_recording(self, camera_id: int) -> bool:
        recorder = self.recorders.get(camera_id)
        return recorder is not None and recorder.is_active

    def stop_all(self):
        with self.lock:
            for cam_id, recorder in list(self.recorders.items()):
                recorder.stop()
            self.recorders.clear()
            logger.info("All video recorders stopped.")


def recover_orphaned_recordings():
    """
    Quét và phục hồi các phân đoạn video dở dang (status='recording') do cúp điện hoặc tắt máy chủ đột ngột.
    Kiểm tra file vật lý trên đĩa để cập nhật đúng thời lượng hoặc dọn dẹp các bản ghi rác.
    """
    db = SessionLocal()
    try:
        orphans = db.query(VideoRecording).filter(VideoRecording.status == "recording").all()
        if not orphans:
            return
        
        logger.info(f"Found {len(orphans)} orphaned recording(s) with status='recording'. Running crash recovery...")
        fixed_count = 0
        cleaned_count = 0

        # Import open_video_file_absolute an toàn
        from routes.playback import open_video_file_absolute

        for rec in orphans:
            abs_path = open_video_file_absolute(rec.file_path)
            if not os.path.exists(abs_path) or os.path.getsize(abs_path) < 10240:
                # File không tồn tại hoặc rỗng (<10KB) -> xóa bản ghi rác
                db.delete(rec)
                cleaned_count += 1
            else:
                # File tồn tại -> đọc kích thước và tính duration
                file_size_mb = round(os.path.getsize(abs_path) / (1024 * 1024), 2)
                cap = cv2.VideoCapture(abs_path)
                fps = cap.get(cv2.CAP_PROP_FPS) or 6.0
                frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
                cap.release()
                dur = int(frame_count / fps) if fps > 0 and frame_count > 0 else 0
                if dur <= 0:
                    dur = int(rec.duration_seconds) if rec.duration_seconds else 10
                
                rec.file_size_mb = file_size_mb
                rec.duration_seconds = max(1, dur)
                rec.end_time = rec.start_time + timedelta(seconds=max(1, dur))
                rec.status = "completed"
                fixed_count += 1
                _h264_ready_files.add(abs_path)
        
        db.commit()
        logger.info(f"Crash recovery complete: Fixed {fixed_count} recording(s), cleaned {cleaned_count} ghost record(s).")
    except Exception as e:
        logger.error(f"Error in recover_orphaned_recordings: {e}")
        db.rollback()
    finally:
        db.close()


# Instance singleton toàn cục
video_recorder_manager = VideoRecorderManager()
