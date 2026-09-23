"""Widen intelligence component version identifiers.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-09
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "player_week_features",
        "feature_version",
        existing_type=sa.String(length=32),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "projection_snapshots",
        "model_version",
        existing_type=sa.String(length=32),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "simulation_runs",
        "model_version",
        existing_type=sa.String(length=32),
        type_=sa.String(length=64),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "simulation_runs",
        "model_version",
        existing_type=sa.String(length=64),
        type_=sa.String(length=32),
        existing_nullable=False,
    )
    op.alter_column(
        "projection_snapshots",
        "model_version",
        existing_type=sa.String(length=64),
        type_=sa.String(length=32),
        existing_nullable=False,
    )
    op.alter_column(
        "player_week_features",
        "feature_version",
        existing_type=sa.String(length=64),
        type_=sa.String(length=32),
        existing_nullable=False,
    )
