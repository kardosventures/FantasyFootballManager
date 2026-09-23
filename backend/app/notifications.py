from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import NotificationDelivery


def _read_trash_talk_image(image_path: str, reports_dir: Path) -> tuple[Path, bytes]:
    path = Path(image_path).resolve()
    reports_root = reports_dir.resolve()
    if reports_root not in path.parents or not path.is_file():
        raise RuntimeError("Trash-talk image path is outside the reports directory")
    return path, path.read_bytes()


async def queue_dashboard_notice(
    session: AsyncSession,
    *,
    subject: str,
    body: str,
    dedupe_key: str,
    action_id: str | None = None,
    severity: str = "info",
) -> NotificationDelivery:
    """Persist an always-available local notification for the private dashboard."""
    existing = await session.scalar(
        select(NotificationDelivery).where(NotificationDelivery.dedupe_key == dedupe_key)
    )
    if existing:
        return existing
    delivery = NotificationDelivery(
        action_id=action_id,
        channel="dashboard",
        recipient="local-owner",
        subject=subject,
        payload={"body": body, "severity": severity},
        dedupe_key=dedupe_key,
        status="delivered",
        delivered_at=datetime.now(timezone.utc),
    )
    session.add(delivery)
    return delivery


async def queue_slack_alert(
    session: AsyncSession,
    settings: Settings,
    *,
    subject: str,
    body: str,
    dedupe_key: str,
    action_id: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> NotificationDelivery:
    existing = await session.scalar(
        select(NotificationDelivery).where(NotificationDelivery.dedupe_key == dedupe_key)
    )
    if existing:
        return existing
    screenshot_count = sum(
        1
        for item in attachments or []
        if isinstance(item, dict) and item.get("data_base64")
    )
    if screenshot_count:
        body += (
            "\n\nScreenshot evidence was captured locally and is available in the "
            "private dashboard execution record."
        )
    delivery = NotificationDelivery(
        action_id=action_id,
        channel="slack",
        recipient=settings.slack_channel_label,
        subject=(
            subject
            if subject.startswith("SLEEPER AI AGENT: ")
            else f"SLEEPER AI AGENT: {subject}"
        )[:255],
        payload={"body": body, "screenshot_count": screenshot_count},
        dedupe_key=dedupe_key,
        status="pending" if settings.slack_notifications_configured else "blocked",
        last_error=(
            None if settings.slack_notifications_configured else "Slack webhook is not configured"
        ),
    )
    session.add(delivery)
    return delivery


async def queue_trash_talk(
    session: AsyncSession,
    settings: Settings,
    *,
    body: str,
    dedupe_key: str,
    image_path: str | None = None,
    alt_text: str | None = None,
) -> NotificationDelivery:
    """Queue entertainment separately so it cannot consume failure-alert policy."""
    existing = await session.scalar(
        select(NotificationDelivery).where(NotificationDelivery.dedupe_key == dedupe_key)
    )
    if existing:
        return existing
    configured = settings.trash_talk_notifications_configured or (
        bool(image_path) and settings.slack_image_upload_configured
    )
    delivery = NotificationDelivery(
        channel="slack",
        kind="trash_talk",
        recipient=settings.trash_talk_channel_label,
        subject="JIM.AI TRASH TALK",
        payload={
            "body": body,
            "image_path": image_path,
            "alt_text": alt_text,
            "severity": "fun",
        },
        dedupe_key=dedupe_key,
        status="pending" if configured else "blocked",
        last_error=None if configured else "Slack trash-talk delivery is not configured",
    )
    session.add(delivery)
    return delivery


async def deliver_slack_alert(delivery: NotificationDelivery, settings: Settings) -> None:
    webhook = settings.slack_webhook_url
    if not settings.slack_notifications_configured or webhook is None:
        delivery.status = "blocked"
        delivery.last_error = "Slack webhook is not configured"
        return
    delivery.attempt_count = int(delivery.attempt_count or 0) + 1
    try:
        text = f":rotating_light: *{delivery.subject}*\n{delivery.payload.get('body') or ''}"
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                webhook.get_secret_value(),
                json={"text": text[:35_000]},
            )
        if response.status_code != 200 or response.text.strip().lower() != "ok":
            raise RuntimeError(f"Slack webhook failed with HTTP {response.status_code}")
        delivery.status = "delivered"
        delivery.delivered_at = datetime.now(timezone.utc)
        delivery.last_error = None
    except Exception as exc:
        delivery.status = "failed"
        delivery.last_error = str(exc)[:4000]
        raise


async def deliver_trash_talk(delivery: NotificationDelivery, settings: Settings) -> None:
    """Upload an original card when possible, otherwise post the approved text once."""
    body = str((delivery.payload or {}).get("body") or "")
    image_path = str((delivery.payload or {}).get("image_path") or "")
    delivery.attempt_count = int(delivery.attempt_count or 0) + 1
    try:
        if image_path and settings.slack_image_upload_configured:
            path, data = await asyncio.to_thread(
                _read_trash_talk_image, image_path, settings.reports_dir
            )
            token = settings.slack_bot_token
            assert token is not None and settings.slack_channel_id is not None
            headers = {"Authorization": f"Bearer {token.get_secret_value()}"}
            async with httpx.AsyncClient(timeout=20) as client:
                ticket = await client.post(
                    "https://slack.com/api/files.getUploadURLExternal",
                    headers=headers,
                    data={"filename": path.name, "length": str(len(data))},
                )
                ticket.raise_for_status()
                ticket_payload = ticket.json()
                if not ticket_payload.get("ok"):
                    raise RuntimeError("Slack refused the image upload ticket")
                upload = await client.post(
                    str(ticket_payload["upload_url"]),
                    content=data,
                    headers={"Content-Type": "image/png"},
                )
                upload.raise_for_status()
                complete = await client.post(
                    "https://slack.com/api/files.completeUploadExternal",
                    headers=headers,
                    json={
                        "files": [{"id": ticket_payload["file_id"], "title": "Jim.ai scoreboard"}],
                        "channel_id": settings.slack_channel_id,
                        "initial_comment": body,
                    },
                )
                complete.raise_for_status()
                complete_payload = complete.json()
                if not complete_payload.get("ok"):
                    raise RuntimeError("Slack did not complete the image upload")
        elif settings.trash_talk_notifications_configured:
            webhook = settings.trash_talk_webhook
            assert webhook is not None
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    webhook.get_secret_value(),
                    json={"text": body[:35_000]},
                )
            if response.status_code != 200 or response.text.strip().lower() != "ok":
                raise RuntimeError(f"Slack webhook failed with HTTP {response.status_code}")
        else:
            raise RuntimeError("Slack trash-talk delivery is not configured")
        delivery.status = "delivered"
        delivery.delivered_at = datetime.now(timezone.utc)
        delivery.last_error = None
    except Exception as exc:
        # An image failure gets exactly one text fallback in the same attempt.
        fallback_attempted = bool((delivery.payload or {}).get("fallback_attempted"))
        if image_path and settings.trash_talk_notifications_configured and not fallback_attempted:
            delivery.payload = {**delivery.payload, "fallback_attempted": True}
            try:
                webhook = settings.trash_talk_webhook
                assert webhook is not None
                async with httpx.AsyncClient(timeout=20) as client:
                    response = await client.post(
                        webhook.get_secret_value(),
                        json={"text": body[:35_000]},
                    )
                if response.status_code == 200 and response.text.strip().lower() == "ok":
                    delivery.status = "delivered"
                    delivery.delivered_at = datetime.now(timezone.utc)
                    delivery.last_error = "Image upload failed; delivered text fallback"
                    delivery.payload = {**delivery.payload, "fallback_format": "text"}
                    return
            except Exception:
                pass
        delivery.status = "failed"
        delivery.last_error = str(exc)[:4000]
        raise


async def queue_manager_notices(
    session: AsyncSession, settings: Settings, report: dict
) -> list[NotificationDelivery]:
    """Publish material manager alerts locally and mirror actual failures to Slack."""
    queued: list[NotificationDelivery] = []
    decision = report.get("decision") or {}
    notices: list[tuple[str, str, str, str]] = []
    if report.get("manager_state") == "blocked" and report.get("blockers"):
        blockers = [str(item).strip() for item in report["blockers"] if str(item).strip()]
        if blockers:
            body = (
                "Issue type: FANTASY MANAGER BLOCKED\n"
                f"Observed at: {report.get('generated_at') or datetime.now(timezone.utc).isoformat()}\n"
                "Manager state: blocked\n"
                "Blocking conditions:\n- "
                + "\n- ".join(blockers)
                + "\n\nRequired response: inspect the dashboard before any manual correction."
            )
            notices.append(("Fantasy manager blocked", body, "warning", "manager-blocked"))
    for index, alert in enumerate(decision.get("urgent_alerts") or []):
        body = str(alert).strip()
        if body:
            notices.append(("Urgent fantasy alert", body, "warning", f"urgent:{index}"))
    ir_plan = report.get("ir_plan") or {}
    for row in ir_plan.get("blocked") or []:
        if row.get("action_type") == "MOVE_TO_IR" and int(
            ir_plan.get("reserve_capacity") or 0
        ) > 0:
            notices.append(
                (
                    "IR opportunity needs attention",
                    f"{row.get('player_name')}: {row.get('reason')}",
                    "warning",
                    f"ir:{row.get('player_id')}:{row.get('reason')}",
                )
            )
    promotion = ((report.get("intelligence") or {}).get("promotion") or {})
    for component in ("projection", "championship"):
        row = promotion.get(component) or {}
        if row.get("eligible") and not row.get("authoritative"):
            notices.append(
                (
                    f"{component.title()} intelligence qualified",
                    "Empirical checks passed; the explicit master switch remains off.",
                    "success",
                    f"promotion:{component}",
                )
            )
    for subject, body, severity, key in notices:
        digest = hashlib.sha256(f"{subject}\n{body}".encode()).hexdigest()[:24]
        dedupe = f"manager:{key}:{digest}"
        queued.append(
            await queue_dashboard_notice(
                session,
                subject=subject,
                body=body,
                severity=severity,
                dedupe_key=f"dashboard:{dedupe}",
            )
        )
        # Slack only an actual manager failure here. Urgent roster intelligence,
        # IR opportunities, success, and readiness updates stay on the dashboard.
        if key == "manager-blocked":
            queued.append(
                await queue_slack_alert(
                    session,
                    settings,
                    subject=subject,
                    body=body,
                    dedupe_key=f"slack:{dedupe}",
                )
            )
    return queued
