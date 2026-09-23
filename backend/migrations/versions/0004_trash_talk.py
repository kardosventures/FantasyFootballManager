"""Add autonomous, separately rate-limited fantasy trash talk.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-20
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from app import models  # noqa: F401
from app.database import Base

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    notification_columns = {
        column["name"] for column in inspector.get_columns("notification_deliveries")
    }
    if "kind" not in notification_columns:
        op.add_column(
            "notification_deliveries",
            sa.Column("kind", sa.String(length=32), nullable=False, server_default="operational"),
        )
    tables = set(inspector.get_table_names())
    if "trash_talk_controls" not in tables:
        Base.metadata.tables["trash_talk_controls"].create(bind=bind)
    if "trash_talk_posts" not in tables:
        Base.metadata.tables["trash_talk_posts"].create(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "trash_talk_posts" in tables:
        Base.metadata.tables["trash_talk_posts"].drop(bind=bind)
    if "trash_talk_controls" in tables:
        Base.metadata.tables["trash_talk_controls"].drop(bind=bind)
    notification_columns = {
        column["name"] for column in inspect(bind).get_columns("notification_deliveries")
    }
    if "kind" in notification_columns:
        op.drop_column("notification_deliveries", "kind")
