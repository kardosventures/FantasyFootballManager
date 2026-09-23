from __future__ import annotations

import hashlib
import hmac
import secrets
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import ActionItem, ExecutionCommand, ManualConfirmation
from app.policy import (
    POLICY_VERSION,
    DropEvidence,
    approval_binding,
    evaluate_drop,
    validate_action,
)
from app.repositories import transition_action
from app.utils import content_hash


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    message: str
    evidence: dict[str, Any]


class ExecutionAdapter(ABC):
    @abstractmethod
    async def submit(self, command: ExecutionCommand) -> ExecutionResult: ...


class ManualExecutionAdapter(ExecutionAdapter):
    async def submit(self, command: ExecutionCommand) -> ExecutionResult:
        return ExecutionResult("blocked", "Manual execution mode; no write attempted", {})


class DisabledSleeperWriteAdapter(ExecutionAdapter):
    async def submit(self, command: ExecutionCommand) -> ExecutionResult:
        return ExecutionResult("blocked", "Sleeper writes are disabled", {})


class BrowserSleeperAdapter(ExecutionAdapter):
    async def submit(self, command: ExecutionCommand) -> ExecutionResult:
        return ExecutionResult(
            "ready", "Queued for the authenticated Playwright browser agent", {"id": command.id}
        )


class FakeSleeperExecutionAdapter(ExecutionAdapter):
    async def submit(self, command: ExecutionCommand) -> ExecutionResult:
        return ExecutionResult(
            "verified",
            "Fake driver accepted and verified the command",
            {"fake": True, "idempotency_key": command.idempotency_key},
        )


def adapter_for_mode(mode: str) -> ExecutionAdapter:
    return {
        "disabled": DisabledSleeperWriteAdapter(),
        "manual": ManualExecutionAdapter(),
        "dry_run": BrowserSleeperAdapter(),
        "browser": BrowserSleeperAdapter(),
        # Kept temporarily so existing queued configuration fails over safely
        # while installations migrate from the Swift desktop shim.
        "desktop": BrowserSleeperAdapter(),
        "fake": FakeSleeperExecutionAdapter(),
    }[mode]


EXECUTION_TRANSITIONS = {
    "ready": {"leased"},
    "leased": {"preflight", "blocked", "failed"},
    "preflight": {"executing", "verified", "blocked", "failed"},
    "executing": {"verifying", "unverified", "failed", "blocked"},
    "verifying": {"verified", "unverified", "failed", "blocked"},
    "verified": set(),
    # An uncertain browser write may be reconciled later by a stronger verifier.
    "unverified": {"verified", "failed"},
    "failed": set(),
    "blocked": set(),
}


def validate_execution_transition(current: str, target: str) -> None:
    if target not in EXECUTION_TRANSITIONS.get(current, set()):
        raise ValueError(f"Invalid execution transition: {current} -> {target}")


async def propose_execution(
    session: AsyncSession,
    *,
    action_type: str,
    league_id: str,
    roster_id: int,
    parameters: dict[str, Any],
    expected_state_hash: str,
    evidence_hashes: list[str],
    reason: str,
    confidence: float,
    approval_required: bool,
    not_before: datetime | None = None,
    expires_at: datetime,
    verification_plan: dict[str, Any],
    dedupe_key: str,
    settings: Settings | None = None,
) -> tuple[ActionItem, ExecutionCommand | None]:
    validate_action(action_type, parameters)
    eligible_at = not_before or datetime.now(timezone.utc)
    if eligible_at >= expires_at:
        raise ValueError("Execution command must become eligible before it expires")
    existing = await session.scalar(select(ActionItem).where(ActionItem.dedupe_key == dedupe_key))
    if existing:
        command = await session.scalar(
            select(ExecutionCommand).where(ExecutionCommand.action_id == existing.id).limit(1)
        )
        return existing, command
    drop_tier: str | None = None
    if parameters.get("drop_player_id"):
        raw = parameters.get("drop_evidence") or {}
        drop = evaluate_drop(
            DropEvidence(
                player_id=str(parameters["drop_player_id"]),
                positions=tuple(raw.get("positions") or ()),
                ros_ranks=raw.get("ros_ranks") or {},
                ranking_fresh=bool(raw.get("ranking_fresh", False)),
                ranking_disputed=bool(raw.get("ranking_disputed", False)),
                manually_locked=bool(raw.get("manually_locked", False)),
                protection_tier=raw.get("protection_tier") or "REVIEW",
                recently_acquired=bool(raw.get("recently_acquired", False)),
            )
        )
        approval_required = approval_required or drop.approval_required
        drop_tier = drop.protection_tier
    target_status = "approval_required" if approval_required else "ready"
    action = ActionItem(
        action_type=action_type,
        title=action_type.replace("_", " ").title(),
        status="proposed",
        priority=0 if action_type == "DRAFT_PLAYER" else 1,
        exact_action=parameters,
        primary_reason=reason,
        confidence=confidence,
        drop_protection_tier=drop_tier,
        approval_required=approval_required,
        decision_policy_version=POLICY_VERSION,
        evidence_hashes=evidence_hashes,
        expected_state_hash=expected_state_hash,
        verification_plan=verification_plan,
        dedupe_key=dedupe_key,
        approval_expires_at=expires_at if approval_required else None,
    )
    session.add(action)
    await session.flush()
    await transition_action(
        session,
        action,
        target_status,
        actor="decision-engine",
        details={"policy_version": POLICY_VERSION},
    )
    if approval_required:
        if settings:
            await create_confirmation(session, action, settings)
        return action, None
    command = ExecutionCommand(
        action_id=action.id,
        action_type=action_type,
        league_id=league_id,
        roster_id=roster_id,
        parameters=parameters,
        expected_state_hash=expected_state_hash,
        decision_policy_version=POLICY_VERSION,
        evidence_hashes=evidence_hashes,
        idempotency_key=content_hash({"action": action.id, "state": expected_state_hash}),
        not_before=eligible_at,
        expires_at=expires_at,
        verification_plan=verification_plan,
        status="ready",
    )
    session.add(command)
    return action, command


def _sign(secret: str, value: str) -> str:
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()


async def create_confirmation(
    session: AsyncSession, action: ActionItem, settings: Settings, *, ttl_minutes: int = 30
) -> str:
    nonce = secrets.token_urlsafe(32)
    binding = approval_binding(
        action_type=action.action_type,
        parameters=action.exact_action,
        evidence_hashes=action.evidence_hashes,
        policy_version=action.decision_policy_version,
    )
    nonce_hash = _sign(settings.app_secret_key, nonce)
    session.add(
        ManualConfirmation(
            action_id=action.id,
            nonce_hash=nonce_hash,
            binding_hash=content_hash(binding),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes),
        )
    )
    return nonce


async def confirm_action(
    session: AsyncSession, action: ActionItem, nonce: str, settings: Settings
) -> ExecutionCommand:
    now = datetime.now(timezone.utc)
    confirmation = await session.scalar(
        select(ManualConfirmation).where(
            ManualConfirmation.action_id == action.id,
            ManualConfirmation.nonce_hash == _sign(settings.app_secret_key, nonce),
        )
    )
    if not confirmation or confirmation.confirmed_at or confirmation.invalidated_at:
        raise ValueError("Approval link is invalid or already used")
    if confirmation.expires_at <= now or (
        action.approval_expires_at and action.approval_expires_at <= now
    ):
        raise ValueError("Approval link has expired")
    current = approval_binding(
        action_type=action.action_type,
        parameters=action.exact_action,
        evidence_hashes=action.evidence_hashes,
        policy_version=action.decision_policy_version,
    )
    if confirmation.binding_hash != content_hash(current):
        confirmation.invalidated_at = now
        raise ValueError("Material evidence changed; a new approval is required")
    confirmation.confirmed_at = now
    action.approved_at = now
    await transition_action(
        session, action, "ready", actor="owner", details={"confirmation": "single-use"}
    )
    command = ExecutionCommand(
        action_id=action.id,
        action_type=action.action_type,
        league_id=settings.sleeper_league_id,
        roster_id=settings.sleeper_roster_id,
        parameters=action.exact_action,
        expected_state_hash=action.expected_state_hash or content_hash(current),
        decision_policy_version=action.decision_policy_version,
        evidence_hashes=action.evidence_hashes,
        idempotency_key=content_hash({"action_id": action.id, "binding": current}),
        expires_at=action.approval_expires_at or (now + timedelta(minutes=10)),
        verification_plan=action.verification_plan
        or {"source": "Sleeper public API", "action_type": action.action_type},
        status="ready",
    )
    session.add(command)
    return command


def command_contract(command: ExecutionCommand) -> dict[str, Any]:
    return {
        "id": command.id,
        "action_type": command.action_type,
        "league_id": command.league_id,
        "roster_id": command.roster_id,
        "parameters": command.parameters,
        "expected_state_hash": command.expected_state_hash,
        "decision_policy_version": command.decision_policy_version,
        "evidence_hashes": command.evidence_hashes,
        "idempotency_key": command.idempotency_key,
        "not_before": command.not_before.isoformat(),
        "expires_at": command.expires_at.isoformat(),
        "verification_plan": command.verification_plan,
    }
