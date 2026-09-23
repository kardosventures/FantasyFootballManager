from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, TimestampMixin

JsonType = JSON().with_variant(JSONB, "postgresql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def uuid_str() -> str:
    return str(uuid.uuid4())


class SourceObservation(Base):
    __tablename__ = "source_observations"
    __table_args__ = (
        UniqueConstraint("provider", "endpoint", "content_hash", name="uq_observation_payload"),
        Index("ix_source_observations_entity", "entity_type", "entity_id", "observed_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(512), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_payload: Mapped[dict[str, Any] | list[Any]] = mapped_column(JsonType, nullable=False)
    normalized_payload: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    freshness_status: Mapped[str] = mapped_column(String(32), default="fresh", nullable=False)
    accepted: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(Text)


class SourceHealth(Base, TimestampMixin):
    __tablename__ = "source_health"

    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_content_hash: Mapped[str | None] = mapped_column(String(64))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    rate_limited_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class SourceDatasetHealth(Base, TimestampMixin):
    """Health and freshness for one independently actionable source dataset."""

    __tablename__ = "source_dataset_health"
    __table_args__ = (
        UniqueConstraint("provider", "dataset", name="uq_source_dataset_health_identity"),
        Index("ix_source_dataset_health_status", "status", "last_success_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_content_hash: Mapped[str | None] = mapped_column(String(64))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    expected_records: Mapped[int | None] = mapped_column(Integer)
    observed_records: Mapped[int | None] = mapped_column(Integer)
    completeness: Mapped[float | None] = mapped_column(Float)
    last_error: Mapped[str | None] = mapped_column(Text)


class OperationalIncident(Base, TimestampMixin):
    __tablename__ = "operational_incidents"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_operational_incident_dedupe"),
        Index("ix_operational_incident_state", "status", "severity"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    summary: Mapped[str] = mapped_column(String(255), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    first_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    last_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DecisionManifest(Base):
    """Immutable binding between a manager result and every input/version it used."""

    __tablename__ = "decision_manifests"
    __table_args__ = (
        UniqueConstraint("decision_type", "state_hash", name="uq_decision_manifest_state"),
        Index("ix_decision_manifest_week", "league_id", "season", "week", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    decision_type: Mapped[str] = mapped_column(String(64), nullable=False)
    league_id: Mapped[str] = mapped_column(String(64), nullable=False)
    season: Mapped[str] = mapped_column(String(16), nullable=False)
    week: Mapped[int] = mapped_column(Integer, nullable=False)
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_hashes: Mapped[list[str]] = mapped_column(JsonType, default=list, nullable=False)
    source_gate: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    versions: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    result_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class PlayerWeekFeature(Base):
    __tablename__ = "player_week_features"
    __table_args__ = (
        UniqueConstraint(
            "player_id",
            "season",
            "week",
            "feature_version",
            "content_hash",
            name="uq_player_week_feature_snapshot",
        ),
        Index("ix_player_week_feature_lookup", "season", "week", "player_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    player_id: Mapped[str] = mapped_column(String(64), nullable=False)
    season: Mapped[int] = mapped_column(Integer, nullable=False)
    week: Mapped[int] = mapped_column(Integer, nullable=False)
    feature_version: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    features: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ProjectionSnapshot(Base):
    __tablename__ = "projection_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "player_id",
            "season",
            "week",
            "model_version",
            "input_hash",
            name="uq_projection_snapshot_input",
        ),
        Index("ix_projection_snapshot_lookup", "season", "week", "player_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    player_id: Mapped[str] = mapped_column(String(64), nullable=False)
    season: Mapped[int] = mapped_column(Integer, nullable=False)
    week: Mapped[int] = mapped_column(Integer, nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    projection: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class SimulationRun(Base):
    __tablename__ = "simulation_runs"
    __table_args__ = (
        UniqueConstraint("input_hash", "model_version", "seed", name="uq_simulation_run_input"),
        Index("ix_simulation_run_week", "league_id", "season", "week", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    league_id: Mapped[str] = mapped_column(String(64), nullable=False)
    season: Mapped[int] = mapped_column(Integer, nullable=False)
    week: Mapped[int] = mapped_column(Integer, nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    simulation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    results: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class LeagueSettingsSnapshot(Base):
    __tablename__ = "league_settings_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    league_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    content_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    normalized_payload: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    discrepancy_report: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)


class LeagueMember(Base):
    __tablename__ = "league_members"
    __table_args__ = (UniqueConstraint("league_id", "user_id", name="uq_league_member"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    league_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    roster_id: Mapped[int | None] = mapped_column(Integer)
    display_name: Mapped[str | None] = mapped_column(String(200))
    team_name: Mapped[str | None] = mapped_column(String(200))
    is_commissioner: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)


class LeagueRosterSnapshot(Base):
    __tablename__ = "league_roster_snapshots"
    __table_args__ = (
        UniqueConstraint("league_id", "roster_id", "content_hash", name="uq_roster_snapshot"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    league_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    roster_id: Mapped[int] = mapped_column(Integer, nullable=False)
    owner_id: Mapped[str | None] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    players: Mapped[list[str]] = mapped_column(JsonType, default=list, nullable=False)
    starters: Mapped[list[str]] = mapped_column(JsonType, default=list, nullable=False)
    reserve: Mapped[list[str]] = mapped_column(JsonType, default=list, nullable=False)
    settings: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)


class Draft(Base, TimestampMixin):
    __tablename__ = "drafts"

    draft_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    league_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    draft_type: Mapped[str] = mapped_column(String(32), nullable=False)
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rounds: Mapped[int | None] = mapped_column(Integer)
    pick_timer: Mapped[int | None] = mapped_column(Integer)
    draft_order: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)


class DraftPick(Base):
    __tablename__ = "draft_picks"
    __table_args__ = (UniqueConstraint("draft_id", "pick_no", name="uq_draft_pick"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    draft_id: Mapped[str] = mapped_column(ForeignKey("drafts.draft_id"), index=True, nullable=False)
    pick_no: Mapped[int] = mapped_column(Integer, nullable=False)
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    roster_id: Mapped[int | None] = mapped_column(Integer)
    player_id: Mapped[str] = mapped_column(String(64), nullable=False)
    picked_by: Mapped[str | None] = mapped_column(String(64))
    picked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)


class DomainEvent(Base):
    __tablename__ = "domain_events"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_domain_event_dedupe"),
        Index("ix_domain_event_lease", "status", "not_before", "priority"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    event_type: Mapped[str] = mapped_column(String(96), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(36))
    causation_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    not_before: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScheduledJob(Base, TimestampMixin):
    __tablename__ = "scheduled_jobs"
    __table_args__ = (UniqueConstraint("dedupe_key", name="uq_scheduled_job_dedupe"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    job_type: Mapped[str] = mapped_column(String(96), nullable=False)
    profile: Mapped[str] = mapped_column(String(48), nullable=False)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="scheduled", nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)


class ActionItem(Base, TimestampMixin):
    __tablename__ = "action_items"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_action_item_dedupe"),
        Index("ix_action_due", "status", "priority", "recommended_complete_by"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="proposed", nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    exact_action: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    primary_reason: Mapped[str] = mapped_column(Text, nullable=False)
    supporting_evidence: Mapped[list[dict[str, Any]]] = mapped_column(
        JsonType, default=list, nullable=False
    )
    source_urls: Mapped[list[str]] = mapped_column(JsonType, default=list, nullable=False)
    source_freshness: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    drop_protection_tier: Mapped[str | None] = mapped_column(String(32))
    approval_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    recommended_complete_by: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    escalation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hard_lock_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verification_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_policy_version: Mapped[str] = mapped_column(String(32), default="v1", nullable=False)
    evidence_hashes: Mapped[list[str]] = mapped_column(JsonType, default=list, nullable=False)
    expected_state_hash: Mapped[str | None] = mapped_column(String(64))
    verification_plan: Mapped[dict[str, Any]] = mapped_column(
        JsonType, default=dict, nullable=False
    )
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approval_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_commands: Mapped[list[ExecutionCommand]] = relationship(back_populates="action")


class ActionAudit(Base):
    __tablename__ = "action_audit"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    action_id: Mapped[str] = mapped_column(
        ForeignKey("action_items.id"), index=True, nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)


class ExecutionCommand(Base):
    __tablename__ = "execution_commands"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_execution_command_idempotency"),
        Index("ix_execution_command_lease", "status", "not_before", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    action_id: Mapped[str] = mapped_column(ForeignKey("action_items.id"), nullable=False)
    action: Mapped[ActionItem] = relationship(back_populates="execution_commands")
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    league_id: Mapped[str] = mapped_column(String(64), nullable=False)
    roster_id: Mapped[int] = mapped_column(Integer, nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    expected_state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_hashes: Mapped[list[str]] = mapped_column(JsonType, default=list, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    not_before: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    verification_plan: Mapped[dict[str, Any]] = mapped_column(
        JsonType, default=dict, nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), default="ready", nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ManualConfirmation(Base):
    __tablename__ = "manual_confirmations"
    __table_args__ = (UniqueConstraint("nonce_hash", name="uq_confirmation_nonce"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    action_id: Mapped[str] = mapped_column(
        ForeignKey("action_items.id"), index=True, nullable=False
    )
    nonce_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"
    __table_args__ = (UniqueConstraint("dedupe_key", name="uq_notification_dedupe"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    action_id: Mapped[str | None] = mapped_column(ForeignKey("action_items.id"), index=True)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), default="operational", nullable=False)
    recipient: Mapped[str] = mapped_column(String(320), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class TrashTalkControl(Base):
    __tablename__ = "trash_talk_controls"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default="global")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    paused_reason: Mapped[str | None] = mapped_column(Text)
    opted_out_roster_ids: Mapped[list[int]] = mapped_column(JsonType, default=list, nullable=False)
    runtime_state: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class TrashTalkPost(Base):
    __tablename__ = "trash_talk_posts"
    __table_args__ = (
        UniqueConstraint("trigger_key", name="uq_trash_talk_trigger_key"),
        Index("ix_trash_talk_week_status", "season", "week", "status"),
        Index("ix_trash_talk_target", "target_roster_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    season: Mapped[str] = mapped_column(String(16), nullable=False)
    week: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger_key: Mapped[str] = mapped_column(String(255), nullable=False)
    target_roster_id: Mapped[int | None] = mapped_column(Integer)
    target_label: Mapped[str | None] = mapped_column(String(128))
    evidence: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    format: Mapped[str] = mapped_column(String(32), default="text", nullable=False)
    image_path: Mapped[str | None] = mapped_column(Text)
    alt_text: Mapped[str | None] = mapped_column(Text)
    safety: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    quality_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="candidate", nullable=False)
    suppression_reason: Mapped[str | None] = mapped_column(Text)
    notification_delivery_id: Mapped[str | None] = mapped_column(
        ForeignKey("notification_deliveries.id"), index=True
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ShimHeartbeat(Base):
    __tablename__ = "shim_heartbeats"

    agent_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    app_version: Mapped[str | None] = mapped_column(String(32))
    app_running: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    session_available: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(32), default="dry_run", nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(JsonType, default=list, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
