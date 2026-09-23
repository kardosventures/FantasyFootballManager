import base64
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
import respx
from httpx import HTTPStatusError, Response
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.database import Base
from app.models import NotificationDelivery
from app.notifications import (
    deliver_slack_alert,
    deliver_trash_talk,
    queue_manager_notices,
    queue_slack_alert,
    queue_trash_talk,
)
from app.worker import _process_slack_alert

WEBHOOK_URL = "https://hooks.slack.com/services/T00000000/B00000000/secret"


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.mark.asyncio
async def test_failure_alert_has_required_subject_and_records_local_screenshot(
    session_factory,
):
    settings = Settings(
        _env_file=None,
        slack_webhook_url=WEBHOOK_URL,
        slack_channel_label="#sleeper-ai-alerts",
    )
    async with session_factory() as session, session.begin():
        delivery = await queue_slack_alert(
            session,
            settings,
            subject="UNVERIFIED ACCEPT TRADE",
            body="Detailed failure evidence",
            dedupe_key="failure-1",
            attachments=[
                {
                    "filename": "failure.jpg",
                    "content_type": "image/jpeg",
                    "data_base64": base64.b64encode(b"screenshot-bytes").decode(),
                }
            ],
        )

    assert delivery.status == "pending"
    assert delivery.channel == "slack"
    assert delivery.subject == "SLEEPER AI AGENT: UNVERIFIED ACCEPT TRADE"
    assert delivery.recipient == "#sleeper-ai-alerts"
    assert delivery.payload["screenshot_count"] == 1
    assert "available in the private dashboard" in delivery.payload["body"]
    assert "data_base64" not in json.dumps(delivery.payload)


@pytest.mark.asyncio
async def test_unconfigured_slack_alert_is_durable_and_blocked(session_factory):
    settings = Settings(_env_file=None)
    async with session_factory() as session, session.begin():
        delivery = await queue_slack_alert(
            session,
            settings,
            subject="FAILED WAIVER CLAIM",
            body="Failure details",
            dedupe_key="failure-2",
        )

    assert delivery.status == "blocked"
    assert delivery.subject == "SLEEPER AI AGENT: FAILED WAIVER CLAIM"
    assert delivery.last_error == "Slack webhook is not configured"


@pytest.mark.asyncio
async def test_manager_blocker_slack_alert_is_durable_without_webhook(session_factory):
    settings = Settings(_env_file=None)
    async with session_factory() as session, session.begin():
        deliveries = await queue_manager_notices(
            session,
            settings,
            {
                "generated_at": "2026-09-15T12:00:00Z",
                "manager_state": "blocked",
                "blockers": ["Browser observation is stale", "Roster could not be verified"],
                "decision": {},
            },
        )

    assert [item.channel for item in deliveries] == ["dashboard", "slack"]
    slack = deliveries[1]
    assert slack.status == "blocked"
    assert slack.subject == "SLEEPER AI AGENT: Fantasy manager blocked"
    assert "Issue type: FANTASY MANAGER BLOCKED" in slack.payload["body"]
    assert "Browser observation is stale" in slack.payload["body"]
    assert "Roster could not be verified" in slack.payload["body"]


@pytest.mark.asyncio
async def test_urgent_intelligence_stays_on_dashboard(session_factory):
    settings = Settings(
        _env_file=None,
        slack_webhook_url=WEBHOOK_URL,
        slack_channel_label="#sleeper-ai-alerts",
    )
    async with session_factory() as session, session.begin():
        deliveries = await queue_manager_notices(
            session,
            settings,
            {
                "generated_at": "2026-09-15T12:00:00Z",
                "manager_state": "ready",
                "blockers": [],
                "decision": {"urgent_alerts": ["Monitor a questionable starter"]},
            },
        )

    assert [item.channel for item in deliveries] == ["dashboard"]


@pytest.mark.asyncio
@respx.mock
async def test_slack_delivery_posts_only_to_configured_channel_webhook(session_factory):
    settings = Settings(
        _env_file=None,
        slack_webhook_url=WEBHOOK_URL,
        slack_channel_label="#sleeper-ai-alerts",
    )
    webhook_request = respx.post(WEBHOOK_URL).mock(return_value=Response(200, text="ok"))
    async with session_factory() as session, session.begin():
        delivery = await queue_slack_alert(
            session,
            settings,
            subject="FAILED LINEUP CHANGE",
            body="Detailed failure evidence",
            dedupe_key="slack-post-only",
        )
        await deliver_slack_alert(delivery, settings)

    assert delivery.status == "delivered"
    assert webhook_request.call_count == 1
    payload = json.loads(webhook_request.calls[0].request.content)
    assert payload == {
        "text": (
            ":rotating_light: *SLEEPER AI AGENT: FAILED LINEUP CHANGE*\n"
            "Detailed failure evidence"
        )
    }
    assert webhook_request.calls[0].request.method == "POST"


def test_slack_webhook_must_use_official_post_only_host():
    with pytest.raises(ValidationError, match="official hooks.slack.com/services URL"):
        Settings(
            _env_file=None,
            slack_webhook_url="https://example.com/services/T/B/secret",
        )


def test_slack_image_upload_requires_bot_token_and_channel_id_shapes():
    with pytest.raises(ValidationError, match="beginning with xoxb-"):
        Settings(_env_file=None, slack_bot_token="not-a-bot-token")
    with pytest.raises(ValidationError, match="channel ID must begin with C"):
        Settings(_env_file=None, slack_channel_id="general")


def test_trash_talk_target_cannot_exceed_hard_limit():
    with pytest.raises(ValidationError, match="weekly target cannot exceed"):
        Settings(_env_file=None, trash_talk_weekly_target=8, trash_talk_weekly_limit=6)


@pytest.mark.asyncio
async def test_worker_suppresses_alert_after_daily_limit(session_factory, monkeypatch):
    settings = Settings(
        _env_file=None,
        slack_webhook_url=WEBHOOK_URL,
        slack_daily_alert_limit=1,
    )

    @asynccontextmanager
    async def test_session_scope():
        async with session_factory() as session, session.begin():
            yield session

    monkeypatch.setattr("app.worker.get_settings", lambda: settings)
    monkeypatch.setattr("app.worker.session_scope", test_session_scope)
    async with session_factory() as session, session.begin():
        session.add_all(
            [
                NotificationDelivery(
                    channel="slack",
                    recipient="sleeper-ai-alerts",
                    subject="SLEEPER AI AGENT: PRIOR FAILURE",
                    payload={"body": "prior"},
                    dedupe_key="prior-slack-alert",
                    status="delivered",
                    delivered_at=datetime.now(timezone.utc),
                ),
                NotificationDelivery(
                    channel="slack",
                    recipient="sleeper-ai-alerts",
                    subject="SLEEPER AI AGENT: NEW FAILURE",
                    payload={"body": "new"},
                    dedupe_key="new-slack-alert",
                    status="pending",
                ),
            ]
        )

    assert await _process_slack_alert() is True
    async with session_factory() as session:
        pending = await session.scalar(
            select(NotificationDelivery).where(
                NotificationDelivery.dedupe_key == "new-slack-alert"
            )
        )
    assert pending.status == "suppressed"
    assert pending.last_error == "Daily Slack failure-alert limit reached"


@pytest.mark.asyncio
@respx.mock
async def test_trash_talk_text_has_no_operational_alarm_prefix(session_factory):
    settings = Settings(_env_file=None, slack_webhook_url=WEBHOOK_URL)
    webhook_request = respx.post(WEBHOOK_URL).mock(return_value=Response(200, text="ok"))
    async with session_factory() as session, session.begin():
        delivery = await queue_trash_talk(
            session,
            settings,
            body="🤖 The scoreboard has completed its audit.",
            dedupe_key="trash-talk:week-1",
        )
        await deliver_trash_talk(delivery, settings)

    assert delivery.kind == "trash_talk"
    assert delivery.status == "delivered"
    assert json.loads(webhook_request.calls[0].request.content) == {
        "text": "🤖 The scoreboard has completed its audit."
    }


@pytest.mark.asyncio
@respx.mock
async def test_trash_talk_uses_dedicated_webhook_and_channel_label(session_factory):
    trash_webhook = "https://hooks.slack.com/services/T00000000/B00000001/trash-secret"
    settings = Settings(
        _env_file=None,
        slack_webhook_url=WEBHOOK_URL,
        slack_channel_label="#sleeper-ai-alerts",
        trash_talk_slack_webhook_url=trash_webhook,
        trash_talk_slack_channel_label="#fantasy-football-trash-talk",
    )
    operational_request = respx.post(WEBHOOK_URL).mock(return_value=Response(200, text="ok"))
    trash_request = respx.post(trash_webhook).mock(return_value=Response(200, text="ok"))
    async with session_factory() as session, session.begin():
        delivery = await queue_trash_talk(
            session,
            settings,
            body="🤖 Dedicated channel check.",
            dedupe_key="trash-talk:dedicated-channel",
        )
        await deliver_trash_talk(delivery, settings)

    assert delivery.recipient == "#fantasy-football-trash-talk"
    assert delivery.status == "delivered"
    assert trash_request.call_count == 1
    assert operational_request.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_trash_talk_uploads_private_image_with_narrow_bot_scope(
    session_factory, tmp_path
):
    image = tmp_path / "trash-talk" / "card.png"
    image.parent.mkdir()
    image.write_bytes(b"png-bytes")
    settings = Settings(
        _env_file=None,
        reports_dir=tmp_path,
        slack_bot_token="xoxb-test-token",
        slack_channel_id="C123456",
    )
    ticket = respx.post("https://slack.com/api/files.getUploadURLExternal").mock(
        return_value=Response(
            200,
            json={"ok": True, "upload_url": "https://uploads.slack.test/file", "file_id": "F1"},
        )
    )
    upload = respx.post("https://uploads.slack.test/file").mock(return_value=Response(200))
    complete = respx.post("https://slack.com/api/files.completeUploadExternal").mock(
        return_value=Response(200, json={"ok": True})
    )
    async with session_factory() as session, session.begin():
        delivery = await queue_trash_talk(
            session,
            settings,
            body="Robot scorecard",
            dedupe_key="trash-talk:image",
            image_path=str(image),
            alt_text="Accessible scorecard description",
        )
        await deliver_trash_talk(delivery, settings)

    assert ticket.called and upload.called and complete.called
    assert delivery.status == "delivered"


@pytest.mark.asyncio
@respx.mock
async def test_trash_talk_text_fallback_is_attempted_only_once(session_factory, tmp_path):
    image = tmp_path / "trash-talk" / "card.png"
    image.parent.mkdir()
    image.write_bytes(b"png-bytes")
    settings = Settings(
        _env_file=None,
        reports_dir=tmp_path,
        slack_webhook_url=WEBHOOK_URL,
        slack_bot_token="xoxb-test-token",
        slack_channel_id="C123456",
    )
    respx.post("https://slack.com/api/files.getUploadURLExternal").mock(
        return_value=Response(500)
    )
    fallback = respx.post(WEBHOOK_URL).mock(return_value=Response(500))
    async with session_factory() as session, session.begin():
        delivery = await queue_trash_talk(
            session,
            settings,
            body="Robot scorecard",
            dedupe_key="trash-talk:fallback-once",
            image_path=str(image),
        )
        with pytest.raises(HTTPStatusError):
            await deliver_trash_talk(delivery, settings)
        with pytest.raises(HTTPStatusError):
            await deliver_trash_talk(delivery, settings)

    assert fallback.call_count == 1
    assert delivery.payload["fallback_attempted"] is True


@pytest.mark.asyncio
async def test_operational_daily_limit_does_not_suppress_trash_talk(
    session_factory, monkeypatch
):
    settings = Settings(
        _env_file=None,
        slack_webhook_url=WEBHOOK_URL,
        slack_daily_alert_limit=1,
    )

    @asynccontextmanager
    async def test_session_scope():
        async with session_factory() as session, session.begin():
            yield session

    async def fake_deliver(delivery, _settings):
        delivery.status = "delivered"
        delivery.delivered_at = datetime.now(timezone.utc)

    monkeypatch.setattr("app.worker.get_settings", lambda: settings)
    monkeypatch.setattr("app.worker.session_scope", test_session_scope)
    monkeypatch.setattr("app.worker.deliver_trash_talk", fake_deliver)
    async with session_factory() as session, session.begin():
        session.add_all(
            [
                NotificationDelivery(
                    channel="slack",
                    kind="operational",
                    recipient="alerts",
                    subject="prior failure",
                    payload={"body": "prior"},
                    dedupe_key="prior-operational",
                    status="delivered",
                    delivered_at=datetime.now(timezone.utc),
                ),
                NotificationDelivery(
                    channel="slack",
                    kind="trash_talk",
                    recipient="alerts",
                    subject="robot",
                    payload={"body": "fun"},
                    dedupe_key="pending-trash-talk",
                    status="pending",
                ),
            ]
        )

    assert await _process_slack_alert() is True
    async with session_factory() as session:
        delivery = await session.scalar(
            select(NotificationDelivery).where(
                NotificationDelivery.dedupe_key == "pending-trash-talk"
            )
        )
    assert delivery.status == "delivered"
