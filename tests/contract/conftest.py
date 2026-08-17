"""Fixtures for the contract suite.

TICKET-1 built the registry to hold classes, and the tests called `builder()`
or `builder(CORPUS)`. That only works for a store with no dependencies and no
lifecycle, and it seeded through a constructor — a shortcut that skipped the
write path entirely. A real store needs a connection, setup and teardown, so
the seam is rebuilt here around fixtures.

Seeding now goes through each profile's own write methods, which means every
contract run exercises `upsert` as well as `search`. That is strictly more
coverage than the shape it replaces.
"""

import os
import pathlib
import sys

import pytest
from stores import StorePair

from rag_adapters.fakes import FakeDenseStore, FakeLexicalStore
from rag_adapters.postgres import PostgresDenseStore, PostgresLexicalStore, PostgresPool


@pytest.fixture
def fake_stores() -> StorePair:
    return StorePair(
        dense=FakeDenseStore(),
        lexical=FakeLexicalStore(),
        # The fakes keep two independent collections, so both need writing.
        _seed_lexical=True,
    )


@pytest.fixture
async def pg_pool():
    """A migrated, empty database.

    Skips rather than errors when there is no DATABASE_URL, so
    `uv run pytest -m postgres` on a machine without a database reports a
    readable reason instead of a connection traceback.
    """
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        pytest.skip("DATABASE_URL is not set; start a pgvector container to run these")

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "db"))
    from migrate import apply_pending

    await apply_pending(dsn)

    pool = PostgresPool(dsn=dsn, min_size=1, max_size=5)
    await pool.open()
    # Truncate between tests rather than wrapping each in a rolled-back
    # transaction: the transaction approach fights pooling (every statement
    # must land on the same connection) and the suite is small enough that the
    # simpler thing is also the faster one to reason about.
    async with pool.pool.acquire() as conn:
        await conn.execute("truncate document, chunk, index_manifest restart identity cascade")
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def pg_stores(pg_pool) -> StorePair:
    return StorePair(
        dense=PostgresDenseStore(pg_pool),
        lexical=PostgresLexicalStore(pg_pool),
        # ADR-002: `tsv` is a generated column on the same row, so one write
        # populates both legs. index() only verifies — see its docstring.
        _seed_lexical=False,
    )


@pytest.fixture
def stores(request) -> StorePair:
    """Resolves the indirect parameter to the named fixture.

    Behaviour that is genuinely Postgres-specific — dimension enforcement,
    foreign keys, NaN from a zero vector — is not a port contract and lives in
    test_postgres_specifics.py rather than being smuggled in here behind a
    conditional.
    """
    return request.getfixturevalue(request.param)
