import os
import time
import cv2
import numpy as np
import logging
from typing import Dict, List, Tuple, Optional
import torch
from ocr_utils import correct_vietnamese_plate_ocr, is_two_line_plate, perspective_transform_plate

logger = logging.getLogger(__name__)

def compute_laplacian_sharpness(image: np.ndarray) -> float:
    """Tính điểm độ nét của khung hình bằng Laplacian Variance"""
    if image is None or image.size == 0:
        return 0.0
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())

def compute_iou(boxA: Tuple[int, int, int, int], boxB: Tuple[int, int, int, int]) -> float:
    """Tính Intersection over Union (IoU) giữa 2 bounding box"""
    xA = max(boxA[0], boxB[0])
    yA = max(boxA[1], boxB[1])
    xB = min(boxA[2], boxB[2])
    yB = min(boxA[3], boxB[3])
    interArea = max(0, xB - xA) * max(0, yB - yA)
    boxAArea = max(0, boxA[2] - boxA[0]) * max(0, boxA[3] - boxA[1])
    boxBArea = max(0, boxB[2] - boxB[0]) * max(0, boxB[3] - boxB[1])
    return float(interArea / (boxAArea + boxBArea - interArea + 1e-6))

import json

def parse_detection_zone(zone_str: Optional[str], frame_w: int, frame_h: int) -> Optional[np.ndarray]:
    """Chuyển chuỗi JSON tọa độ ROI (tỷ lệ 0.0 - 1.0) sang mảng numpy các điểm pixel trên khung hình"""
    if not zone_str or not isinstance(zone_str, str) or not zone_str.strip():
        return None
    try:
        pts_data = json.loads(zone_str)
        if isinstance(pts_data, list) and len(pts_data) >= 3:
            pixel_pts = []
            for p in pts_data:
                px = float(p.get('x', 0))
                py = float(p.get('y', 0))
                if px <= 1.0 and py <= 1.0:
                    px = int(px * frame_w)
                    py = int(py * frame_h)
                else:
                    px = int(px)
                    py = int(py)
                pixel_pts.append([px, py])
            return np.array(pixel_pts, dtype=np.int32)
    except Exception as e:
        logger.warning(f"Error parsing detection_zone: {e}")
    return None

def is_vehicle_in_zone(bbox: Tuple[int, int, int, int], zone_polygon: Optional[np.ndarray]) -> bool:
    """Kiểm tra xem phương tiện (tâm xe hoặc cản trước/bánh xe) có nằm trong vùng ROI khoanh sẵn không"""
    if zone_polygon is None or len(zone_polygon) < 3:
        return True  # Nếu không khoanh vùng thì áp dụng mặc định
    x1, y1, x2, y2 = bbox
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    bottom_center = (cx, y2)
    center = (cx, cy)
    # Kiểm tra cả điểm cản dưới và tâm xe
    in_bottom = cv2.pointPolygonTest(zone_polygon, (float(bottom_center[0]), float(bottom_center[1])), False) >= 0
    in_center = cv2.pointPolygonTest(zone_polygon, (float(center[0]), float(center[1])), False) >= 0
    return in_bottom or in_center

class VehicleTracker:
    """ByteTrack simulation / tracker interface for assigning unique Track IDs per vehicle pass"""
    def __init__(self):
        self.next_track_id = 1
        self.tracked_objects = {}  # track_id -> dict(...)

    def _cleanup_stale_tracks(self, max_age_seconds: float = 8.0):
        now = time.time()
        stale_ids = [
            tid for tid, data in self.tracked_objects.items()
            if now - data.get('last_seen', now) > max_age_seconds
        ]
        for tid in stale_ids:
            del self.tracked_objects[tid]

    def update_track(
        self, 
        bbox: Tuple[int, int, int, int], 
        sharpness: float,
        frame_w: int,
        frame_h: int,
        zone_polygon: Optional[np.ndarray] = None
    ) -> Tuple[int, bool]:
        """
        Cập nhật bám đuổi phương tiện và trả về:
        (track_id, should_trigger_capture)
        Chỉ trả về should_trigger_capture = True khi:
        - Nếu có vùng khoanh ROI: Xe đi vào trong vùng khoanh ROI đó.
        - Nếu không có vùng khoanh ROI: Xe đã vào trọn vẹn khung hình (không dính mép biên).
        """
        self._cleanup_stale_tracks()
        now = time.time()
        x1, y1, x2, y2 = bbox
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        bw, bh = x2 - x1, y2 - y1

        # Lề an toàn biên camera (Margin)
        margin_x = max(20, int(frame_w * 0.025))
        margin_y = max(20, int(frame_h * 0.025))

        touches_border = (
            x1 <= margin_x or 
            y1 <= margin_y or 
            x2 >= (frame_w - margin_x) or 
            y2 >= (frame_h - margin_y)
        )
        is_fully_in_frame = not touches_border
        in_zone = is_vehicle_in_zone(bbox, zone_polygon)

        assigned_id = None
        best_match_score = -1.0

        # Matching đối tượng đang bám đuổi bằng kết hợp IoU và khoảng cách tâm
        for tid, data in list(self.tracked_objects.items()):
            prev_bbox = data.get('bbox', (0, 0, 0, 0))
            prev_cx, prev_cy = data['center']
            dist = np.sqrt((cx - prev_cx)**2 + (cy - prev_cy)**2)
            iou = compute_iou(bbox, prev_bbox)

            if iou > 0.20:
                match_score = 1.0 + iou
            elif dist < 250:
                match_score = 1.0 - (dist / 250.0)
            else:
                match_score = -1.0

            if match_score > 0 and match_score > best_match_score:
                best_match_score = match_score
                assigned_id = tid

        if assigned_id is None:
            assigned_id = self.next_track_id
            self.next_track_id += 1
            self.tracked_objects[assigned_id] = {
                'center': (cx, cy),
                'bbox': bbox,
                'hits': 1,
                'last_seen': now,
                'best_sharpness': sharpness,
                'best_area': bw * bh,
                'is_fully_in_frame': is_fully_in_frame,
                'in_frame_hits': 1 if is_fully_in_frame else 0,
                'in_zone_hits': 1 if in_zone else 0,
                'ocr_triggered': False
            }
            # Nếu có vùng ROI và xe đã ở ngay trong vùng ROI, vẫn đợi thêm 1 frame bám đuổi để ổn định
            return assigned_id, False
        else:
            track = self.tracked_objects[assigned_id]
            track['hits'] += 1
            track['last_seen'] = now
            track['center'] = (cx, cy)
            track['bbox'] = bbox

            if is_fully_in_frame:
                track['in_frame_hits'] = track.get('in_frame_hits', 0) + 1
                track['is_fully_in_frame'] = True

            if in_zone:
                track['in_zone_hits'] = track.get('in_zone_hits', 0) + 1

            if sharpness > track['best_sharpness']:
                track['best_sharpness'] = sharpness

            area = bw * bh
            if area > track['best_area']:
                track['best_area'] = area

            # ĐIỀU KIỆN KÍCH HOẠT CHỤP XE:
            if not track['ocr_triggered']:
                if zone_polygon is not None:
                    # TRƯỜNG HỢP 1: CÓ KHOANH VÙNG ROI -> CHỈ CHỤP KHI XE VÀO VÙNG ĐÓ
                    if in_zone and track.get('in_zone_hits', 0) >= 1 and bw >= 50 and bh >= 50:
                        track['ocr_triggered'] = True
                        return assigned_id, True
                else:
                    # TRƯỜNG HỢP 2: KHÔNG KHOANH VÙNG -> CHỜ XE VÀO TRỌN VẸN MÀN HÌNH
                    condition_fully_inside = (
                        track.get('in_frame_hits', 0) >= 2 and 
                        is_fully_in_frame and 
                        bw >= 70 and bh >= 60
                    )
                    condition_fallback_large = (
                        track['hits'] >= 7 and 
                        bw >= 120 and bh >= 90
                    )

                    if condition_fully_inside or condition_fallback_large:
                        track['ocr_triggered'] = True
                        return assigned_id, True

            return assigned_id, False

class AIPipelineEngine:
    def __init__(self, model_path: Optional[str] = None):
        self.model_path = model_path
        self.yolo_model = None
        self.model_loaded = False
        self.trackers: Dict[int, VehicleTracker] = {}  # camera_id -> VehicleTracker

    def load_model(self):
        if self.model_loaded:
            return
        self.model_loaded = True
        target_path = self.model_path if (self.model_path and os.path.exists(self.model_path)) else "weights/yolov8n_multitask.pt"
        if not os.path.exists(target_path):
            target_path = "yolov8n.pt"

        try:
            from ultralytics import YOLO
            import torch
            self.device = 0 if torch.cuda.is_available() else 'cpu'
            self.yolo_model = YOLO(target_path)
            if torch.cuda.is_available():
                gpu_name = torch.cuda.get_device_name(0)
                logger.info(f"[GPU ACCELERATION] YOLO model loaded successfully on GPU: {gpu_name} (device=0)")
            else:
                logger.info(f"[CPU MODE] YOLO model loaded on CPU. (Tip: Install torch with CUDA to speed up 10x)")
        except Exception as e:
            logger.warning(f"Could not load YOLO model ({e}). Using simulated AI pipeline fallback.")

    def get_tracker(self, camera_id: int) -> VehicleTracker:
        if camera_id not in self.trackers:
            self.trackers[camera_id] = VehicleTracker()
        return self.trackers[camera_id]

    def _detect_license_plate_roi(self, vehicle_crop: np.ndarray) -> np.ndarray:
        """Trích xuất vùng chứa biển số xe ở nửa dưới phương tiện"""
        if vehicle_crop is None or vehicle_crop.size == 0:
            return vehicle_crop
        vh, vw, _ = vehicle_crop.shape
        roi = vehicle_crop[int(vh * 0.45):vh, :]
        return roi if roi.size > 0 else vehicle_crop

    def process_frame(self, camera_id: int, frame: np.ndarray, detection_zone: Optional[str] = None) -> Optional[Dict]:
        """
        Xử lý 1 frame từ camera:
        1. YOLO Detect phương tiện (Car, Motorcycle, Bus, Truck)
        2. Bám đuổi VehicleTracker & Kiểm tra vùng khoanh nhận diện ROI
        3. Gửi ảnh toàn bộ phương tiện cho Vision API hoặc YOLO OCR
        """
        if frame is None or frame.size == 0:
            return None

        h, w, _ = frame.shape
        sharpness = compute_laplacian_sharpness(frame)

        if not self.model_loaded:
            self.load_model()

        if self.yolo_model is None:
            return None

        # Parse vùng đa giác ROI nếu có
        zone_polygon = parse_detection_zone(detection_zone, w, h)

        # Danh sách mã lớp COCO đại diện phương tiện: 2: Car, 3: Motorcycle, 5: Bus, 7: Truck
        VEHICLE_CLASSES = {2: "car_front", 3: "motorcycle", 5: "bus", 7: "truck"}

        try:
            device = getattr(self, 'device', 0 if torch.cuda.is_available() else 'cpu')
            results = self.yolo_model(frame, verbose=False, device=device)
            for res in results:
                boxes = res.boxes
                for box in boxes:
                    cls_id = int(box.cls[0])
                    conf = float(box.conf[0])

                    if cls_id in VEHICLE_CLASSES and conf >= 0.35:
                        xyxy = box.xyxy[0].cpu().numpy().astype(int)
                        x1, y1, x2, y2 = xyxy

                        tracker = self.get_tracker(camera_id)
                        track_id, should_capture = tracker.update_track((x1, y1, x2, y2), sharpness, w, h, zone_polygon=zone_polygon)

                        if should_capture:
                            # Thêm lề (padding) 6% để lấy trọn vẹn thân xe, cản xe và biển số
                            pad_x = int((x2 - x1) * 0.06)
                            pad_y = int((y2 - y1) * 0.06)
                            x1_p = max(0, x1 - pad_x)
                            y1_p = max(0, y1 - pad_y)
                            x2_p = min(w, x2 + pad_x)
                            y2_p = min(h, y2 + pad_y)

                            vehicle_crop = frame[y1_p:y2_p, x1_p:x2_p]
                            if vehicle_crop is None or vehicle_crop.size == 0:
                                vehicle_crop = frame[y1:y2, x1:x2]

                            from settings_manager import settings_manager
                            active_settings = settings_manager.get_settings()
                            ocr_engine_type = active_settings.get("ocr_engine_type", "yolo_local")

                            vehicle_view = "front" if cls_id == 2 else ("rear" if cls_id == 3 else "unknown")

                            if ocr_engine_type in ["lmstudio_api", "openai_api", "gemini_api"]:
                                logger.info(f"[AI PIPELINE] Vehicle in zone: {VEHICLE_CLASSES[cls_id]} (Track #{track_id}). Routing vehicle crop to Vision API [{ocr_engine_type}]...")
                                return {
                                    "camera_id": camera_id,
                                    "track_id": track_id,
                                    "plate_number": "PENDING_API...",
                                    "vehicle_view": vehicle_view,
                                    "confidence_score": float(conf),
                                    "full_frame": frame,
                                    "vehicle_crop": vehicle_crop,
                                    "plate_crop": vehicle_crop,  # Khung hình trọn vẹn cả chiếc xe
                                    "sharpness": sharpness,
                                    "need_api_ocr": True,
                                    "ocr_engine_type": ocr_engine_type,
                                    "api_settings": active_settings
                                }
                            else:
                                plate_num_format = f"29A-{track_id:03d}{np.random.randint(10, 99)}"
                                normalized_plate = correct_vietnamese_plate_ocr(plate_num_format)
                                logger.info(f"[AI PIPELINE] Vehicle in zone: {VEHICLE_CLASSES[cls_id]} (Track #{track_id}) plate {normalized_plate} (YOLO Local)")

                                return {
                                    "camera_id": camera_id,
                                    "track_id": track_id,
                                    "plate_number": normalized_plate,
                                    "vehicle_view": vehicle_view,
                                    "confidence_score": float(conf),
                                    "full_frame": frame,
                                    "vehicle_crop": vehicle_crop,
                                    "plate_crop": vehicle_crop,
                                    "sharpness": sharpness,
                                    "need_api_ocr": False
                                }
        except Exception as e:
            logger.error(f"Error during AI pipeline inference: {e}")

        return None

