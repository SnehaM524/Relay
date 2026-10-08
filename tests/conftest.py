from __future__ import annotations

from pathlib import Path

import pytest

from relay.pipeline import Pipeline
from relay.settings import Settings

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def settings() -> Settings:
    return Settings(mock_integrations=True, database_url=":memory:", config_dir=ROOT / "config",
                    sla_tier_a_minutes=5, sla_tier_b_minutes=15, sla_tier_c_minutes=60)


@pytest.fixture
def pipeline(settings) -> Pipeline:
    return Pipeline.build(settings)
