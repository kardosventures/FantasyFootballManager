from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class HeartbeatIn(BaseModel):
    agent_id: str = Field(min_length=1, max_length=128)
    app_version: str | None = None
    app_running: bool = False
    session_available: bool = False
    execution_mode: Literal["disabled", "manual", "dry_run", "fake", "browser", "desktop"] = (
        "dry_run"
    )
    capabilities: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class CommandResultIn(BaseModel):
    agent_id: str
    status: Literal[
        "preflight", "executing", "verifying", "verified", "unverified", "failed", "blocked"
    ]
    message: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class WritePreflightIn(BaseModel):
    agent_id: str = Field(min_length=1, max_length=128)
    ui_observation: dict[str, Any] = Field(default_factory=dict)


class ConfirmationIn(BaseModel):
    nonce: str = Field(min_length=20)


class TrashTalkControlIn(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class TrashTalkFeedbackIn(BaseModel):
    post_id: str = Field(min_length=36, max_length=36)
    rating: Literal["not_funny", "crossed_line"]


class TrashTalkOptOutIn(BaseModel):
    roster_id: int = Field(ge=1)
    opted_out: bool = True
