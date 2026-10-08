"""Environment-driven settings. Every integration can run in `mock` mode so the
engine is fully testable without credentials."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RELAY_", env_file=".env", extra="ignore")

    # Global
    env: str = "dev"
    mock_integrations: bool = Field(default=True, description="Use in-memory fakes for all vendors")
    config_dir: Path = Path("config")
    database_url: str = "sqlite:///relay.db"
    log_level: str = "INFO"

    # HubSpot
    hubspot_access_token: str | None = None
    hubspot_webhook_secret: str | None = None  # client secret used for v3 signature

    # Clay
    clay_api_key: str | None = None
    clay_webhook_url: str | None = None  # Clay table webhook (push model)
    clay_timeout_s: float = 8.0

    # Harmonic
    harmonic_api_key: str | None = None
    harmonic_base_url: str = "https://api.harmonic.ai"
    harmonic_timeout_s: float = 8.0

    # Salesforce
    sf_instance_url: str | None = None
    sf_client_id: str | None = None
    sf_client_secret: str | None = None
    sf_username: str | None = None
    sf_password: str | None = None
    sf_security_token: str | None = None
    sf_api_version: str = "v60.0"

    # Slack
    slack_bot_token: str | None = None
    slack_signing_secret: str | None = None
    slack_handoff_channel: str = "#sdr-handoffs"
    slack_escalation_channel: str = "#sdr-escalations"

    # SLA (minutes), per tier
    sla_tier_a_minutes: int = 5
    sla_tier_b_minutes: int = 15
    sla_tier_c_minutes: int = 60
    sla_check_interval_s: int = 30
    sla_reassign_after_breaches: int = 1

    @property
    def sla_minutes(self) -> dict[str, int]:
        return {"A": self.sla_tier_a_minutes, "B": self.sla_tier_b_minutes, "C": self.sla_tier_c_minutes}


settings = Settings()
