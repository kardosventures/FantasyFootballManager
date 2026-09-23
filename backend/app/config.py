from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    app_env: Literal["development", "test", "production"] = "development"
    app_timezone: str = "America/Denver"
    database_url: str = "postgresql+asyncpg://fantasy:fantasy@db:5432/fantasy"
    sync_database_url: str = "postgresql+psycopg://fantasy:fantasy@db:5432/fantasy"
    sleeper_base_url: str = "https://api.sleeper.app/v1"
    sleeper_league_id: str = "1395499060898586624"
    sleeper_owner_user_id: str = "1398337982238343168"
    sleeper_roster_id: int = 8
    sleeper_draft_id: str | None = None
    sleeper_league_web_url: str = "https://sleeper.com/leagues/1395499060898586624"
    draft_poll_seconds: float = Field(default=2.0, ge=1.0, le=15.0)
    draft_static_refresh_minutes: int = Field(default=720, ge=15, le=1440)
    draft_auto_pick_enabled: bool = False
    draft_auto_submit_seconds_remaining: int = Field(default=15, ge=5, le=45)
    draft_mock_auto_submit_delay_seconds: int = Field(default=0, ge=0, le=300)
    draft_min_confidence: float = Field(default=0.65, ge=0.0, le=1.0)
    draft_expert_enabled: bool = False
    draft_expert_provider: Literal["codex_cli", "openai_api"] = "codex_cli"
    draft_expert_model: str = "gpt-5.6-terra"
    draft_expert_reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    draft_expert_timeout_seconds: float = Field(default=85.0, ge=10.0, le=90.0)
    draft_expert_emergency_fallback_enabled: bool = True
    draft_expert_candidate_pool_size: int = Field(default=72, ge=24, le=120)
    draft_expert_web_search_enabled: bool = True
    openai_api_key: SecretStr | None = None
    fantasypros_api_key: SecretStr | None = None
    fantasypros_base_url: str = "https://api.fantasypros.com/public/v2/json"
    in_season_sync_enabled: bool = True
    nflverse_sync_hours: int = Field(default=4, ge=1, le=24)
    nflverse_history_start_season: int = Field(default=2021, ge=1999, le=2100)
    sleeper_historical_league_ids: str = ""
    fantasypros_sync_minutes: int = Field(default=180, ge=60, le=360)
    fantasypros_daily_request_limit: int = Field(default=50, ge=10, le=10_000)
    fantasypros_request_reserve: int = Field(default=10, ge=1, le=1_000)
    fantasypros_requests_per_sync: int = Field(default=4, ge=1, le=10)
    fantasypros_emergency_refresh_cooldown_minutes: int = Field(default=15, ge=5, le=60)
    in_season_expert_enabled: bool = False
    in_season_expert_minutes: int = Field(default=60, ge=15, le=360)
    in_season_expert_timeout_seconds: float = Field(default=300.0, ge=30.0, le=600.0)
    in_season_lineup_actions_enabled: bool = False
    in_season_lineup_min_confidence: float = Field(default=0.75, ge=0.0, le=1.0)
    in_season_lineup_submit_minutes_before_kickoff: int = Field(default=45, ge=10, le=180)
    in_season_lineup_expire_minutes_before_kickoff: int = Field(default=5, ge=2, le=30)
    in_season_acquisition_actions_enabled: bool = False
    in_season_transaction_trust_then_verify: bool = False
    in_season_sequential_free_agent_actions_enabled: bool = False
    in_season_multi_claim_actions_enabled: bool = False
    in_season_pending_waiver_actions_enabled: bool = False
    in_season_ir_actions_enabled: bool = False
    in_season_trade_actions_enabled: bool = False
    in_season_trade_min_confidence: float = Field(default=0.92, ge=0.8, le=1.0)
    in_season_trade_max_proposals_per_week: int = Field(default=1, ge=0, le=3)
    in_season_trade_cooldown_days: int = Field(default=7, ge=1, le=30)
    in_season_trade_expiration_hours: int = Field(default=24, ge=1, le=72)
    in_season_acquisition_min_confidence: float = Field(default=0.80, ge=0.0, le=1.0)
    in_season_acquisition_expire_minutes_before_kickoff: int = Field(default=10, ge=5, le=60)
    in_season_waiver_expire_minutes_before_processing: int = Field(default=5, ge=2, le=30)
    waiver_semantics_confirmed: bool = False
    waiver_mode: Literal["rolling_priority"] = "rolling_priority"
    waiver_process_weekday: int = Field(default=2, ge=0, le=6)
    waiver_process_hour: int = Field(default=1, ge=0, le=23)
    waiver_drop_clear_days: int = Field(default=2, ge=0, le=7)
    codex_decisions_dir: Path = Path("/app/codex-decisions")
    codex_watchdog_path: Path = Path("/app/host-runtime/codex-watchdog.json")
    reports_dir: Path = Path(__file__).resolve().parents[2] / "reports"
    app_secret_key: str = "development-only-change-me"
    shim_shared_secret: str = "development-shim-secret"
    execution_mode: Literal["disabled", "manual", "dry_run", "fake", "browser", "desktop"] = (
        "dry_run"
    )
    dashboard_public_url: str = "http://fantasy-manager.local:3000"
    dashboard_bind_address: str = "127.0.0.1"
    slack_webhook_url: SecretStr | None = None
    slack_channel_label: str = "Sleeper AI alerts"
    slack_daily_alert_limit: int = Field(default=20, ge=1, le=100)
    trash_talk_slack_webhook_url: SecretStr | None = None
    trash_talk_slack_channel_label: str | None = None
    slack_bot_token: SecretStr | None = None
    slack_channel_id: str | None = None
    trash_talk_enabled: bool = False
    trash_talk_auto_post: bool = True
    trash_talk_weekly_target: int = Field(default=6, ge=1, le=10)
    trash_talk_weekly_limit: int = Field(default=10, ge=1, le=10)
    trash_talk_daily_limit: int = Field(default=4, ge=1, le=10)
    trash_talk_cooldown_minutes: int = Field(default=90, ge=15, le=360)
    trash_talk_model_timeout_seconds: float = Field(default=60.0, ge=10.0, le=90.0)
    nws_base_url: str = "https://api.weather.gov"
    nws_user_agent: str = "JimAiFantasy/1.0"
    codex_explanations_enabled: bool = False
    codex_bin: str = "/usr/local/bin/codex"
    min_free_disk_gb: int = Field(default=40, ge=10)
    fantasy_agent_id: str = "playwright-agent-primary"
    browser_live_actions: str = ""
    sleeper_ui_contract_version: str = "sleeper-web-2026-09-09"
    source_gating_v2_enabled: bool = True
    sleeper_state_max_age_minutes: int = Field(default=20, ge=2, le=120)
    browser_observation_max_age_seconds: int = Field(default=180, ge=30, le=900)
    nflverse_context_max_age_hours: int = Field(default=8, ge=1, le=48)
    usage_features_enabled: bool = False
    projection_ensemble_enabled: bool = False
    championship_simulation_enabled: bool = False
    championship_simulations: int = Field(default=10_000, ge=200, le=100_000)
    championship_simulation_seed: int = 20260909
    in_season_plan_v2_enabled: bool = False
    in_season_plan_v2_execution_enabled: bool = False
    host_health_path: Path = Path("/app/host-runtime/host-health.json")
    backup_health_dir: Path = Path("/app/backups/daily")

    @field_validator("sleeper_base_url")
    @classmethod
    def validate_sleeper_base(cls, value: str) -> str:
        if value.rstrip("/") != "https://api.sleeper.app/v1":
            raise ValueError("Sleeper base URL must be the documented public v1 endpoint")
        return value.rstrip("/")

    @field_validator("fantasypros_base_url")
    @classmethod
    def validate_fantasypros_base(cls, value: str) -> str:
        if value.rstrip("/") != "https://api.fantasypros.com/public/v2/json":
            raise ValueError("FantasyPros base URL must be the documented public v2 endpoint")
        return value.rstrip("/")

    @field_validator("nws_base_url")
    @classmethod
    def validate_nws_base(cls, value: str) -> str:
        if value.rstrip("/") != "https://api.weather.gov":
            raise ValueError("NWS base URL must be the official api.weather.gov endpoint")
        return value.rstrip("/")

    @field_validator("slack_webhook_url", "trash_talk_slack_webhook_url")
    @classmethod
    def validate_slack_webhook(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None or not value.get_secret_value():
            return value
        parsed = urlsplit(value.get_secret_value())
        path_parts = [part for part in parsed.path.split("/") if part]
        if (
            parsed.scheme != "https"
            or parsed.hostname != "hooks.slack.com"
            or len(path_parts) != 4
            or path_parts[0] != "services"
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Slack webhook must be an official hooks.slack.com/services URL")
        return value

    @field_validator("slack_bot_token")
    @classmethod
    def validate_slack_bot_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and value.get_secret_value() and not value.get_secret_value().startswith(
            "xoxb-"
        ):
            raise ValueError("Slack image uploads require a bot token beginning with xoxb-")
        return value

    @field_validator("slack_channel_id")
    @classmethod
    def validate_slack_channel_id(cls, value: str | None) -> str | None:
        if value and not value.startswith("C"):
            raise ValueError("Slack channel ID must begin with C")
        return value

    @model_validator(mode="after")
    def validate_trash_talk_limits(self) -> Settings:
        if self.trash_talk_weekly_target > self.trash_talk_weekly_limit:
            raise ValueError("Trash-talk weekly target cannot exceed its hard limit")
        return self

    @property
    def slack_notifications_configured(self) -> bool:
        return bool(self.slack_webhook_url and self.slack_webhook_url.get_secret_value())

    @property
    def trash_talk_webhook(self) -> SecretStr | None:
        return self.trash_talk_slack_webhook_url or self.slack_webhook_url

    @property
    def trash_talk_notifications_configured(self) -> bool:
        webhook = self.trash_talk_webhook
        return bool(webhook and webhook.get_secret_value())

    @property
    def trash_talk_channel_label(self) -> str:
        return self.trash_talk_slack_channel_label or self.slack_channel_label

    @property
    def slack_image_upload_configured(self) -> bool:
        return bool(
            self.slack_bot_token
            and self.slack_bot_token.get_secret_value()
            and self.slack_channel_id
        )

    @property
    def live_browser_actions(self) -> set[str]:
        return {item.strip() for item in self.browser_live_actions.split(",") if item.strip()}

    @property
    def historical_sleeper_league_ids(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                item.strip()
                for item in self.sleeper_historical_league_ids.split(",")
                if item.strip() and item.strip() != self.sleeper_league_id
            )
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
