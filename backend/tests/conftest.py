"""Keep app import-time configuration independent of local model credentials."""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from langsmith import tracing_context

PROJECT_ROOT = Path(__file__).resolve().parents[2]
os.environ["TEST_AGENT_MODEL_CONFIG_PATH"] = str(PROJECT_ROOT / "models.example.toml")
os.environ["TEST_AGENT_PLANNING_MODEL_ID"] = "default"
os.environ["TEST_AGENT_EXECUTION_MODEL_ID"] = "default"


@pytest.fixture(autouse=True)
def disable_external_tracing() -> Iterator[None]:
    """Offline tests must not inherit tracing credentials from a local .env."""
    with tracing_context(enabled=False):
        yield
