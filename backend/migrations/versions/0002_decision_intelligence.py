"""Dataset health, operational incidents, and decision manifests.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-09
"""

from alembic import op
from sqlalchemy import inspect

from app.models import (
    DecisionManifest,
    OperationalIncident,
    PlayerWeekFeature,
    ProjectionSnapshot,
    SimulationRun,
    SourceDatasetHealth,
)

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    for table in (
        SourceDatasetHealth.__table__,
        OperationalIncident.__table__,
        DecisionManifest.__table__,
        PlayerWeekFeature.__table__,
        ProjectionSnapshot.__table__,
        SimulationRun.__table__,
    ):
        if table.name not in tables:
            table.create(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    for table in (
        DecisionManifest.__table__,
        OperationalIncident.__table__,
        SourceDatasetHealth.__table__,
        SimulationRun.__table__,
        ProjectionSnapshot.__table__,
        PlayerWeekFeature.__table__,
    ):
        if table.name in tables:
            table.drop(bind=bind)
