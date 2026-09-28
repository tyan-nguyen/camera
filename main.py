import os
# Cấu hình FFmpeg timeout 10 giây (10.000.000 microsecond) trước khi load bất kỳ module OpenCV nào
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|stimeout;10000000|timeout;10000000"

import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse

from config import settings
from database import init_db, SessionLocal
from models import Camera, ActionGroup
from ai_pipeline import AIPipelineEngine
from disk_worker import AsyncDiskWorker
from stream_manager import DynamicStreamManager
from websocket_manager import websocket_manager
from notification_service import notification_service
from routes import camera, history, settings as settings_route, tts, auth, users, action_groups, devices, playback
from settings_manager import settings_manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# System components
ai_engine = AIPipelineEngine(settings.YOLO_MODEL_PATH)
disk_worker = AsyncDiskWorker(websocket_manager=websocket_manager)
stream_manager = DynamicStreamManager(ai_engine=ai_engine, disk_worker=disk_worker)

# Inject stream manager reference into camera routes
camera.set_stream_manager(stream_manager)
notification_service.set_websocket_manager(websocket_manager)

def seed_default_cameras():
    db = SessionLocal()
    try:
        count = db.query(Camera).count()
        if count == 0:
            logger.info("Seeding default camera RTSP streams into database...")
            # Lấy action group mặc định
            first_ag = db.query(ActionGroup).first()
            ag_id = first_ag.id if first_ag else None

            default_cams = [
                Camera(
                    camera_name="Căn tin ra", 
                    rtsp_url="rtsp://admin:HAO@041298.@14.238.118.58:5541/Streaming/Channels/501", 
                    target_fps=6, 
                    is_active=True,
                    zone_code="TRUONGLAI",
                    zone_name="Trường Lái",
                    camera_function="ANPR",
                    action_group_id=ag_id
                ),
                Camera(
                    camera_name="Vào Căn tin", 
                    rtsp_url="rtsp://admin:HAO@041298.@14.238.118.58:5541/Streaming/Channels/101", 
                    target_fps=6, 
                    is_active=True,
                    zone_code="TRUONGLAI",
                    zone_name="Trường Lái",
                    camera_function="ANPR",
                    action_group_id=ag_id
                ),
                Camera(
                    camera_name="Bãi Xe Khu B", 
                    rtsp_url="rtsp://admin:HAO@041298.@14.238.118.58:5541/Streaming/Channels/601", 
                    target_fps=6, 
                    is_active=True,
                    zone_code="KHO_BAI",
                    zone_name="Kho Bãi",
                    camera_function="ANPR",
                    action_group_id=ag_id
                ),
                Camera(
                    camera_name="Cổng Quan Sát Toàn Cảnh", 
                    rtsp_url="rtsp://admin:admin123@192.168.1.103:554/stream1", 
                    target_fps=6, 
                    is_active=False,
                    zone_code="TRUONGLAI",
                    zone_name="Trường Lái",
                    camera_function="SURVEILLANCE"
                ),
                Camera(
                    camera_name="Kho Hàng Giám Sát", 
                    rtsp_url="rtsp://admin:admin123@192.168.1.104:554/stream1", 
                    target_fps=6, 
                    is_active=False,
                    zone_code="KHO_BAI",
                    zone_name="Kho Bãi",
                    camera_function="SURVEILLANCE"
                ),
            ]
            db.add_all(default_cams)
            db.commit()
    finally:
        db.close()

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting ANPR Multi-Zone Monitoring System...")
    init_db()
    settings_manager.reload_from_db()
    seed_default_cameras()
    ai_engine.load_model()
    
    websocket_manager.set_loop(asyncio.get_running_loop())
    notification_service.set_websocket_manager(websocket_manager)
    
    disk_worker.start()
    stream_manager.start_all_active_cameras()
    
    try:
        yield
    finally:
        logger.info("Stopping ANPR System components...")
        try:
            stream_manager.stop_all()
        except Exception as e:
            logger.warning(f"Error stopping stream_manager: {e}")
        try:
            disk_worker.stop()
        except Exception as e:
            logger.warning(f"Error stopping disk_worker: {e}")
        logger.info("ANPR System components stopped clean.")

app = FastAPI(
    title=settings.APP_NAME,
    version="2.0.0",
    lifespan=lifespan
)

# Mount static file directories
app.mount("/static/captures", StaticFiles(directory=settings.CAPTURES_DIR), name="captures")
app.mount("/static/recordings", StaticFiles(directory=settings.RECORDINGS_DIR), name="recordings")
app.mount("/static/exports", StaticFiles(directory=settings.EXPORTS_DIR), name="exports")

# Setup Jinja2 templates
templates = Jinja2Templates(directory=os.path.join(settings.BASE_DIR, "templates"))

# Include API routers
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(camera.router)
app.include_router(action_groups.router)
app.include_router(devices.router)
app.include_router(history.router)
app.include_router(playback.router)
app.include_router(settings_route.router)
app.include_router(tts.router)

@app.get("/", response_class=HTMLResponse)
async def get_index_page(request: Request):
    try:
        return templates.TemplateResponse(request=request, name="index.html")
    except TypeError:
        return templates.TemplateResponse("index.html", {"request": request})

@app.websocket("/ws/events")
async def websocket_events_endpoint(websocket: WebSocket):
    await websocket_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        websocket_manager.disconnect(websocket)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        access_log=False
    )
