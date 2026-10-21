import os
import re
import cv2
import shutil
import logging
import subprocess
import threading
from datetime import datetime, timedelta
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, or_

from database import get_db, SessionLocal
from config import settings
from models import Camera, VideoRecording, VideoExportJob, VehicleLog, UserRoleEnum, User
from settings_manager import settings_manager
from schemas import (
    PlaybackTimelineResponse, 
    TimelineBlock, 
    TimelineEvent, 
    VideoRecordingResponse, 
    VideoExportRequest, 
    VideoExportResponse,
    StorageStatusResponse
)
from auth_utils import require_roles, get_current_user_optional

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/playback", tags=["Video Playback & Exports"])

def check_camera_access(camera_id: int, user: Optional[User]) -> bool:
    if not user:
        return True
    if user.role == UserRoleEnum.ADMIN.value:
        return True
    allowed_ids = [c.id for c in user.allowed_cameras] if user.allowed_cameras else []
    return camera_id in allowed_ids

def parse_iso_or_str_datetime(dt_str: str) -> datetime:
    """Hỗ trợ parse nhiều định dạng chuỗi datetime khác nhau"""
    dt_str = dt_str.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(dt_str, fmt)
        except ValueError:
            continue
    raise ValueError(f"Invalid datetime format: {dt_str}")

# Bộ nhớ đệm các file video đã xác minh chuẩn H.264 (tránh kiểm tra lại trên từng Range request)
_h264_verified_paths = set()
# Khóa tránh chạy đồng thời nhiều tiến trình FFmpeg trên cùng 1 file
_transcoding_in_progress = set()
_transcode_lock = threading.Lock()

def open_video_file_absolute(file_path: str) -> str:
    """
    Chuẩn hóa đường dẫn file video tuyệt đối trên ổ đĩa kèm cơ chế fallback thông minh.
    Tự động hỗ trợ cả thư mục cấu hình động video_storage_path (ổ D:/...) và thư mục mặc định (ổ C:/...).
    """
    if not file_path:
        return ""
    
    # 1. Kiểm tra trực tiếp đường dẫn nếu tồn tại
    if os.path.isabs(file_path) and os.path.exists(file_path):
        return file_path
        
    direct_rel = os.path.join(settings.BASE_DIR, file_path)
    if os.path.exists(direct_rel):
        return direct_rel

    from disk_cleanup import get_recordings_base_dir
    configured_rec_dir = get_recordings_base_dir()

    # 2. Tìm kiếm thông minh nếu đường dẫn gốc từ máy khác hoặc thay đổi mount drive
    norm_path = file_path.replace("\\", "/")
    markers = ["storage/recordings/", "recordings/"]
    for marker in markers:
        if marker in norm_path:
            sub = norm_path.split(marker, 1)[1]
            for base in [configured_rec_dir, settings.RECORDINGS_DIR, os.path.join(settings.STORAGE_DIR, "recordings")]:
                candidate = os.path.join(base, sub)
                if os.path.exists(candidate):
                    return candidate

    # 3. Thử tìm theo tên file trong folder recordings đã cấu hình
    filename = os.path.basename(file_path)
    for search_dir in [configured_rec_dir, settings.RECORDINGS_DIR]:
        if os.path.exists(search_dir):
            for root, dirs, files in os.walk(search_dir):
                if filename in files:
                    return os.path.join(root, filename)

    return os.path.join(settings.BASE_DIR, file_path) if not os.path.isabs(file_path) else file_path

def ensure_h264_playable(abs_path: str) -> str:
    """Tự động kiểm tra và chuyển đổi file MP4 sang chuẩn H.264 (+faststart) nếu file chưa tương thích trình duyệt"""
    if not abs_path or not os.path.exists(abs_path):
        return abs_path

    # Kiểm tra bộ nhớ cache xác thực
    from video_recorder import _h264_ready_files
    if abs_path in _h264_ready_files or abs_path in _h264_verified_paths:
        return abs_path

    with _transcode_lock:
        if abs_path in _transcoding_in_progress:
            return abs_path
        _transcoding_in_progress.add(abs_path)

    try:
        try:
            import imageio_ffmpeg
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            ffmpeg_exe = "ffmpeg"

        cap = cv2.VideoCapture(abs_path)
        fourcc_int = int(cap.get(cv2.CAP_PROP_FOURCC)) if cap.isOpened() else 0
        fourcc_str = ''.join([chr((fourcc_int >> 8 * i) & 0xFF) for i in range(4)]).lower()
        cap.release()
        
        # Nếu là FMP4 / mp4v không xem được trên web -> transcode nhanh
        if fourcc_str in ['fmp4', 'mp4v', 'xvid', 'divx', '']:
            temp_path = f"{abs_path}.h264tmp_{os.getpid()}_{threading.get_ident()}.mp4"
            cmd = [
                ffmpeg_exe, "-y",
                "-i", abs_path,
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-crf", "23",
                "-g", "12",
                "-keyint_min", "6",
                "-sc_threshold", "0",
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                temp_path
            ]
            creationflags = 0
            if os.name == 'nt' and hasattr(subprocess, 'CREATE_NO_WINDOW'):
                creationflags = subprocess.CREATE_NO_WINDOW

            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags)
            if res.returncode == 0 and os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
                try:
                    os.replace(temp_path, abs_path)
                    logger.info(f"Auto-transcoded on-demand to H.264 (+faststart): {abs_path}")
                except Exception as replace_err:
                    logger.warning(f"Could not replace file after transcode (file locked): {replace_err}")
            else:
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except Exception:
                        pass
        _h264_verified_paths.add(abs_path)
    except Exception as e:
        logger.warning(f"ensure_h264_playable note for {abs_path}: {e}")
    finally:
        with _transcode_lock:
            _transcoding_in_progress.discard(abs_path)

    return abs_path

@router.get("/timeline", response_model=PlaybackTimelineResponse)
def get_playback_timeline(
    camera_id: int,
    date: Optional[str] = None,
    request: Request = None,
    db: Session = Depends(get_db)
):
    """
    Lấy toàn bộ dữ liệu timeline 24 giờ của 1 camera trong 1 ngày cụ thể:
    - Danh sách các phân đoạn video ghi hình (Video Recording blocks)
    - Danh sách các mốc sự kiện nhận diện biển số xe (Vehicle Logs)
    """
    current_user = get_current_user_optional(request, db)
    if not check_camera_access(camera_id, current_user):
        raise HTTPException(status_code=403, detail="Tài khoản của bạn không có quyền xem camera này.")

    cam = db.query(Camera).filter(Camera.id == camera_id).first()
    if not cam:
        raise HTTPException(status_code=404, detail="Không tìm thấy Camera.")

    target_date_str = date.strip() if date and date.strip() else datetime.now().strftime("%Y-%m-%d")
    try:
        target_date = datetime.strptime(target_date_str, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Định dạng ngày không hợp lệ (cần YYYY-MM-DD).")

    day_start = datetime.combine(target_date, datetime.min.time())
    day_end = datetime.combine(target_date, datetime.max.time())

    # 1. Lấy các bản ghi video phân đoạn (loại bỏ các đoạn rác 0 frame / rỗng)
    recordings = db.query(VideoRecording).filter(
        VideoRecording.camera_id == camera_id,
        VideoRecording.start_time >= day_start,
        VideoRecording.start_time <= day_end,
        VideoRecording.file_size_mb > 0.01
    ).order_by(VideoRecording.start_time.asc()).all()

    blocks: List[TimelineBlock] = []
    for rec in recordings:
        start_sec = max(0.0, (rec.start_time - day_start).total_seconds())
        if rec.end_time:
            end_sec = min(86400.0, (rec.end_time - day_start).total_seconds())
        else:
            end_sec = min(86400.0, start_sec + (rec.duration_seconds or 300))
        
        duration = max(1, int(end_sec - start_sec))
        blocks.append(TimelineBlock(
            id=rec.id,
            start_time=rec.start_time.strftime("%Y-%m-%d %H:%M:%S"),
            end_time=rec.end_time.strftime("%Y-%m-%d %H:%M:%S") if rec.end_time else (rec.start_time + timedelta(seconds=duration)).strftime("%Y-%m-%d %H:%M:%S"),
            start_seconds=round(start_sec, 2),
            end_seconds=round(end_sec, 2),
            duration_seconds=duration,
            file_size_mb=rec.file_size_mb or 0.0
        ))

    # 2. Lấy các mốc sự kiện nhận diện biển số xe trong ngày
    logs = db.query(VehicleLog).filter(
        VehicleLog.camera_id == camera_id,
        VehicleLog.detected_at >= day_start,
        VehicleLog.detected_at <= day_end,
        VehicleLog.is_deleted == False
    ).order_by(VehicleLog.detected_at.asc()).all()

    events: List[TimelineEvent] = []
    for log in logs:
        sec_in_day = (log.detected_at - day_start).total_seconds()
        img_url = f"/static/captures/{log.image_full_path}" if log.image_full_path else ""
        events.append(TimelineEvent(
            id=log.id,
            detected_at=log.detected_at.strftime("%Y-%m-%d %H:%M:%S"),
            seconds_in_day=round(sec_in_day, 2),
            plate_number=log.plate_number,
            action_status=log.action_status,
            alert_level=log.alert_level or "normal",
            image_full_url=img_url
        ))

    return PlaybackTimelineResponse(
        camera_id=cam.id,
        camera_name=cam.camera_name,
        date=target_date_str,
        total_recordings=len(blocks),
        total_events=len(events),
        blocks=blocks,
        events=events
    )

@router.get("/stream/{recording_id}")
def stream_recording_video(
    recording_id: int, 
    request: Request, 
    db: Session = Depends(get_db)
):
    """
    Stream video phân đoạn MP4 hỗ trợ chuẩn HTTP 206 Partial Content (Range Requests).
    Cho phép trình duyệt tua nhanh đến bất kỳ giây nào mà không cần tải lại toàn bộ file.
    """
    rec = db.query(VideoRecording).filter(VideoRecording.id == recording_id).first()
    if not rec:
        raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi Video.")

    current_user = get_current_user_optional(request, db)
    if not check_camera_access(rec.camera_id, current_user):
        raise HTTPException(status_code=403, detail="Tài khoản không có quyền xem video camera này.")

    raw_path = open_video_file_absolute(rec.file_path)
    if not os.path.exists(raw_path):
        raise HTTPException(status_code=404, detail="File video không tồn tại trên ổ đĩa.")

    abs_path = ensure_h264_playable(raw_path)
    file_size = os.path.getsize(abs_path)
    range_header = request.headers.get("range", "").strip()

    if not range_header:
        return FileResponse(
            path=abs_path,
            media_type="video/mp4",
            headers={"Accept-Ranges": "bytes"}
        )

    # Xử lý Range Request: bytes=start-end
    range_match = re.search(r"bytes=(\d+)-(\d*)", range_header)
    if not range_match:
        return FileResponse(
            path=abs_path,
            media_type="video/mp4",
            headers={"Accept-Ranges": "bytes"}
        )

    start_str, end_str = range_match.groups()
    start = int(start_str)
    end = int(end_str) if end_str else file_size - 1

    if start >= file_size:
        raise HTTPException(
            status_code=416, 
            detail="Requested range not satisfiable",
            headers={"Content-Range": f"bytes */{file_size}"}
        )

    end = min(end, file_size - 1)
    chunk_size = end - start + 1

    def iterfile():
        with open(abs_path, mode="rb") as f:
            f.seek(start)
            bytes_left = chunk_size
            while bytes_left > 0:
                read_amount = min(bytes_left, 256 * 1024)
                data = f.read(read_amount)
                if not data:
                    break
                bytes_left -= len(data)
                yield data

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(chunk_size),
        "Content-Type": "video/mp4",
    }

    return StreamingResponse(iterfile(), status_code=206, headers=headers)

@router.get("/locate")
def locate_recording_by_time(
    camera_id: int,
    timestamp: str,
    request: Request = None,
    db: Session = Depends(get_db)
):
    """
    Tìm file video phân đoạn chứa một mốc thời gian timestamp cụ thể (hoặc file gần nhất)
    để player tự động tải và nhảy tới đúng giây cần xem.
    Loại bỏ triệt để các bản ghi rác cũ và hỗ trợ chốt file tức thì nếu đang ghi hình.
    """
    current_user = get_current_user_optional(request, db)
    if not check_camera_access(camera_id, current_user):
        raise HTTPException(status_code=403, detail="Tài khoản không có quyền xem camera này.")

    try:
        target_dt = parse_iso_or_str_datetime(timestamp)
    except ValueError:
        raise HTTPException(status_code=400, detail="Thời gian timestamp không đúng định dạng.")

    day_start = datetime.combine(target_dt.date(), datetime.min.time())
    day_end = datetime.combine(target_dt.date(), datetime.max.time())

    # 1. Tìm file đã hoàn tất bao trùm target_dt
    rec = db.query(VideoRecording).filter(
        VideoRecording.camera_id == camera_id,
        VideoRecording.start_time <= target_dt,
        VideoRecording.end_time >= target_dt,
        VideoRecording.status == "completed",
        VideoRecording.file_size_mb > 0.01
    ).order_by(VideoRecording.start_time.desc()).first()

    # 2. Nếu không thấy, kiểm tra xem có phải sự kiện nằm trong file đang ghi hình hiện tại hay không
    if not rec:
        active_rec = db.query(VideoRecording).filter(
            VideoRecording.camera_id == camera_id,
            VideoRecording.status == "recording",
            VideoRecording.start_time <= target_dt,
            VideoRecording.start_time >= (target_dt - timedelta(minutes=15))
        ).order_by(VideoRecording.start_time.desc()).first()

        if active_rec:
            try:
                from video_recorder import video_recorder_manager
                video_recorder_manager.rotate_camera_segment(camera_id, wait_timeout=1.5)
                db.refresh(active_rec)
            except Exception as e:
                logger.warning(f"Error rotating segment on locate: {e}")
            rec = active_rec

    # 3. Nếu vẫn không thấy, tìm file gần nhất trong cùng ngày
    if not rec:
        closest_before = db.query(VideoRecording).filter(
            VideoRecording.camera_id == camera_id,
            VideoRecording.start_time >= day_start,
            VideoRecording.start_time <= target_dt,
            VideoRecording.status == "completed",
            VideoRecording.file_size_mb > 0.01
        ).order_by(VideoRecording.start_time.desc()).first()

        closest_after = db.query(VideoRecording).filter(
            VideoRecording.camera_id == camera_id,
            VideoRecording.start_time >= target_dt,
            VideoRecording.start_time <= day_end,
            VideoRecording.status.in_(["completed", "recording"]),
            VideoRecording.file_size_mb > 0.01
        ).order_by(VideoRecording.start_time.asc()).first()

        if closest_before and closest_after:
            end_b = closest_before.end_time or closest_before.start_time
            diff_before = abs((target_dt - end_b).total_seconds())
            diff_after = abs((closest_after.start_time - target_dt).total_seconds())
            rec = closest_after if diff_after < diff_before else closest_before
        elif closest_before:
            rec = closest_before
        elif closest_after:
            rec = closest_after

    if not rec:
        raise HTTPException(
            status_code=404, 
            detail=f"Không tìm thấy đoạn video nào cho camera #{camera_id} vào ngày {target_dt.strftime('%d/%m/%Y')} quanh thời điểm {target_dt.strftime('%H:%M:%S')}."
        )

    # 4. Tính toán offset giây cần tua (lùi 3s để thấy xe đang chạy vào)
    if target_dt >= rec.start_time:
        raw_offset = (target_dt - rec.start_time).total_seconds()
        seek_offset_seconds = max(0.0, raw_offset - 3.0)
    else:
        seek_offset_seconds = 0.0

    if rec.duration_seconds and rec.duration_seconds > 0:
        if seek_offset_seconds >= rec.duration_seconds:
            seek_offset_seconds = max(0.0, float(rec.duration_seconds - 1.0))

    return {
        "recording_id": rec.id,
        "camera_id": rec.camera_id,
        "start_time": rec.start_time.strftime("%Y-%m-%d %H:%M:%S"),
        "end_time": rec.end_time.strftime("%Y-%m-%d %H:%M:%S") if rec.end_time else None,
        "duration_seconds": rec.duration_seconds or 0,
        "stream_url": f"/api/v1/playback/stream/{rec.id}",
        "seek_offset_seconds": round(seek_offset_seconds, 2)
    }

def _fast_ffmpeg_trim_and_export(
    recordings_info: List[dict],
    start_dt: datetime,
    end_dt: datetime,
    output_abs_path: str,
    target_fps: int = 6
) -> bool:
    """Cắt và ghép nối video siêu tốc bằng FFmpeg (chuẩn 100% H.264 AVC + faststart)"""
    try:
        import imageio_ffmpeg
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        ffmpeg_exe = "ffmpeg"

    creationflags = 0
    if os.name == 'nt' and hasattr(subprocess, 'CREATE_NO_WINDOW'):
        creationflags = subprocess.CREATE_NO_WINDOW

    # TH1: Cắt trong 1 file phân đoạn duy nhất (95% trường hợp)
    if len(recordings_info) == 1:
        rec = recordings_info[0]
        abs_file = rec["abs_file"]
        rec_start = rec["rec_start"]

        offset_start = max(0.0, (start_dt - rec_start).total_seconds())
        clip_duration = max(1.0, (end_dt - start_dt).total_seconds())

        cmd = [
            ffmpeg_exe, "-y",
            "-ss", f"{offset_start:.2f}",
            "-t", f"{clip_duration:.2f}",
            "-i", abs_file,
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            output_abs_path
        ]
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags)
        return res.returncode == 0 and os.path.exists(output_abs_path) and os.path.getsize(output_abs_path) > 1024

    # TH2: Cắt giao thoa giữa nhiều file phân đoạn rồi ghép lại bằng ffmpeg concat
    temp_parts = []
    try:
        for idx, rec in enumerate(recordings_info):
            abs_file = rec["abs_file"]
            rec_start = rec["rec_start"]
            rec_end = rec["rec_end"]

            part_start = max(start_dt, rec_start)
            part_end = min(end_dt, rec_end)
            if part_start >= part_end:
                continue

            offset_sec = max(0.0, (part_start - rec_start).total_seconds())
            duration_sec = max(0.5, (part_end - part_start).total_seconds())
            part_path = f"{output_abs_path}.part_{idx}.mp4"

            cmd = [
                ffmpeg_exe, "-y",
                "-ss", f"{offset_sec:.2f}",
                "-t", f"{duration_sec:.2f}",
                "-i", abs_file,
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-crf", "23",
                "-pix_fmt", "yuv420p",
                part_path
            ]
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags)
            if res.returncode == 0 and os.path.exists(part_path) and os.path.getsize(part_path) > 0:
                temp_parts.append(part_path)

        if not temp_parts:
            return False

        list_txt_path = f"{output_abs_path}.concat_list.txt"
        with open(list_txt_path, "w", encoding="utf-8") as f:
            for p in temp_parts:
                clean_p = p.replace("\\", "/")
                f.write(f"file '{clean_p}'\n")

        cmd_concat = [
            ffmpeg_exe, "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", list_txt_path,
            "-c", "copy",
            "-movflags", "+faststart",
            output_abs_path
        ]
        res = subprocess.run(cmd_concat, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags)
        
        try:
            os.remove(list_txt_path)
        except Exception:
            pass
        for p in temp_parts:
            try:
                os.remove(p)
            except Exception:
                pass

        return res.returncode == 0 and os.path.exists(output_abs_path) and os.path.getsize(output_abs_path) > 1024
    except Exception as e:
        logger.error(f"Error in _fast_ffmpeg_trim_and_export: {e}")
        return False

@router.post("/export", response_model=VideoExportResponse)
def export_video_clip(
    export_in: VideoExportRequest,
    request: Request = None,
    db: Session = Depends(get_db)
):
    """
    Cắt và trích xuất một đoạn video MP4 siêu tốc bằng FFmpeg theo khoảng thời gian tùy chọn [Start Time - End Time].
    """
    current_user = get_current_user_optional(request, db)
    if not check_camera_access(export_in.camera_id, current_user):
        raise HTTPException(status_code=403, detail="Tài khoản không có quyền trích xuất video camera này.")

    cam = db.query(Camera).filter(Camera.id == export_in.camera_id).first()
    if not cam:
        raise HTTPException(status_code=404, detail="Không tìm thấy Camera.")

    try:
        start_dt = parse_iso_or_str_datetime(export_in.start_time)
        end_dt = parse_iso_or_str_datetime(export_in.end_time)
    except ValueError:
        raise HTTPException(status_code=400, detail="Định dạng thời gian bắt đầu hoặc kết thúc không hợp lệ.")

    if start_dt >= end_dt:
        raise HTTPException(status_code=400, detail="Thời gian bắt đầu phải nhỏ hơn thời gian kết thúc.")

    total_req_duration = (end_dt - start_dt).total_seconds()
    if total_req_duration > 7200:
        raise HTTPException(status_code=400, detail="Thời lượng trích xuất tối đa là 2 giờ (120 phút).")

    # Tìm các file phân đoạn giao thoa với khoảng thời gian [start_dt, end_dt]
    recordings = db.query(VideoRecording).filter(
        VideoRecording.camera_id == export_in.camera_id,
        VideoRecording.start_time <= end_dt,
        or_(VideoRecording.end_time >= start_dt, VideoRecording.end_time.is_(None))
    ).order_by(VideoRecording.start_time.asc()).all()

    if not recordings:
        raise HTTPException(status_code=404, detail="Không tìm thấy dữ liệu video trong khoảng thời gian đã chọn.")

    # Thu thập danh sách file hợp lệ
    rec_info_list = []
    for rec in recordings:
        abs_file = open_video_file_absolute(rec.file_path)
        if os.path.exists(abs_file) and os.path.getsize(abs_file) > 10240:
            rec_end = rec.end_time or (rec.start_time + timedelta(seconds=rec.duration_seconds or 300))
            rec_info_list.append({
                "abs_file": abs_file,
                "rec_start": rec.start_time,
                "rec_end": rec_end,
                "duration_sec": rec.duration_seconds or int((rec_end - rec.start_time).total_seconds())
            })

    if not rec_info_list:
        raise HTTPException(status_code=404, detail="Các file video nguồn không tồn tại trên ổ đĩa.")

    # Chuẩn bị file kết quả
    exports_dir = settings.EXPORTS_DIR
    os.makedirs(exports_dir, exist_ok=True)
    filename = f"clip_cam{export_in.camera_id}_{start_dt.strftime('%Y%m%d_%H%M%S')}_{end_dt.strftime('%Y%m%d_%H%M%S')}.mp4"
    output_abs_path = os.path.join(exports_dir, filename)

    # 1. Thực hiện cắt ghép siêu tốc bằng FFmpeg
    success = _fast_ffmpeg_trim_and_export(
        rec_info_list,
        start_dt,
        end_dt,
        output_abs_path,
        target_fps=cam.target_fps or 6
    )

    # 2. Fallback OpenCV nếu FFmpeg không cắt được
    if not success or not os.path.exists(output_abs_path):
        target_fps = cam.target_fps if cam.target_fps > 0 else 6
        writer = None
        total_written = 0

        for r in rec_info_list:
            cap = cv2.VideoCapture(r["abs_file"])
            if not cap.isOpened():
                continue
            file_fps = cap.get(cv2.CAP_PROP_FPS) or target_fps
            sec_start = max(0.0, (start_dt - r["rec_start"]).total_seconds())
            sec_end = min(r["duration_sec"], (end_dt - r["rec_start"]).total_seconds())

            cap.set(cv2.CAP_PROP_POS_FRAMES, int(sec_start * file_fps))
            curr = int(sec_start * file_fps)
            end_f = int(sec_end * file_fps)

            while curr <= end_f:
                ret, frame = cap.read()
                if not ret or frame is None:
                    break
                h, w = frame.shape[:2]
                if writer is None:
                    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                    writer = cv2.VideoWriter(output_abs_path, fourcc, float(target_fps), (w, h))
                writer.write(frame)
                total_written += 1
                curr += 1
            cap.release()

        if writer is not None:
            writer.release()
            # Convert fallback sang H.264
            ensure_h264_playable(output_abs_path)

    if not os.path.exists(output_abs_path) or os.path.getsize(output_abs_path) == 0:
        raise HTTPException(status_code=500, detail="Không thể trích xuất khung hình từ các file video nguồn.")

    file_size_mb = round(os.path.getsize(output_abs_path) / (1024 * 1024), 2)
    duration_sec = int(total_req_duration)
    rel_output = os.path.relpath(output_abs_path, settings.BASE_DIR).replace("\\", "/")

    # Lưu bản ghi VideoExportJob vào DB
    job = VideoExportJob(
        camera_id=export_in.camera_id,
        title=export_in.title or f"Clip Camera {cam.camera_name} ({start_dt.strftime('%H:%M:%S')} - {end_dt.strftime('%H:%M:%S')})",
        start_time=start_dt,
        end_time=end_dt,
        output_file_path=rel_output,
        file_size_mb=file_size_mb,
        duration_seconds=duration_sec,
        status="completed"
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    return VideoExportResponse(
        id=job.id,
        camera_id=job.camera_id,
        title=job.title,
        start_time=job.start_time,
        end_time=job.end_time,
        output_file_path=job.output_file_path,
        download_url=f"/api/v1/playback/download/{job.id}",
        file_size_mb=job.file_size_mb,
        duration_seconds=job.duration_seconds,
        status=job.status,
        created_at=job.created_at,
        success=True,
        message=f"Đã xuất đoạn video thành công ({file_size_mb} MB)"
    )

@router.get("/exports", response_model=List[VideoExportResponse])
def get_exported_clips(
    camera_id: Optional[int] = None,
    limit: int = 20,
    db: Session = Depends(get_db)
):
    """Lấy danh sách các clip video đã trích xuất gần đây"""
    query = db.query(VideoExportJob)
    if camera_id and camera_id > 0:
        query = query.filter(VideoExportJob.camera_id == camera_id)

    jobs = query.order_by(VideoExportJob.created_at.desc()).limit(limit).all()
    results = []
    for job in jobs:
        results.append(VideoExportResponse(
            id=job.id,
            camera_id=job.camera_id,
            title=job.title,
            start_time=job.start_time,
            end_time=job.end_time,
            output_file_path=job.output_file_path,
            download_url=f"/api/v1/playback/download/{job.id}",
            file_size_mb=job.file_size_mb or 0.0,
            duration_seconds=job.duration_seconds or 0,
            status=job.status or "completed",
            created_at=job.created_at,
            success=True,
            message="OK"
        ))
    return results

@router.get("/download/{export_id}")
def download_exported_clip(export_id: int, db: Session = Depends(get_db)):
    """Tải file MP4 clip đã trích xuất về máy tính / điện thoại"""
    job = db.query(VideoExportJob).filter(VideoExportJob.id == export_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Không tìm thấy Clip đã xuất.")

    abs_path = open_video_file_absolute(job.output_file_path)
    if not os.path.exists(abs_path):
        raise HTTPException(status_code=404, detail="File clip không tồn tại trên máy chủ.")

    filename = os.path.basename(abs_path)
    return FileResponse(
        path=abs_path,
        media_type="video/mp4",
        filename=filename
    )

@router.delete("/exports/{export_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_exported_clip(export_id: int, db: Session = Depends(get_db)):
    """Xóa một clip đã xuất khỏi hệ thống và ổ đĩa"""
    job = db.query(VideoExportJob).filter(VideoExportJob.id == export_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Không tìm thấy Clip đã xuất.")

    abs_path = open_video_file_absolute(job.output_file_path)
    if os.path.exists(abs_path):
        try:
            os.remove(abs_path)
        except Exception:
            pass

    db.delete(job)
    db.commit()
    return None

@router.get("/storage-status", response_model=StorageStatusResponse)
def get_playback_storage_status(db: Session = Depends(get_db)):
    """Lấy thông tin tình trạng ổ đĩa lưu trữ video và cấu hình dọn dẹp tự động"""
    from disk_cleanup import get_recordings_base_dir
    rec_dir = get_recordings_base_dir()
    sys_cfg = settings_manager.get_settings()

    total_gb = 0.0
    used_gb = 0.0
    free_gb = 0.0
    used_percent = 0.0

    if os.path.exists(rec_dir):
        try:
            total, used, free = shutil.disk_usage(rec_dir)
            total_gb = round(total / (1024 ** 3), 2)
            used_gb = round(used / (1024 ** 3), 2)
            free_gb = round(free / (1024 ** 3), 2)
            used_percent = round((used / total) * 100, 1)
        except Exception:
            pass

    total_files = 0
    total_size_bytes = 0
    if os.path.exists(rec_dir):
        for root, dirs, files in os.walk(rec_dir):
            for f in files:
                if f.endswith(".mp4"):
                    total_files += 1
                    try:
                        total_size_bytes += os.path.getsize(os.path.join(root, f))
                    except Exception:
                        pass

    rec_size_gb = round(total_size_bytes / (1024 ** 3), 2)

    return StorageStatusResponse(
        storage_path=rec_dir,
        total_gb=total_gb,
        used_gb=used_gb,
        free_gb=free_gb,
        used_percent=used_percent,
        recordings_size_gb=rec_size_gb,
        recordings_count=total_files,
        retention_days=sys_cfg.get("video_retention_days", 15),
        auto_cleanup_disk=bool(sys_cfg.get("auto_cleanup_disk", True)),
        is_disk_low=bool(used_percent >= 90.0 or free_gb < 10.0)
    )
