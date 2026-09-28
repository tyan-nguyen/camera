import os
# Cấu hình FFmpeg timeout 10 giây (10.000.000 microsecond) để hỗ trợ camera IP/RTSP qua mạng WAN/Internet và codec H.265
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|stimeout;10000000|timeout;10000000"

import time
import threading
import logging
import socket
import cv2
import numpy as np
from typing import Dict, Optional

from database import SessionLocal
from models import Camera, CameraFunctionEnum
from ai_pipeline import AIPipelineEngine
from disk_worker import AsyncDiskWorker
from video_recorder import video_recorder_manager

logger = logging.getLogger(__name__)

def strip_accents(text: str) -> str:
    """Loại bỏ dấu tiếng Việt khi vẽ chữ bằng OpenCV cv2.putText để tránh bị ký tự hỏi chấm ??"""
    import unicodedata
    if not text:
        return ""
    text = unicodedata.normalize('NFD', text)
    text = ''.join(c for c in text if unicodedata.category(c) != 'Mn')
    return text.replace('đ', 'd').replace('Đ', 'D')

def sanitize_rtsp_url(url: str) -> str:
    """Tự động mã hóa ký tự @ trong mật khẩu (%40) để tránh lỗi 'Unable to open RTSP for listening' của FFmpeg"""
    if not isinstance(url, str) or not url.startswith("rtsp://"):
        return url
    import re
    import urllib.parse
    match = re.match(r'^rtsp://([^:]+):(.+)@([^@]+)$', url)
    if match:
        user, pwd, host_path = match.groups()
        encoded_pwd = urllib.parse.quote(urllib.parse.unquote(pwd), safe='')
        return f"rtsp://{user}:{encoded_pwd}@{host_path}"
    return url

class CameraThread(threading.Thread):
    def __init__(
        self, 
        camera_id: int, 
        camera_name: str, 
        rtsp_url: str, 
        target_fps: int, 
        ai_engine: AIPipelineEngine, 
        disk_worker: AsyncDiskWorker, 
        detection_zone: Optional[str] = None,
        camera_function: str = "ANPR",
        is_recording: bool = False
    ):
        super().__init__(daemon=True)
        self.camera_id = camera_id
        self.camera_name = camera_name
        self.rtsp_url = rtsp_url
        self.target_fps = target_fps if target_fps > 0 else 6
        self.ai_engine = ai_engine
        self.disk_worker = disk_worker
        self.detection_zone = detection_zone
        self.camera_function = camera_function or "ANPR"
        self.is_recording = bool(is_recording)
        self.stop_requested = threading.Event()
        self.latest_frame: Optional[np.ndarray] = self._generate_connecting_frame()
        self.frame_lock = threading.Lock()

        # Khởi động recorder nếu bật lưu
        if self.is_recording:
            video_recorder_manager.start_recorder(self.camera_id, self.camera_name, self.target_fps)

    def set_recording_state(self, is_recording: bool):
        """Bật/Tắt chế độ lưu video trực tiếp mà không ngắt luồng live stream"""
        self.is_recording = bool(is_recording)
        if self.is_recording:
            video_recorder_manager.start_recorder(self.camera_id, self.camera_name, self.target_fps)
            logger.info(f"[{self.camera_name}] Hot-enabled video recording.")
        else:
            video_recorder_manager.stop_recorder(self.camera_id)
            logger.info(f"[{self.camera_name}] Hot-disabled video recording.")

    def set_detection_zone(self, new_zone: Optional[str]):
        """Cập nhật vùng khoanh ROI trực tiếp trong thời gian thực"""
        self.detection_zone = new_zone
        logger.info(f"[{self.camera_name}] Updated detection zone ROI: {new_zone}")

    def update_runtime_config(
        self, 
        camera_name: str, 
        target_fps: int, 
        camera_function: str, 
        detection_zone: Optional[str] = None,
        is_recording: bool = False
    ):
        """Cập nhật cấu hình runtime tức thì mà không cần khởi động lại kết nối RTSP/OpenCV"""
        self.camera_name = camera_name
        self.target_fps = target_fps if target_fps > 0 else 6
        self.camera_function = camera_function or "ANPR"
        self.detection_zone = detection_zone
        self.set_recording_state(is_recording)
        logger.info(f"[{self.camera_name}] Hot-reloaded runtime config: FPS={self.target_fps}, Func={self.camera_function}, Rec={self.is_recording}, ZoneROI={self.detection_zone}")

    def _draw_zone_overlay(self, frame: np.ndarray) -> np.ndarray:
        """Vẽ lớp phủ vùng khoanh ROI (Detection Zone) màu xanh neon trực quan lên video live"""
        if not self.detection_zone or self.camera_function == CameraFunctionEnum.SURVEILLANCE.value:
            return frame
        try:
            from ai_pipeline import parse_detection_zone
            h, w = frame.shape[:2]
            pts = parse_detection_zone(self.detection_zone, w, h)
            if pts is not None and len(pts) >= 3:
                overlay = frame.copy()
                cv2.fillPoly(overlay, [pts], (0, 200, 80))
                cv2.addWeighted(overlay, 0.20, frame, 0.80, 0, frame)
                cv2.polylines(frame, [pts], isClosed=True, color=(0, 255, 128), thickness=2)
                for p in pts:
                    cv2.circle(frame, (int(p[0]), int(p[1])), 4, (255, 255, 255), -1)
                top_pt = pts[0]
                cv2.putText(frame, "VUNG KHOANH ROI", (max(10, int(top_pt[0])), max(25, int(top_pt[1]) - 8)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.48, (0, 255, 128), 1, cv2.LINE_AA)
        except Exception:
            pass
        return frame

    def _generate_connecting_frame(self) -> np.ndarray:
        img = np.zeros((360, 640, 3), dtype=np.uint8)
        img[:, :] = [20, 30, 45]
        cv2.rectangle(img, (10, 10), (630, 350), (220, 140, 40), 2)
        cv2.circle(img, (40, 40), 10, (255, 180, 50), -1)

        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        ascii_name = strip_accents(self.camera_name)

        func_badge = "QUAN SAT (CCTV)" if self.camera_function == "SURVEILLANCE" else "DOC BIEN SO (ANPR)"
        cv2.putText(img, f"CAMERA - {ascii_name} [{func_badge}]", (60, 45), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2)
        cv2.putText(img, "DANG KET NOI LUONG RTSP...", (30, 120), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 200, 50), 2)
        cv2.putText(img, "Vui long cho 3 - 5 giay de nap hinh anh...", (30, 170), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (200, 200, 200), 1)
        cv2.putText(img, f"Time: {now_str} | ID: {self.camera_id}", (30, 325), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (150, 150, 150), 1)
        return img

    def _generate_offline_status_frame(self, reason: str) -> np.ndarray:
        img = np.zeros((360, 640, 3), dtype=np.uint8)
        img[:, :] = [25, 25, 30]

        cv2.rectangle(img, (10, 10), (630, 350), (40, 40, 180), 2)
        cv2.circle(img, (40, 40), 10, (0, 0, 220), -1)

        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        ascii_name = strip_accents(self.camera_name)

        cv2.putText(img, f"CAMERA OFFLINE - {ascii_name}", (60, 45), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 255), 2)
        cv2.putText(img, "TRANG THAI: KHONG KET NOI DUOC (NO CONNECTION)", (30, 110), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (50, 50, 255), 2)
        cv2.putText(img, "Ly do / Error:", (30, 160), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (180, 180, 180), 1)
        cv2.putText(img, f"- {reason}", (30, 190), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 200, 255), 1)
        cv2.putText(img, "- Kiem tra cap mang & Dia chi IP RTSP", (30, 230), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (150, 150, 150), 1)
        cv2.putText(img, "- Kiem tra Tai khoan / Mat khau Camera IP", (30, 255), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (150, 150, 150), 1)
        cv2.putText(img, f"Time: {now_str} | ID: {self.camera_id}", (30, 325), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (120, 120, 120), 1)
        return img

    def run(self):
        logger.info(f"Starting camera thread for [{self.camera_name}] (ID: {self.camera_id}, Type: {self.camera_function}, Rec: {self.is_recording}) RTSP: {self.rtsp_url}")
        frame_interval = 1.0 / self.target_fps
        last_processed_time = time.time()

        if self.rtsp_url.isdigit():
            stream_src = int(self.rtsp_url)
            is_usb = True
        else:
            stream_src = sanitize_rtsp_url(self.rtsp_url)
            is_usb = False

        while not self.stop_requested.is_set():
            if is_usb:
                cap = cv2.VideoCapture(stream_src)
            else:
                params = [
                    cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 10000,
                    cv2.CAP_PROP_READ_TIMEOUT_MSEC, 10000,
                ]
                cap = cv2.VideoCapture(stream_src, cv2.CAP_FFMPEG, params)

            if not cap.isOpened():
                reason = f"Khong the mo luong RTSP ({self.rtsp_url})"
                logger.warning(f"[{self.camera_name}] {reason}")
                frame = self._generate_offline_status_frame(reason)
                with self.frame_lock:
                    self.latest_frame = frame.copy()
                cap.release()
                time.sleep(2.0)
                continue

            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            logger.info(f"[{self.camera_name}] RTSP Stream connected successfully.")
            last_processed_time = time.time()
            last_frame_time = time.time()

            while not self.stop_requested.is_set():
                ret, raw_frame = cap.read()
                if not ret or raw_frame is None:
                    if time.time() - last_frame_time > 8.0:
                        reason = f"Mat luong tin hieu tu camera ({self.rtsp_url})"
                        logger.warning(f"[{self.camera_name}] {reason}")
                        frame = self._generate_offline_status_frame(reason)
                        with self.frame_lock:
                            self.latest_frame = frame.copy()
                        break
                    time.sleep(0.01)
                    continue

                last_frame_time = time.time()

                # 1. Ghi hình video phân đoạn nếu camera được BẬT tính năng lưu
                if self.is_recording:
                    video_recorder_manager.push_frame(self.camera_id, raw_frame)

                # 2. Tạo khung hình hiển thị Live Dashboard
                display_frame = self._draw_zone_overlay(raw_frame.copy())
                with self.frame_lock:
                    self.latest_frame = display_frame

                # 3. AI Nhận dạng biển số (chỉ chạy cho ANPR camera)
                current_time = time.time()
                if current_time - last_processed_time >= frame_interval:
                    last_processed_time = current_time
                    if self.camera_function == CameraFunctionEnum.ANPR.value or self.camera_function == "ANPR":
                        event = self.ai_engine.process_frame(self.camera_id, raw_frame, detection_zone=self.detection_zone)
                        if event:
                            self.disk_worker.enqueue_event(event)

            cap.release()
            if not self.stop_requested.is_set():
                time.sleep(1.0)

        # Dừng luồng recorder khi camera thread dừng
        video_recorder_manager.stop_recorder(self.camera_id)
        logger.info(f"Stopped camera thread for [{self.camera_name}] (ID: {self.camera_id})")

    def get_latest_frame_jpeg(self) -> Optional[bytes]:
        with self.frame_lock:
            if self.latest_frame is None:
                return None
            ret, buffer = cv2.imencode('.jpg', self.latest_frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if ret:
                return buffer.tobytes()
            return None

    def stop(self):
        self.stop_requested.set()
        video_recorder_manager.stop_recorder(self.camera_id)

class DynamicStreamManager:
    def __init__(self, ai_engine: AIPipelineEngine, disk_worker: AsyncDiskWorker):
        self.ai_engine = ai_engine
        self.disk_worker = disk_worker
        self.threads: Dict[int, CameraThread] = {}
        self.lock = threading.Lock()

    def start_all_active_cameras(self):
        """Khởi động toàn bộ camera active từ Database"""
        db = SessionLocal()
        try:
            cameras = db.query(Camera).filter(Camera.is_active == True).all()
            logger.info(f"Found {len(cameras)} active camera(s) in DB.")
            for cam in cameras:
                self.start_camera(
                    camera_id=cam.id, 
                    camera_name=cam.camera_name, 
                    rtsp_url=cam.rtsp_url, 
                    target_fps=cam.target_fps, 
                    detection_zone=cam.detection_zone,
                    camera_function=cam.camera_function or "ANPR",
                    is_recording=bool(cam.is_recording)
                )
        finally:
            db.close()

    def start_camera(
        self, 
        camera_id: int, 
        camera_name: str, 
        rtsp_url: str, 
        target_fps: int, 
        detection_zone: Optional[str] = None,
        camera_function: str = "ANPR",
        is_recording: bool = False
    ):
        with self.lock:
            if camera_id in self.threads:
                self.stop_camera_nolock(camera_id)

            thread = CameraThread(
                camera_id=camera_id,
                camera_name=camera_name,
                rtsp_url=rtsp_url,
                target_fps=target_fps,
                ai_engine=self.ai_engine,
                disk_worker=self.disk_worker,
                detection_zone=detection_zone,
                camera_function=camera_function,
                is_recording=is_recording
            )
            self.threads[camera_id] = thread
            thread.start()

    def update_camera_runtime(
        self, 
        camera_id: int, 
        camera_name: str, 
        rtsp_url: str, 
        target_fps: int, 
        detection_zone: Optional[str] = None,
        camera_function: str = "ANPR",
        is_active: bool = True,
        is_recording: bool = False
    ):
        """Cập nhật thông tin camera: hot-reload nếu rtsp_url không đổi, hoặc khởi động lại trong background"""
        with self.lock:
            existing_thread = self.threads.get(camera_id)
            if not is_active:
                if existing_thread:
                    self.stop_camera_nolock(camera_id)
                return

            # Nếu camera đang chạy và RTSP URL giữ nguyên -> Hot-reload tức thì!
            if existing_thread and existing_thread.is_alive() and existing_thread.rtsp_url == rtsp_url:
                existing_thread.update_runtime_config(
                    camera_name=camera_name,
                    target_fps=target_fps,
                    camera_function=camera_function,
                    detection_zone=detection_zone,
                    is_recording=is_recording
                )
                return

        # Nếu RTSP URL thay đổi hoặc camera đang tắt/chưa chạy -> Khởi động lại trong background thread riêng
        def _restart_bg():
            self.start_camera(
                camera_id=camera_id,
                camera_name=camera_name,
                rtsp_url=rtsp_url,
                target_fps=target_fps,
                detection_zone=detection_zone,
                camera_function=camera_function,
                is_recording=is_recording
            )
        threading.Thread(target=_restart_bg, daemon=True).start()

    def set_camera_recording(self, camera_id: int, is_recording: bool):
        """Bật/Tắt chế độ ghi hình cho camera đang chạy"""
        with self.lock:
            thread = self.threads.get(camera_id)
            if thread and thread.is_alive():
                thread.set_recording_state(is_recording)
            else:
                if is_recording:
                    logger.warning(f"Camera ID {camera_id} is not running, recording flag will apply when camera starts.")
                else:
                    video_recorder_manager.stop_recorder(camera_id)

    def update_camera_detection_zone(self, camera_id: int, new_zone: Optional[str]):
        with self.lock:
            if camera_id in self.threads:
                self.threads[camera_id].set_detection_zone(new_zone)

    def stop_camera(self, camera_id: int):
        with self.lock:
            self.stop_camera_nolock(camera_id)

    def stop_camera_nolock(self, camera_id: int):
        if camera_id in self.threads:
            thread = self.threads.pop(camera_id)
            thread.stop()
        video_recorder_manager.stop_recorder(camera_id)

    def _create_default_offline_jpeg(self, cam_name: str, reason: str) -> bytes:
        img = np.zeros((360, 640, 3), dtype=np.uint8)
        img[:, :] = [25, 25, 30]
        cv2.rectangle(img, (10, 10), (630, 350), (40, 40, 180), 2)
        cv2.circle(img, (40, 40), 10, (0, 0, 220), -1)
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        ascii_name = strip_accents(cam_name)
        cv2.putText(img, f"CAMERA OFFLINE - {ascii_name}", (60, 45), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 255), 2)
        cv2.putText(img, "TRANG THAI: KHONG KET NOI DUOC (NO CONNECTION)", (30, 110), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (50, 50, 255), 2)
        cv2.putText(img, "Ly do / Error:", (30, 160), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (180, 180, 180), 1)
        cv2.putText(img, f"- {reason}", (30, 190), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 200, 255), 1)
        cv2.putText(img, "- Kiem tra cap mang & Dia chi IP RTSP", (30, 230), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (150, 150, 150), 1)
        cv2.putText(img, "- Kiem tra Tai khoan / Mat khau Camera IP", (30, 255), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (150, 150, 150), 1)
        cv2.putText(img, f"Time: {now_str}", (30, 325), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (120, 120, 120), 1)
        ret, buf = cv2.imencode('.jpg', img, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        return buf.tobytes() if ret else b''

    def get_camera_snapshot(self, camera_id: int) -> bytes:
        thread = self.threads.get(camera_id)
        if thread:
            img_bytes = thread.get_latest_frame_jpeg()
            if img_bytes:
                return img_bytes
            return self._create_default_offline_jpeg(thread.camera_name, "Dang ket noi luong RTSP...")
        return self._create_default_offline_jpeg(f"Camera #{camera_id}", "Camera chua duoc bat hoac khong ton tai")

    def stop_all(self):
        with self.lock:
            for cam_id, thread in list(self.threads.items()):
                thread.stop()
                thread.join(timeout=0.2)
            self.threads.clear()
            video_recorder_manager.stop_all()
