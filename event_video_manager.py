import os
import time
import cv2
import logging
import threading
import collections
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any

from config import settings
from settings_manager import settings_manager
from database import SessionLocal
from models import VehicleLog
from websocket_manager import websocket_manager
from video_recorder import FFmpegPipeVideoWriter

logger = logging.getLogger(__name__)

class EventClipCollector:
    """
    Thu thập các khung hình cho một sự kiện xe cụ thể:
    - pre_frames: danh sách khung hình từ 5 giây trước thời điểm chụp
    - post_frames: tiếp tục thu thập các khung hình trong 5 giây sau thời điểm chụp
    """
    def __init__(
        self,
        event_token: str,
        camera_id: int,
        plate_number: str,
        detected_at: datetime,
        pre_frames: List[Tuple[float, Any]],
        target_end_time: float,
        fps: float,
        output_dir: str,
        output_filename: str,
        rel_path: str,
        log_id: Optional[int] = None
    ):
        self.event_token = event_token
        self.camera_id = camera_id
        self.plate_number = plate_number
        self.detected_at = detected_at
        self.frames: List[Tuple[float, Any]] = list(pre_frames)
        self.target_end_time = target_end_time
        self.fps = max(4.0, float(fps))
        self.output_dir = output_dir
        self.output_filename = output_filename
        self.rel_path = rel_path
        self.log_id = log_id
        self.lock = threading.Lock()
        self.is_completed = False
        self.is_encoded = False

    def add_frame(self, timestamp: float, frame):
        with self.lock:
            if not self.is_completed:
                self.frames.append((timestamp, frame))
                if timestamp >= self.target_end_time:
                    self.is_completed = True

    def mark_completed(self):
        with self.lock:
            self.is_completed = True


class EventVideoManager:
    """
    Quản lý bộ đệm vòng (Rolling Pre-Buffer) và thu thập video clip ngắn (-5s đến +5s)
    cho từng sự kiện nhận dạng phương tiện.
    """
    def __init__(self):
        self.lock = threading.Lock()
        # camera_id -> deque of (timestamp, frame)
        self.camera_buffers: Dict[int, collections.deque] = {}
        # camera_id -> target_fps
        self.camera_fps: Dict[int, float] = {}
        # event_token -> EventClipCollector
        self.active_collectors: Dict[str, EventClipCollector] = {}
        # event_token -> log_id (nếu log_id được liên kết trước hoặc sau khi mã hóa)
        self.token_to_log_id: Dict[str, int] = {}
        # event_token -> encoded rel_path (nếu clip mã hóa xong trước khi log_id được gắn)
        self.completed_clips: Dict[str, str] = {}
        # Worker thread kiểm tra timeout và xuất video
        self.stop_requested = threading.Event()
        self.worker_thread = threading.Thread(target=self._monitor_loop, daemon=True, name="EventVideoMonitor")
        self.worker_thread.start()
        logger.info("EventVideoManager initialized with rolling pre-buffer & clip collector.")

    def init_camera_buffer(self, camera_id: int, target_fps: int = 6):
        """Khởi tạo hoặc cập nhật kích thước bộ đệm vòng cho camera"""
        with self.lock:
            fps = max(4.0, float(target_fps))
            self.camera_fps[camera_id] = fps
            # Lưu trữ tối đa 8 giây khung hình để đảm bảo luôn đủ 5 giây trước sự kiện
            buffer_len = int(fps * 8)
            if camera_id not in self.camera_buffers:
                self.camera_buffers[camera_id] = collections.deque(maxlen=buffer_len)
            else:
                # Nếu FPS thay đổi, tạo deque mới với kích thước phù hợp
                old_buf = self.camera_buffers[camera_id]
                new_buf = collections.deque(old_buf, maxlen=buffer_len)
                self.camera_buffers[camera_id] = new_buf

    def push_camera_frame(self, camera_id: int, frame):
        """Đẩy khung hình mới từ luồng camera vào bộ đệm vòng và các collector đang hoạt động"""
        if frame is None or frame.size == 0:
            return

        now_ts = time.time()
        
        # Tối ưu kích thước: nếu khung hình quá lớn (> 1280px), scale về max 1280px để tiết kiệm RAM & CPU
        h, w = frame.shape[:2]
        if w > 1280:
            new_w = 1280
            new_h = int(h * (1280.0 / w))
            buffered_frame = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)
        else:
            buffered_frame = frame.copy()

        # 1. Đưa vào Rolling Pre-Buffer của camera
        with self.lock:
            buf = self.camera_buffers.get(camera_id)
            if buf is None:
                fps = self.camera_fps.get(camera_id, 6.0)
                buf = collections.deque(maxlen=int(fps * 8))
                self.camera_buffers[camera_id] = buf
            buf.append((now_ts, buffered_frame))

            # 2. Đẩy vào các collector đang đợi khung hình tương lai (+5s)
            active_collectors = [c for c in self.active_collectors.values() if c.camera_id == camera_id and not c.is_completed]

        for collector in active_collectors:
            collector.add_frame(now_ts, buffered_frame)

    def trigger_event_clip(
        self,
        camera_id: int,
        event_token: str,
        plate_number: str,
        detected_at: Optional[datetime] = None,
        log_id: Optional[int] = None
    ) -> Optional[str]:
        """
        Kích hoạt thu thập video sự kiện từ T-5s đến T+5s.
        Trả về relative file path dự kiến của clip.
        """
        sys_cfg = settings_manager.get_settings()
        enabled = sys_cfg.get("enable_event_video_clip", True)
        if not enabled:
            return None

        pre_sec = float(sys_cfg.get("event_video_pre_seconds", 5))
        post_sec = float(sys_cfg.get("event_video_post_seconds", 5))

        if detected_at is None:
            detected_at = datetime.now()

        now_ts = time.time()
        cutoff_pre_ts = now_ts - pre_sec

        # 1. Trích xuất các khung hình trong quá khứ [T - pre_sec, T] từ Ring Buffer
        pre_frames: List[Tuple[float, Any]] = []
        with self.lock:
            buf = self.camera_buffers.get(camera_id)
            if buf:
                for ts, f in list(buf):
                    if ts >= cutoff_pre_ts:
                        pre_frames.append((ts, f))
            fps = self.camera_fps.get(camera_id, 6.0)

        # 2. Tạo đường dẫn lưu file MP4 tương tự ảnh chụp sự kiện
        date_folder = detected_at.strftime("%Y/%m/%d")
        cam_folder = f"cam_{camera_id}"
        target_dir = os.path.join(settings.CAPTURES_DIR, date_folder, cam_folder)
        os.makedirs(target_dir, exist_ok=True)

        timestamp_str = detected_at.strftime("%Y%m%d_%H%M%S_%f")[:19]
        safe_plate = "".join([c for c in plate_number if c.isalnum()]) or "UNKNOWN"
        video_filename = f"{timestamp_str}_event_{safe_plate}.mp4"
        rel_path = os.path.normpath(os.path.join(date_folder, cam_folder, video_filename)).replace("\\", "/")

        target_end_ts = now_ts + post_sec

        collector = EventClipCollector(
            event_token=event_token,
            camera_id=camera_id,
            plate_number=plate_number,
            detected_at=detected_at,
            pre_frames=pre_frames,
            target_end_time=target_end_ts,
            fps=fps,
            output_dir=target_dir,
            output_filename=video_filename,
            rel_path=rel_path,
            log_id=log_id
        )

        with self.lock:
            self.active_collectors[event_token] = collector
            if log_id:
                self.token_to_log_id[event_token] = log_id

        logger.info(
            f"[EventVideoManager] Started clip collection for [{plate_number}] on Cam #{camera_id} "
            f"(Pre-frames: {len(pre_frames)}, Target end in {post_sec}s -> {video_filename})"
        )
        return rel_path

    def attach_log_id(self, event_token: str, log_id: int):
        """Gắn log_id sau khi bản ghi VehicleLog được tạo trong CSDL"""
        if not event_token or not log_id:
            return

        with self.lock:
            self.token_to_log_id[event_token] = log_id
            collector = self.active_collectors.get(event_token)
            if collector:
                collector.log_id = log_id
            
            # Nếu clip đã mã hóa xong trước đó, cập nhật ngay vào CSDL
            rel_path = self.completed_clips.pop(event_token, None)

        if rel_path:
            self._update_log_video_path(log_id, rel_path, collector.plate_number if collector else "")

    def _monitor_loop(self):
        """Vòng lặp chạy ngầm kiểm tra các collector đã hoàn thành thời gian hứng khung hình (+5s)"""
        while not self.stop_requested.is_set():
            time.sleep(0.3)
            now_ts = time.time()
            to_encode: List[EventClipCollector] = []

            with self.lock:
                for token, collector in list(self.active_collectors.items()):
                    # Đã hứng đủ thời gian hoặc quá hạn timeout (thêm 2.5s phòng khi mất tín hiệu)
                    if not collector.is_encoded and (collector.is_completed or now_ts >= collector.target_end_time + 2.5):
                        collector.is_completed = True
                        collector.is_encoded = True
                        to_encode.append(collector)
                        self.active_collectors.pop(token, None)

            for col in to_encode:
                # Xử lý mã hóa video bất đồng bộ trên luồng riêng để không nghẽn monitor loop
                threading.Thread(
                    target=self._encode_and_save_clip,
                    args=(col,),
                    daemon=True,
                    name=f"EncodeClip-{col.output_filename}"
                ).start()

    def _encode_and_save_clip(self, collector: EventClipCollector):
        """Mã hóa danh sách khung hình ra file MP4 chuẩn H.264 + faststart bằng FFmpeg"""
        output_filepath = os.path.join(collector.output_dir, collector.output_filename)
        frames_data = collector.frames

        if not frames_data:
            logger.warning(f"[EventVideoManager] No frames collected for [{collector.plate_number}], skipping video clip.")
            return

        # Sắp xếp khung hình theo thứ tự thời gian
        frames_data.sort(key=lambda x: x[0])
        first_frame = frames_data[0][1]
        h, w = first_frame.shape[:2]

        # Tính toán FPS thực tế dựa trên độ dài thời gian của các frame
        total_frames = len(frames_data)
        time_span = frames_data[-1][0] - frames_data[0][0]
        if time_span > 1.0 and total_frames > 2:
            calc_fps = total_frames / time_span
            effective_fps = max(4.0, min(calc_fps, 25.0))
        else:
            effective_fps = collector.fps

        logger.info(
            f"[EventVideoManager] Encoding clip {collector.output_filename} ({total_frames} frames, "
            f"{effective_fps:.1f} fps, {w}x{h}, span: {time_span:.1f}s)..."
        )

        try:
            writer = FFmpegPipeVideoWriter(output_filepath, width=w, height=h, fps=effective_fps)
            for ts, frame in frames_data:
                if frame.shape[:2] != (h, w):
                    frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
                writer.write(frame)
            writer.release()

            if os.path.exists(output_filepath) and os.path.getsize(output_filepath) > 1024:
                file_size_kb = os.path.getsize(output_filepath) / 1024
                logger.info(f"✅ [EventVideoManager] Successfully created event clip: {collector.rel_path} ({file_size_kb:.1f} KB)")

                # Lấy log_id nếu đã có
                with self.lock:
                    log_id = collector.log_id or self.token_to_log_id.get(collector.event_token)
                    if not log_id:
                        # Lưu tạm để attach_log_id cập nhật sau
                        self.completed_clips[collector.event_token] = collector.rel_path

                if log_id:
                    self._update_log_video_path(log_id, collector.rel_path, collector.plate_number)
            else:
                logger.warning(f"❌ [EventVideoManager] Failed to create valid event clip: {output_filepath}")
        except Exception as e:
            logger.error(f"[EventVideoManager] Error encoding event clip: {e}")

    def _update_log_video_path(self, log_id: int, rel_path: str, plate_number: str):
        """Cập nhật video_path vào VehicleLog trong CSDL và gửi WebSocket thông báo tới UI"""
        db = SessionLocal()
        try:
            log_item = db.query(VehicleLog).filter(VehicleLog.id == log_id).first()
            if log_item:
                log_item.video_path = rel_path
                db.commit()
                logger.info(f"[EventVideoManager] Linked event clip to VehicleLog #{log_id} ({plate_number}): {rel_path}")

                # Gửi thông báo WebSocket tới tất cả các client Web đang mở
                video_url = f"/static/captures/{rel_path}"
                websocket_manager.broadcast_sync({
                    "event": "vehicle_log_video_ready",
                    "data": {
                        "id": log_id,
                        "plate_number": plate_number,
                        "video_path": video_url,
                        "video_url": video_url
                    }
                })
        except Exception as e:
            db.rollback()
            logger.error(f"[EventVideoManager] Failed to update VehicleLog #{log_id} with video_path: {e}")
        finally:
            db.close()

    def stop_all(self):
        self.stop_requested.set()
        if self.worker_thread.is_alive():
            self.worker_thread.join(timeout=2.0)
        logger.info("EventVideoManager stopped.")


# Global Singleton instance
event_video_manager = EventVideoManager()
