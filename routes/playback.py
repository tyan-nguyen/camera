import os
import re
import cv2
import logging
from datetime import datetime, timedelta
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, or_

from database import get_db, SessionLocal
from config import settings
from models import Camera, VideoRecording, VideoExportJob, VehicleLog, UserRoleEnum, User
from schemas import (
    PlaybackTimelineResponse, 
    TimelineBlock, 
    TimelineEvent, 
    VideoRecordingResponse, 
    VideoExportRequest, 
    VideoExportResponse
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

    # 1. Lấy các bản ghi video phân đoạn
    recordings = db.query(VideoRecording).filter(
        VideoRecording.camera_id == camera_id,
        VideoRecording.start_time >= day_start,
        VideoRecording.start_time <= day_end
    ).order_by(VideoRecording.start_time.asc()).all()

    blocks: List[TimelineBlock] = []
    for rec in recordings:
        start_sec = max(0.0, (rec.start_time - day_start).total_seconds())
        if rec.end_time:
            end_sec = min(86400.0, (rec.end_time - day_start).total_seconds())
        else:
            end_sec = min(86400.0, start_sec + (rec.duration_seconds or 300))
        
        duration = int(end_sec - start_sec)
        blocks.append(TimelineBlock(
            id=rec.id,
            start_time=rec.start_time.strftime("%Y-%m-%d %H:%M:%S"),
            end_time=rec.end_time.strftime("%Y-%m-%d %H:%M:%S") if rec.end_time else (rec.start_time + timedelta(seconds=duration)).strftime("%Y-%m-%d %H:%M:%S"),
            start_seconds=round(start_sec, 2),
            end_seconds=round(end_sec, 2),
            duration_seconds=max(1, duration),
            file_size_mb=rec.file_size_mb or 0.0
        ))

    # 2. Lấy các mốc sự kiện nhận diện biển số xe trong ngày
    logs = db.query(VehicleLog).filter(
        VehicleLog.camera_id == camera_id,
        VehicleLog.detected_at >= day_start,
        VehicleLog.detected_at <= day_end
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

def open_video_file_absolute(file_path: str) -> str:
    """Chuẩn hóa đường dẫn file video tuyệt đối trên ổ đĩa"""
    if os.path.isabs(file_path):
        abs_path = file_path
    else:
        abs_path = os.path.join(settings.BASE_DIR, file_path)
    return abs_path

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

    abs_path = open_video_file_absolute(rec.file_path)
    if not os.path.exists(abs_path):
        raise HTTPException(status_code=404, detail="File video không tồn tại trên ổ đĩa.")

    file_size = os.path.getsize(abs_path)
    range_header = request.headers.get("range", "").strip()

    if not range_header:
        # Stream thông thường nếu không có range header
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
                read_amount = min(bytes_left, 64 * 1024)
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
    """
    current_user = get_current_user_optional(request, db)
    if not check_camera_access(camera_id, current_user):
        raise HTTPException(status_code=403, detail="Tài khoản không có quyền xem camera này.")

    try:
        target_dt = parse_iso_or_str_datetime(timestamp)
    except ValueError:
        raise HTTPException(status_code=400, detail="Thời gian timestamp không đúng định dạng.")

    # Tìm file bao trùm target_dt
    rec = db.query(VideoRecording).filter(
        VideoRecording.camera_id == camera_id,
        VideoRecording.start_time <= target_dt,
        or_(VideoRecording.end_time >= target_dt, VideoRecording.end_time.is_(None))
    ).order_by(VideoRecording.start_time.desc()).first()

    # Nếu không thấy đoạn bao trùm, tìm đoạn gần nhất trước target_dt
    if not rec:
        rec = db.query(VideoRecording).filter(
            VideoRecording.camera_id == camera_id,
            VideoRecording.start_time <= target_dt
        ).order_by(VideoRecording.start_time.desc()).first()

    # Nếu vẫn không thấy, tìm đoạn gần nhất sau target_dt
    if not rec:
        rec = db.query(VideoRecording).filter(
            VideoRecording.camera_id == camera_id,
            VideoRecording.start_time >= target_dt
        ).order_by(VideoRecording.start_time.asc()).first()

    if not rec:
        raise HTTPException(status_code=404, detail="Không tìm thấy video nào tại mốc thời gian này.")

    seek_offset_seconds = max(0.0, (target_dt - rec.start_time).total_seconds())

    return {
        "recording_id": rec.id,
        "camera_id": rec.camera_id,
        "start_time": rec.start_time.strftime("%Y-%m-%d %H:%M:%S"),
        "end_time": rec.end_time.strftime("%Y-%m-%d %H:%M:%S") if rec.end_time else None,
        "duration_seconds": rec.duration_seconds,
        "stream_url": f"/api/v1/playback/stream/{rec.id}",
        "seek_offset_seconds": round(seek_offset_seconds, 2)
    }

@router.post("/export", response_model=VideoExportResponse)
def export_video_clip(
    export_in: VideoExportRequest,
    request: Request = None,
    db: Session = Depends(get_db)
):
    """
    Cắt và trích xuất một đoạn video MP4 theo khoảng thời gian tùy chọn [Start Time - End Time].
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
    if total_req_duration > 7200:  # Giới hạn tối đa 2 tiếng cho 1 lần trích xuất
        raise HTTPException(status_code=400, detail="Thời lượng trích xuất tối đa là 2 giờ (120 phút).")

    # Tìm các file phân đoạn giao thoa với khoảng thời gian [start_dt, end_dt]
    recordings = db.query(VideoRecording).filter(
        VideoRecording.camera_id == export_in.camera_id,
        VideoRecording.start_time <= end_dt,
        or_(VideoRecording.end_time >= start_dt, VideoRecording.end_time.is_(None))
    ).order_by(VideoRecording.start_time.asc()).all()

    if not recordings:
        raise HTTPException(status_code=404, detail="Không tìm thấy dữ liệu video trong khoảng thời gian đã chọn.")

    # Chuẩn bị file kết quả
    exports_dir = settings.EXPORTS_DIR
    os.makedirs(exports_dir, exist_ok=True)
    filename = f"clip_cam{export_in.camera_id}_{start_dt.strftime('%Y%m%d_%H%M%S')}_{end_dt.strftime('%Y%m%d_%H%M%S')}.mp4"
    output_abs_path = os.path.join(exports_dir, filename)

    # Đọc và cắt ghép các frame từ các file phân đoạn
    writer = None
    target_fps = cam.target_fps if cam.target_fps > 0 else 6
    total_written_frames = 0
    frame_size = None

    for rec in recordings:
        abs_file = open_video_file_absolute(rec.file_path)
        if not os.path.exists(abs_file):
            continue

        cap = cv2.VideoCapture(abs_file)
        if not cap.isOpened():
            continue

        file_fps = cap.get(cv2.CAP_PROP_FPS) or target_fps
        rec_start = rec.start_time
        
        # Tính offset cần đọc trong file này
        sec_offset_start = max(0.0, (start_dt - rec_start).total_seconds()) if start_dt > rec_start else 0.0
        sec_offset_end = (end_dt - rec_start).total_seconds() if end_dt < (rec.end_time or rec_start + timedelta(seconds=rec.duration_seconds or 300)) else (rec.duration_seconds or 300)

        start_frame_idx = int(sec_offset_start * file_fps)
        end_frame_idx = int(sec_offset_end * file_fps)

        cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame_idx)
        current_frame_idx = start_frame_idx

        while current_frame_idx <= end_frame_idx:
            ret, frame = cap.read()
            if not ret or frame is None:
                break
            
            h, w = frame.shape[:2]
            if writer is None:
                frame_size = (w, h)
                fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                writer = cv2.VideoWriter(output_abs_path, fourcc, float(target_fps), (w, h))

            writer.write(frame)
            total_written_frames += 1
            current_frame_idx += 1

        cap.release()

    if writer is not None:
        writer.release()

    if total_written_frames == 0 or not os.path.exists(output_abs_path):
        raise HTTPException(status_code=500, detail="Không thể trích xuất khung hình từ các file video nguồn.")

    file_size_mb = round(os.path.getsize(output_abs_path) / (1024 * 1024), 2)
    duration_sec = int(total_written_frames / target_fps) if target_fps > 0 else int(total_req_duration)

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
        created_at=job.created_at
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
            created_at=job.created_at
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
