import c_env_helper
import os
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
from auth_utils import decode_access_token
from routes import camera, history, settings as settings_route, tts, auth, users, action_groups, devices, playback, webhook_sync, search_shortcuts, external_api
from settings_manager import settings_manager
from webhook_sync_worker import webhook_sync_worker
from video_recorder import recover_orphaned_recordings
from disk_cleanup import disk_cleanup_scheduler
from event_video_manager import event_video_manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# System components
ai_engine = AIPipelineEngine(settings.YOLO_MODEL_PATH)
disk_worker = AsyncDiskWorker(websocket_manager=websocket_manager)
stream_manager = DynamicStreamManager(ai_engine=ai_engine, disk_worker=disk_worker)

# Inject stream manager reference into camera routes
camera.set_stream_manager(stream_manager)
external_api.set_stream_manager(stream_manager)
notification_service.set_websocket_manager(websocket_manager)

# Toàn bộ danh sách Camera được quản lý và load thực tế từ CSDL MySQL

def prevent_windows_sleep():
    """Ngăn Windows tự động đi vào trạng thái Sleep/Hibernate khi chạy server camera qua đêm"""
    import sys
    if sys.platform == "win32":
        try:
            import ctypes
            ES_CONTINUOUS = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            logger.info("Windows Sleep Prevention enabled: System will remain awake 24/7.")
        except Exception as e:
            logger.debug(f"Could not set Windows execution state: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    prevent_windows_sleep()
    logger.info("Starting ANPR Multi-Zone Monitoring System...")
    init_db()
    settings_manager.reload_from_db()
    ai_engine.load_model()
    
    websocket_manager.set_loop(asyncio.get_running_loop())
    notification_service.set_websocket_manager(websocket_manager)
    
    disk_worker.start()
    webhook_sync_worker.start()
    disk_cleanup_scheduler.start()
    recover_orphaned_recordings()
    stream_manager.start_all_active_cameras()
    
    try:
        yield
    finally:
        logger.info("Stopping ANPR System components...")
        try:
            disk_cleanup_scheduler.stop()
        except Exception as e:
            logger.warning(f"Error stopping disk_cleanup_scheduler: {e}")
        try:
            webhook_sync_worker.stop()
        except Exception as e:
            logger.warning(f"Error stopping webhook_sync_worker: {e}")
        try:
            event_video_manager.stop_all()
        except Exception as e:
            logger.warning(f"Error stopping event_video_manager: {e}")
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
app.include_router(webhook_sync.router)
app.include_router(search_shortcuts.router)
app.include_router(external_api.router, prefix="/api/external/v1", tags=["External Integration"])

@app.get("/", response_class=HTMLResponse)
async def get_index_page(request: Request):
    try:
        return templates.TemplateResponse(request=request, name="index.html")
    except TypeError:
        return templates.TemplateResponse("index.html", {"request": request})

@app.websocket("/ws/events")
async def websocket_events_endpoint(websocket: WebSocket):
    # Xác thực người dùng qua Cookie access_token hoặc query parameter ?token=
    token = websocket.cookies.get("access_token") or websocket.query_params.get("token")
    user_payload = decode_access_token(token) if token else None
    if not user_payload:
        # Từ chối kết nối nếu chưa đăng nhập hoặc token hết hạn (WS 1008: Policy Violation)
        await websocket.close(code=1008)
        return

    # Nếu vai trò là quan_sat, quan sát viên không cần nhận push sự kiện đếm xe
    if user_payload.get("role") == "quan_sat":
        await websocket.close(code=1000)
        return

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
