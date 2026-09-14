"""Keep app import-time configuration independent of local model credentials."""

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
os.environ["TEST_AGENT_MODEL_CONFIG_PATH"] = str(PROJECT_ROOT / "models.example.toml")
os.environ["TEST_AGENT_PLANNING_MODEL_ID"] = "default"
os.environ["TEST_AGENT_EXECUTION_MODEL_ID"] = "default"
