import logging
from datetime import datetime, date
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from database import get_db
from models import ActionGroup, Camera, WebhookSyncQueue, UserRoleEnum
from schemas import (
    WebhookSyncItemResponse,
    PaginatedWebhookSyncQueue,
    WebhookHealthStatusResponse,
    WebhookSystemStatusResponse
)
from auth_utils import require_roles, get_current_user
from webhook_service import webhook_circuit_breaker, test_action_group_health
from webhook_sync_worker import webhook_sync_worker

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/webhook-sync", tags=["Webhook Synchronization"])

admin_or_manager = require_roles([UserRoleEnum.ADMIN.value, UserRoleEnum.QUAN_LY.value])
admin_only = require_roles([UserRoleEnum.ADMIN.value])

@router.get("/status", response_model=WebhookSystemStatusResponse)
def get_webhook_system_status(db: Session = Depends(get_db)):
    """
    Lấy thông tin tổng quan trạng thái Webhook:
    - Sức khỏe từng ActionGroup (Online/Offline, Độ trễ ms, Trạng thái Circuit Breaker)
    - Tổng số bản ghi đang chờ đồng bộ, đã lỗi, đã đồng bộ thành công hôm nay
    """
    active_groups = db.query(ActionGroup).order_by(ActionGroup.id.asc()).all()
    ag_responses: List[WebhookHealthStatusResponse] = []
    has_offline = False

    for ag in active_groups:
        info = webhook_circuit_breaker.get_info(ag.id)
        pending_cnt = db.query(WebhookSyncQueue).filter(
            WebhookSyncQueue.action_group_id == ag.id,
            WebhookSyncQueue.status == "PENDING"
        ).count()

        if info["status"] != "ONLINE" and ag.is_active:
            has_offline = True

        ag_responses.append(WebhookHealthStatusResponse(
            action_group_id=ag.id,
            action_group_name=ag.name,
            code=ag.code,
            api_url=ag.api_url,
            http_method=ag.http_method or "POST",
            is_active=ag.is_active,
            status=info["status"],
            circuit_state=info["state"],
            circuit_breaker_state=info["state"],
            latency_ms=info["last_latency_ms"],
            last_response_time_ms=info["last_latency_ms"],
            last_checked_at=info["last_health_check_time"],
            last_error=info["last_error"],
            consecutive_failures=info["consecutive_failures"],
            pending_count=pending_cnt
        ))

    total_pending = db.query(WebhookSyncQueue).filter(WebhookSyncQueue.status == "PENDING").count()
    total_failed = db.query(WebhookSyncQueue).filter(WebhookSyncQueue.status == "FAILED").count()
    
    # Đếm số bản ghi đồng bộ thành công trong ngày hôm nay
    today_start = datetime.combine(date.today(), datetime.min.time())
    total_synced_today = db.query(WebhookSyncQueue).filter(
        WebhookSyncQueue.status == "SUCCESS",
        WebhookSyncQueue.synced_at >= today_start
    ).count()

    online_count = sum(1 for it in ag_responses if it.status == "ONLINE" and it.is_active)
    offline_count = sum(1 for it in ag_responses if it.status != "ONLINE" and it.is_active)
    open_circuit_count = sum(1 for it in ag_responses if it.circuit_state == "OPEN")

    return WebhookSystemStatusResponse(
        total_pending=total_pending,
        pending_count=total_pending,
        total_failed=total_failed,
        failed_count=total_failed,
        total_synced_today=total_synced_today,
        has_offline_webhook=has_offline,
        total_action_groups=len(ag_responses),
        online_count=online_count,
        offline_count=offline_count,
        open_circuit_count=open_circuit_count,
        action_groups=ag_responses
    )

@router.get("/queue", response_model=PaginatedWebhookSyncQueue)
def get_webhook_queue_items(
    status_filter: Optional[str] = Query(None, alias="status"),
    action_group_id: Optional[int] = None,
    event_action: Optional[str] = None,
    plate: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db)
):
    """
    Lấy danh sách các bản ghi trong hàng đợi đồng bộ Webhook (phân trang và lọc)
    """
    query = db.query(WebhookSyncQueue)

    if isinstance(status_filter, str) and status_filter.strip():
        query = query.filter(WebhookSyncQueue.status == status_filter.strip().upper())
    if isinstance(action_group_id, int):
        query = query.filter(WebhookSyncQueue.action_group_id == action_group_id)
    if isinstance(event_action, str) and event_action.strip():
        query = query.filter(WebhookSyncQueue.event_action == event_action.strip().upper())
    if isinstance(plate, str) and plate.strip():
        query = query.filter(WebhookSyncQueue.plate_number.like(f"%{plate.strip().upper()}%"))

    p = page if isinstance(page, int) else 1
    ps = page_size if isinstance(page_size, int) else 20

    total = query.count()
    items = query.order_by(WebhookSyncQueue.id.desc()).offset((p - 1) * ps).limit(ps).all()

    # Pre-fetch ActionGroup and Camera names
    ag_map = {ag.id: ag.name for ag in db.query(ActionGroup.id, ActionGroup.name).all()}
    cam_map = {c.id: c.camera_name for c in db.query(Camera.id, Camera.camera_name).all()}

    response_items: List[WebhookSyncItemResponse] = []
    for it in items:
        resp = WebhookSyncItemResponse.model_validate(it)
        resp.action_group_name = ag_map.get(it.action_group_id, f"Nhóm #{it.action_group_id}")
        resp.camera_name = cam_map.get(it.camera_id) if it.camera_id else None
        response_items.append(resp)

    return PaginatedWebhookSyncQueue(
        total=total,
        page=p,
        page_size=ps,
        items=response_items
    )

@router.post("/retry-all")
def retry_all_pending_and_failed(
    db: Session = Depends(get_db),
    current_user=Depends(admin_or_manager)
):
    """
    Thử lại ngay lập tức tất cả các bản ghi đang PENDING hoặc FAILED
    Đồng thời reset trạng thái ngắt mạch (Circuit Breaker)
    """
    now = datetime.utcnow()
    updated_cnt = (
        db.query(WebhookSyncQueue)
        .filter(WebhookSyncQueue.status.in_(["PENDING", "FAILED"]))
        .update(
            {
                WebhookSyncQueue.status: "PENDING",
                WebhookSyncQueue.next_retry_at: now
            },
            synchronize_session=False
        )
    )
    db.commit()

    # Reset circuit breaker cho toàn bộ ActionGroups
    active_groups = db.query(ActionGroup).all()
    for ag in active_groups:
        webhook_circuit_breaker.force_reset(ag.id)

    # Đánh thức sync worker
    webhook_sync_worker.trigger_now()

    logger.info(f"User {current_user.username} triggered retry-all for {updated_cnt} webhook queue items.")
    return {
        "success": True,
        "retried_count": updated_cnt,
        "message": f"Đã kích hoạt đồng bộ lại cho {updated_cnt} bản ghi trong hàng đợi."
    }

@router.post("/{queue_id}/retry")
def retry_single_queue_item(
    queue_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(admin_or_manager)
):
    """
    Thử lại ngay lập tức 1 bản ghi cụ thể trong hàng đợi
    """
    item = db.query(WebhookSyncQueue).filter(WebhookSyncQueue.id == queue_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi trong hàng đợi.")

    item.status = "PENDING"
    item.next_retry_at = datetime.utcnow()
    db.commit()

    webhook_circuit_breaker.force_reset(item.action_group_id)
    webhook_sync_worker.trigger_now()

    return {"success": True, "message": f"Đã đưa bản ghi #{queue_id} ({item.plate_number}) vào hàng đợi đồng bộ ngay."}

@router.delete("/{queue_id}")
def cancel_queue_item(
    queue_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(admin_only)
):
    """
    Hủy bỏ (bỏ qua) một bản ghi lỗi không cần đồng bộ nữa (Chỉ Admin)
    """
    item = db.query(WebhookSyncQueue).filter(WebhookSyncQueue.id == queue_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi trong hàng đợi.")

    item.status = "CANCELLED"
    item.last_error = f"Hủy thủ công bởi admin {current_user.username}"
    db.commit()

    return {"success": True, "message": f"Đã hủy bản ghi #{queue_id} khỏi hàng đợi đồng bộ."}

@router.post("/check-health")
def trigger_manual_health_check(
    db: Session = Depends(get_db),
    current_user=Depends(admin_or_manager)
):
    """
    Kiểm tra sức khỏe (Ping trực tiếp) tất cả các ActionGroup Webhook API ngay lập tức
    """
    active_groups = db.query(ActionGroup).filter(ActionGroup.is_active == True).all()
    results = []

    for ag in active_groups:
        is_online, latency, msg = test_action_group_health(ag, timeout=3.0)
        results.append({
            "action_group_id": ag.id,
            "action_group_name": ag.name,
            "is_online": is_online,
            "latency_ms": round(latency, 1),
            "message": msg
        })

    # Nếu có API nào online trở lại, kích hoạt kiểm tra queue ngay
    webhook_sync_worker.trigger_now()

    online_count = sum(1 for r in results if r["is_online"])
    offline_count = len(results) - online_count

    return {
        "success": True,
        "total_checked": len(results),
        "online_count": online_count,
        "offline_count": offline_count,
        "results": results
    }
