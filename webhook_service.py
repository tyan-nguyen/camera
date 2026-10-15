import json
import logging
import threading
import time
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Tuple, List

from database import SessionLocal
from models import ActionGroup, VehicleLog, WebhookSyncQueue, Camera
from websocket_manager import websocket_manager

logger = logging.getLogger(__name__)

class WebhookCircuitBreaker:
    """
    Bộ ngắt mạch (Circuit Breaker) cho các Webhook API đối tác.
    Trạng thái:
      - CLOSED: Bình thường, API online, cho phép gửi dữ liệu.
      - OPEN: Ngắt mạch khi API gặp lỗi liên tiếp (>= 3 lần). Bỏ qua gửi HTTP (fail-fast), đưa thẳng vào hàng đợi.
      - HALF_OPEN: Hết thời gian cooldown (30s) hoặc khi ping thấy tín hiệu hồi phục, cho phép gửi 1 request thử nghiệm.
    """
    def __init__(self, failure_threshold: int = 3, cooldown_seconds: int = 30):
        self._lock = threading.Lock()
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        # dict key: action_group_id -> metadata
        self._groups: Dict[int, Dict[str, Any]] = {}

    def _get_entry(self, ag_id: int) -> Dict[str, Any]:
        if ag_id not in self._groups:
            self._groups[ag_id] = {
                "state": "CLOSED",               # CLOSED, OPEN, HALF_OPEN
                "status": "ONLINE",              # ONLINE, OFFLINE, DEGRADED
                "consecutive_failures": 0,
                "last_failure_time": None,
                "last_success_time": None,
                "last_health_check_time": None,
                "last_latency_ms": None,
                "last_error": None
            }
        return self._groups[ag_id]

    def can_execute(self, ag_id: int) -> bool:
        """Kiểm tra xem có thể gửi request ngay tới ActionGroup này không"""
        with self._lock:
            entry = self._get_entry(ag_id)
            state = entry["state"]

            if state == "CLOSED":
                return True

            if state == "OPEN":
                # Kiểm tra xem đã qua thời gian cooldown chưa
                last_fail = entry["last_failure_time"]
                if last_fail:
                    elapsed = (datetime.utcnow() - last_fail).total_seconds()
                    if elapsed >= self.cooldown_seconds:
                        entry["state"] = "HALF_OPEN"
                        logger.info(f"[CircuitBreaker] ActionGroup #{ag_id} cooldown ({self.cooldown_seconds}s) expired -> HALF_OPEN (probing...)")
                        return True
                return False

            if state == "HALF_OPEN":
                # Cho phép 1 request thử nghiệm
                return True

            return True

    def record_success(self, ag_id: int, latency_ms: float = 0.0):
        """Ghi nhận request hoặc ping thành công"""
        with self._lock:
            entry = self._get_entry(ag_id)
            prev_status = entry["status"]
            entry["state"] = "CLOSED"
            entry["status"] = "ONLINE"
            entry["consecutive_failures"] = 0
            entry["last_success_time"] = datetime.utcnow()
            entry["last_health_check_time"] = datetime.utcnow()
            entry["last_latency_ms"] = round(latency_ms, 1)
            entry["last_error"] = None

        if prev_status != "ONLINE":
            logger.info(f"[CircuitBreaker] ActionGroup #{ag_id} restored to ONLINE (latency: {latency_ms:.1f}ms).")
            self._broadcast_status_change(ag_id)

    def record_failure(self, ag_id: int, error_msg: str):
        """Ghi nhận request hoặc ping thất bại"""
        trigger_broadcast = False
        with self._lock:
            entry = self._get_entry(ag_id)
            prev_status = entry["status"]
            entry["consecutive_failures"] += 1
            entry["last_failure_time"] = datetime.utcnow()
            entry["last_health_check_time"] = datetime.utcnow()
            entry["last_error"] = str(error_msg)[:500]

            if entry["consecutive_failures"] >= self.failure_threshold:
                if entry["state"] != "OPEN":
                    entry["state"] = "OPEN"
                    entry["status"] = "OFFLINE"
                    trigger_broadcast = True
                    logger.warning(
                        f"[CircuitBreaker] ActionGroup #{ag_id} tripped OPEN after {entry['consecutive_failures']} consecutive errors. "
                        f"Error: {error_msg}"
                    )
            else:
                entry["status"] = "DEGRADED"

        if trigger_broadcast or (prev_status == "ONLINE" and entry["status"] != "ONLINE"):
            self._broadcast_status_change(ag_id)

    def force_reset(self, ag_id: int):
        with self._lock:
            entry = self._get_entry(ag_id)
            entry["state"] = "CLOSED"
            entry["status"] = "ONLINE"
            entry["consecutive_failures"] = 0
            entry["last_error"] = None
        self._broadcast_status_change(ag_id)

    def get_info(self, ag_id: int) -> Dict[str, Any]:
        with self._lock:
            return dict(self._get_entry(ag_id))

    def _broadcast_status_change(self, ag_id: int):
        """Bắn WebSocket thông báo trạng thái thay đổi tới giao diện Web"""
        try:
            db = SessionLocal()
            try:
                ag = db.query(ActionGroup).filter(ActionGroup.id == ag_id).first()
                if not ag:
                    return
                info = self.get_info(ag_id)
                pending_count = db.query(WebhookSyncQueue).filter(
                    WebhookSyncQueue.action_group_id == ag_id,
                    WebhookSyncQueue.status == "PENDING"
                ).count()

                payload = {
                    "event": "webhook_health_changed",
                    "data": {
                        "action_group_id": ag.id,
                        "action_group_name": ag.name,
                        "code": ag.code,
                        "api_url": ag.api_url,
                        "status": info["status"],
                        "circuit_state": info["state"],
                        "latency_ms": info["last_latency_ms"],
                        "last_error": info["last_error"],
                        "pending_count": pending_count
                    }
                }
                websocket_manager.broadcast_sync(payload)
            finally:
                db.close()
        except Exception as e:
            logger.debug(f"Broadcast webhook health error: {e}")

# Global singleton
webhook_circuit_breaker = WebhookCircuitBreaker()

def test_action_group_health(action_group: ActionGroup, timeout: float = 3.0) -> Tuple[bool, float, str]:
    """
    Thực hiện ping kiểm tra tính khả dụng của ActionGroup Webhook API.
    Trả về: (is_online, latency_ms, message)
    """
    if not action_group or not action_group.api_url:
        return False, 0.0, "API URL không tồn tại"

    api_url = action_group.api_url.strip()
    clean_url = api_url.replace("{plate}", "PINGTEST").replace("{plate_number}", "PINGTEST")
    
    # Nối thêm tham số ping để API phía đối tác có thể nhận biết và trả về 200 nhanh
    sep = "&" if "?" in clean_url else "?"
    ping_url = f"{clean_url}{sep}_ping=1"

    headers = {
        "User-Agent": "ANPR-HealthCheck/2.0",
        "Accept": "*/*"
    }
    if action_group.auth_token and action_group.auth_token.strip():
        tok = action_group.auth_token.strip()
        auth_type = (action_group.auth_type or "BEARER").upper()
        if auth_type == "BEARER":
            headers["Authorization"] = f"Bearer {tok}"
        elif auth_type == "API_KEY":
            headers["X-API-Key"] = tok

    start_t = time.time()
    try:
        req = urllib.request.Request(ping_url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            latency = (time.time() - start_t) * 1000
            msg = f"HTTP {resp.status}"
            webhook_circuit_breaker.record_success(action_group.id, latency_ms=latency)
            return True, latency, msg
    except urllib.error.HTTPError as e:
        latency = (time.time() - start_t) * 1000
        # Nếu server trả về 401, 403, 404, 405 hoặc 422: server đích vẫn đang SỐNG và phản hồi HTTP!
        if e.code in [200, 201, 204, 400, 401, 403, 404, 405, 422]:
            webhook_circuit_breaker.record_success(action_group.id, latency_ms=latency)
            return True, latency, f"HTTP {e.code} (Server phản hồi tốt)"
        else:
            msg = f"HTTP Error {e.code}: {e.reason}"
            webhook_circuit_breaker.record_failure(action_group.id, msg)
            return False, latency, msg
    except Exception as e:
        latency = (time.time() - start_t) * 1000
        msg = f"Lỗi kết nối: {e}"
        webhook_circuit_breaker.record_failure(action_group.id, msg)
        return False, latency, msg

def enqueue_webhook_event(
    db,
    action_group_id: int,
    log_id: Optional[int],
    camera_id: Optional[int],
    event_action: str,
    event_id: str,
    plate_number: Optional[str],
    payload_dict: Dict[str, Any],
    initial_error: Optional[str] = None
) -> WebhookSyncQueue:
    """
    Lưu sự kiện vào bảng webhook_sync_queue (Transactional Outbox) để chờ gửi lại.
    Đồng thời cập nhật trạng thái VehicleLog tương ứng thành PENDING_SYNC.
    """
    now = datetime.utcnow()
    # Thời gian thử lại lần đầu: sau 15 giây
    next_retry = now + timedelta(seconds=15)

    queue_item = WebhookSyncQueue(
        action_group_id=action_group_id,
        log_id=log_id,
        camera_id=camera_id,
        event_action=event_action.upper(),
        event_id=event_id,
        plate_number=str(plate_number).upper() if plate_number else None,
        payload_json=json.dumps(payload_dict, ensure_ascii=False),
        status="PENDING",
        retry_count=0,
        max_retries=10,
        next_retry_at=next_retry,
        last_error=initial_error,
        created_at=now
    )
    db.add(queue_item)

    if log_id:
        log_item = db.query(VehicleLog).filter(VehicleLog.id == log_id).first()
        if log_item:
            log_item.sync_status = "PENDING_SYNC"
            log_item.sync_error = initial_error

    db.commit()
    db.refresh(queue_item)
    logger.info(
        f"[WebhookQueue] Enqueued #{queue_item.id} for ActionGroup #{action_group_id}, "
        f"action={event_action}, plate={plate_number}, log_id={log_id}. Next retry: {next_retry.strftime('%H:%M:%S')}"
    )

    # Phát thông báo WebSocket tới Web UI
    try:
        websocket_manager.broadcast_sync({
            "event": "webhook_queue_updated",
            "data": {
                "queue_id": queue_item.id,
                "action_group_id": action_group_id,
                "log_id": log_id,
                "plate_number": plate_number,
                "event_action": event_action,
                "status": "PENDING"
            }
        })
    except Exception:
        pass

    return queue_item
