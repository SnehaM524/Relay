from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AUTOPILOT_", env_file=".env", extra="ignore")

    # Claude. If no key, the agent runs in mock mode with the heuristic playbook.
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    model: str = "claude-sonnet-4-5"
    max_tokens: int = 4000
    mock_llm: bool = False  # force mock even if a key is present

    # Where things live
    config_dir: Path = Path("config")
    data_dir: Path = Path("data")
    output_dir: Path = Path("out")
    database_url: str = "sqlite:///autopilot.db"

    # Analytics sources (optional; local JSON/CSV works without any)
    mixpanel_project_id: str | None = None
    mixpanel_service_account: str | None = None
    mixpanel_service_secret: str | None = None
    posthog_api_key: str | None = None
    posthog_project_id: str | None = None
    posthog_host: str = "https://us.posthog.com"

    # Feature flag provider for generated config: launchdarkly | statsig | growthbook | posthog
    flag_provider: str = "launchdarkly"

    # Experiment policy
    alpha: float = 0.05
    power_target: float = 0.8
    min_days: int = 7
    max_days: int = 28
    max_concurrent: int = 3
    min_detectable_effect: float = 0.05  # relative
    # Users gained by removing friction are lower-intent than the baseline cohort and
    # convert worse downstream. 0.7 = marginal users convert at 70% of baseline downstream rates.
    marginal_decay: float = 0.7

    @property
    def llm_enabled(self) -> bool:
        return bool(self.anthropic_api_key) and not self.mock_llm
