#!/usr/bin/env python3
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings
from app.models import NotificationDelivery
from app.notifications import deliver_slack_alert


async def send_test(settings: Settings) -> None:
    if not settings.slack_notifications_configured:
        raise SystemExit("Slack webhook is not configured")
    delivery = NotificationDelivery(
        channel="slack",
        recipient=settings.slack_channel_label,
        subject="SLEEPER AI AGENT: NOTIFICATION TEST",
        payload={
            "body": (
                "This confirms that failure notifications can reach this channel through "
                "a one-way, post-only Slack webhook."
            ),
            "screenshot_count": 0,
        },
        dedupe_key=f"manual-slack-test:{datetime.now(timezone.utc).isoformat()}",
        status="pending",
    )
    await deliver_slack_alert(delivery, settings)
    if delivery.status != "delivered":
        raise SystemExit(delivery.last_error or "Slack notification test failed")
    print(f"Notification test delivered to {delivery.recipient}")


def main() -> None:
    project = Path(__file__).resolve().parents[1]
    asyncio.run(send_test(Settings(_env_file=project / ".env")))


if __name__ == "__main__":
    main()
