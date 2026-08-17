import os

import pytest


@pytest.fixture
def pg_dsn() -> str:
    """Skips rather than errors when there is no database, so
    `uv run pytest -m postgres` without one reports a readable reason instead
    of a connection traceback."""
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        pytest.skip("DATABASE_URL is not set; start a pgvector container to run these")
    return dsn
