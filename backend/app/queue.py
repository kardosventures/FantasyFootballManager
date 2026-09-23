from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DomainEvent


async def enqueue_event(
    session: AsyncSession,
    *,
    event_type: str,
    entity_type: str,
    entity_id: str,
    dedupe_key: str,
    payload: dict[str, Any] | None = None,
    priority: int = 2,
    not_before: datetime | None = None,
) -> DomainEvent:
    existing = await session.scalar(select(DomainEvent).where(DomainEvent.dedupe_key == dedupe_key))
    if existing:
        return existing
    event = DomainEvent(
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        dedupe_key=dedupe_key,
        payload=payload or {},
        priority=priority,
        not_before=not_before or datetime.now(timezone.utc),
    )
    session.add(event)
    return event


async def lease_event(
    session: AsyncSession, *, worker_id: str, lease_seconds: int = 60
) -> DomainEvent | None:
    now = datetime.now(timezone.utc)
    event = await session.scalar(
        select(DomainEvent)
        .where(
            DomainEvent.status.in_(["pending", "leased"]),
            DomainEvent.not_before <= now,
            (DomainEvent.expires_at.is_(None) | (DomainEvent.expires_at > now)),
            (DomainEvent.lease_until.is_(None) | (DomainEvent.lease_until < now)),
        )
        .order_by(DomainEvent.priority, DomainEvent.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if event:
        event.status = "leased"
        event.lease_owner = worker_id
        event.lease_until = now + timedelta(seconds=lease_seconds)
        event.attempt_count += 1
    return event


def complete_event(event: DomainEvent) -> None:
    event.status = "completed"
    event.completed_at = datetime.now(timezone.utc)
    event.lease_owner = None
    event.lease_until = None


def retry_event(event: DomainEvent, error: Exception, *, max_attempts: int = 8) -> None:
    event.last_error = str(error)[:4000]
    event.lease_owner = None
    event.lease_until = None
    if event.attempt_count >= max_attempts:
        event.status = "failed"
        return
    event.status = "pending"
    event.not_before = datetime.now(timezone.utc) + timedelta(
        seconds=min(2**event.attempt_count, 900)
    )
