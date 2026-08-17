"""Apply pending SQL migrations. Idempotent.

Deliberately not Alembic. There is one schema, no history to migrate and no
branching; autogenerate would add a dependency and a second source of truth for
a table definition ARCHITECTURE.md §5 already writes out in full.

    DATABASE_URL=postgresql://... uv run python db/migrate.py
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys

import asyncpg

MIGRATIONS = pathlib.Path(__file__).parent / "migrations"

BOOTSTRAP = """
create table if not exists schema_migration (
  filename   text primary key,
  applied_at timestamptz not null default now()
)
"""


async def apply_pending(dsn: str) -> list[str]:
    """Apply every migration not yet recorded. Returns what was applied."""
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(BOOTSTRAP)
        applied = {r["filename"] for r in await conn.fetch("select filename from schema_migration")}

        pending = [p for p in sorted(MIGRATIONS.glob("*.sql")) if p.name not in applied]
        for path in pending:
            # One transaction per file, so a failure leaves nothing
            # half-applied. Postgres has transactional DDL; use it.
            async with conn.transaction():
                await conn.execute(path.read_text(encoding="utf-8"))
                await conn.execute("insert into schema_migration (filename) values ($1)", path.name)
        return [p.name for p in pending]
    finally:
        await conn.close()


async def main() -> int:
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1

    applied = await apply_pending(dsn)
    if not applied:
        print("no pending migrations")
    else:
        for name in applied:
            print(f"applied {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
