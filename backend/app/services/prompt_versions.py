from __future__ import annotations

import hashlib
from pathlib import Path


PROMPT_ROOT = Path(__file__).resolve().parents[1] / "prompts"


def prompt_version(name: str) -> str:
    return hashlib.sha256((PROMPT_ROOT / f"{name}.txt").read_bytes()).hexdigest()[:12]
