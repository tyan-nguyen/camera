import os
import time
import queue
import threading
import logging
import cv2
import numpy as np
from datetime import datetime
from typing import Dict, Optional, Tuple

from config import settings
from database import SessionLocal
from models import VideoRecording, Camera
from settings_manager import settings_manager

logger = logging.getLogger(__name__)

class CameraVideoRecorder:
    """
    Worker ghi hình phân đoạn cho từng Camera cụ thể (Segmented MP4 Recording).
    Chạy trong thread riêng với hàng đợi (Queue) chống tràn ram và không block luồng RTSP stream.
    """
    def __init__(self, camera_id: int, camera_name: str, target_fps: int = 6):
        self.camera_id = camera_id
        self.camera_name = camera_name
        self.target_fps = target_fps if target_fps > 0 else 6
        
        self.frame_queue: queue.Queue = queue.Queue(maxsize=120)  # Bộ đệm tối đa 120 frame (~20 giây)
        self.stop_requested = threading.Event()
        self.worker_thread = threading.Thread(target=self._run_writer_loop, daemon=True)
        
        # State ghi hình
        self.writer: Optional[cv2.VideoWriter] = None
        self.current_recording_id: Optional[int] = None
        self.current_file_path: Optional[str] = None
        self.segment_start_time: Optional[datetime] = None
        self.frame_size: Optional[Tuple[int, int]] = None  # (width, height)
        self.frame_count: int = 0
        self.is_active: bool = False

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
            # Drop frame nếu đĩa ghi quá chậm để bảo vệ RAM
            try:
                _ = self.frame_queue.get_nowait()
                self.frame_queue.put_nowait(frame.copy())
            except Exception:
                pass

    def _get_storage_base_dir(self) -> str:
        sys_cfg = settings_manager.get_settings()
        custom_path = sys_cfg.get("video_storage_path", "storage/recordings").strip()
        if os.path.isabs(custom_path):
            base_dir = custom_path
        else:
            base_dir = os.path.join(settings.BASE_DIR, custom_path)
        os.makedirs(base_dir, exist_ok=True)
        return base_dir

    def _get_segment_duration_seconds(self) -> int:
        sys_cfg = settings_manager.get_settings()
        mins = sys_cfg.get("video_segment_minutes", 5)
        try:
            mins = int(mins)
        except Exception:
            mins = 5
        mins = max(1, min(60, mins))  # Giới hạn 1 - 60 phút
        return mins * 60

    def _open_new_segment(self, frame_w: int, frame_h: int) -> bool:
        self._close_current_segment()
        
        now = datetime.now()
        base_dir = self._get_storage_base_dir()
        
        date_str = now.strftime("%Y-%m-%d")
        cam_folder = os.path.join(base_dir, f"camera_{self.camera_id}", date_str)
        os.makedirs(cam_folder, exist_ok=True)
        
        filename = f"cam_{self.camera_id}_{now.strftime('%Y%m%d_%H%M%S')}.mp4"
        file_path = os.path.join(cam_folder, filename)
        
        # Thử codec 'mp4v' (tương thích 100% trên mọi nền tảng không cần cài FFmpeg ngoài)
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        writer = cv2.VideoWriter(file_path, fourcc, float(self.target_fps), (frame_w, frame_h))
        
        if not writer.isOpened():
            # Fallback 'avc1'
            try:
                fourcc = cv2.VideoWriter_fourcc(*'avc1')
                writer = cv2.VideoWriter(file_path, fourcc, float(self.target_fps), (frame_w, frame_h))
            except Exception:
                pass
                
        if not writer.isOpened():
            logger.error(f"[{self.camera_name}] Failed to open VideoWriter for {file_path}")
            return False

        self.writer = writer
        self.current_file_path = file_path
        self.segment_start_time = now
        self.frame_size = (frame_w, frame_h)
        self.frame_count = 0
        
        # Lưu bản ghi ban đầu vào CSDL
        db = SessionLocal()
        try:
            # Tính đường dẫn tương đối lưu trong DB
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
            logger.info(f"[{self.camera_name}] Started new video segment: {filename} (ID: {rec.id})")
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
                logger.warning(f"[{self.camera_name}] Error releasing VideoWriter: {e}")
            self.writer = None

        if self.current_recording_id and self.current_file_path and self.segment_start_time:
            now = datetime.now()
            file_size_mb = 0.0
            if os.path.exists(self.current_file_path):
                file_size_mb = round(os.path.getsize(self.current_file_path) / (1024 * 1024), 2)
            
            duration_sec = int((now - self.segment_start_time).total_seconds())
            if duration_sec <= 0 and self.frame_count > 0:
                duration_sec = int(self.frame_count / self.target_fps)

            db = SessionLocal()
            try:
                rec = db.query(VideoRecording).filter(VideoRecording.id == self.current_recording_id).first()
                if rec:
                    rec.end_time = now
                    rec.file_size_mb = file_size_mb
                    rec.duration_seconds = duration_sec
                    rec.status = "completed"
                    db.commit()
                    logger.info(f"[{self.camera_name}] Closed video segment ID {self.current_recording_id}: {duration_sec}s, {file_size_mb} MB")
            except Exception as e:
                logger.error(f"[{self.camera_name}] Error updating VideoRecording record: {e}")
                db.rollback()
            finally:
                db.close()

        self.current_recording_id = None
        self.current_file_path = None
        self.segment_start_time = None
        self.frame_count = 0

    def _run_writer_loop(self):
        while not self.stop_requested.is_set() or not self.frame_queue.empty():
            try:
                frame = self.frame_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            h, w = frame.shape[:2]
            current_time = datetime.now()
            max_segment_sec = self._get_segment_duration_seconds()

            # Kiểm tra nếu cần mở file mới
            need_new_segment = (
                self.writer is None or 
                self.frame_size != (w, h) or
                (self.segment_start_time and (current_time - self.segment_start_time).total_seconds() >= max_segment_sec)
            )

            if need_new_segment:
                success = self._open_new_segment(w, h)
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

# Instance singleton toàn cục
video_recorder_manager = VideoRecorderManager()
