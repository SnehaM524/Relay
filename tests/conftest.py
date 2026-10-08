from pathlib import Path

import pytest

from autopilot.agent import Agent
from autopilot.settings import Settings

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def settings() -> Settings:
    return Settings(mock_llm=True, database_url=":memory:", config_dir=ROOT / "config", data_dir=ROOT / "data")


@pytest.fixture
def agent(settings) -> Agent:
    a = Agent(settings)
    a.load()
    return a
