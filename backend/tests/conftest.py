"""Keep offline tests independent of external tracing configuration."""

from collections.abc import Iterator

import pytest
from langsmith import tracing_context

@pytest.fixture(autouse=True)
def disable_external_tracing() -> Iterator[None]:
    """Offline tests must not inherit tracing credentials from the parent process."""
    with tracing_context(enabled=False):
        yield
