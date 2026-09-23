from __future__ import annotations

import asyncio
import os
import signal
import socket
from datetime import datetime, timezone

import structlog
from sqlalchemy import case, func, select

from app.bootstrap import bootstrap_live, sync_draft_picks, sync_rankings
from app.config import get_settings
from app.database import session_scope
from app.models import NotificationDelivery, TrashTalkPost
from app.notifications import deliver_slack_alert, deliver_trash_talk
from app.queue import complete_event, lease_event, retry_event

logger = structlog.get_logger()


async def _process_event(worker_id: str) -> bool:
    async with session_scope() as session:
        event = await lease_event(session, worker_id=worker_id)
        if not event:
            return False
        try:
            if event.event_type == "SYNC_SLEEPER":
                await bootstrap_live()
            elif event.event_type == "SYNC_DRAFT_PICKS":
                await sync_draft_picks()
            elif event.event_type == "SYNC_RANKINGS":
                await sync_rankings()
            else:
                raise ValueError(f"Unknown event type {event.event_type}")
            complete_event(event)
        except Exception as exc:
            retry_event(event, exc)
            logger.exception("event_failed", event_id=event.id, error=str(exc))
        return True


async def _process_slack_alert() -> bool:
    settings = get_settings()
    eligible_statuses = ["pending", "failed"]
    if settings.slack_notifications_configured:
        eligible_statuses.append("blocked")
    async with session_scope() as session:
        delivery = await session.scalar(
            select(NotificationDelivery)
            .where(
                NotificationDelivery.channel == "slack",
                NotificationDelivery.status.in_(eligible_statuses),
                NotificationDelivery.attempt_count < 8,
            )
            .order_by(
                case((NotificationDelivery.kind == "operational", 0), else_=1),
                NotificationDelivery.created_at,
            )
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if not delivery:
            return False
        if delivery.kind == "operational":
            start_of_day = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            delivered_today = await session.scalar(
                select(func.count())
                .select_from(NotificationDelivery)
                .where(
                    NotificationDelivery.channel == "slack",
                    NotificationDelivery.kind == "operational",
                    NotificationDelivery.status == "delivered",
                    NotificationDelivery.delivered_at >= start_of_day,
                )
            )
            if int(delivered_today or 0) >= settings.slack_daily_alert_limit:
                delivery.status = "suppressed"
                delivery.last_error = "Daily Slack failure-alert limit reached"
                return True
        try:
            if delivery.kind == "trash_talk":
                await deliver_trash_talk(delivery, settings)
            else:
                await deliver_slack_alert(delivery, settings)
        except Exception as exc:
            logger.warning("slack_delivery_failed", delivery_id=delivery.id, error=str(exc))
        post = await session.scalar(
            select(TrashTalkPost).where(
                TrashTalkPost.notification_delivery_id == delivery.id
            )
        )
        if post is not None:
            post.status = delivery.status
            post.delivered_at = delivery.delivered_at
            post.suppression_reason = delivery.last_error if delivery.status != "delivered" else None
        return True


async def run_worker() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    while not stop.is_set():
        handled = await _process_slack_alert()
        handled = await _process_event(worker_id) or handled
        if not handled:
            try:
                await asyncio.wait_for(stop.wait(), timeout=2)
            except TimeoutError:
                pass


if __name__ == "__main__":
    asyncio.run(run_worker())
