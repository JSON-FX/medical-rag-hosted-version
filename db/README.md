# Database

One Postgres database serves both retrieval legs (ADR-002): pgvector with an
HNSW index for dense, a generated `tsvector` column with a GIN index for
lexical. They are the same row, so they cannot disagree about what has been
ingested.

## Running migrations

```bash
export DATABASE_URL="postgresql://..."
uv run python db/migrate.py
```

Idempotent — applied files are recorded in `schema_migration` and skipped on
later runs. Each file is applied inside its own transaction, so a failure
leaves nothing half-applied.

## A local database

```bash
docker run -d --name medrag-pg \
  -e POSTGRES_PASSWORD=postgres -p 5432:5432 \
  pgvector/pgvector:pg17

export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/postgres"
uv run python db/migrate.py
uv run pytest -m postgres
```

The default `uv run pytest` deselects everything needing a database, so the
suite stays green with nothing running.

## Adding a migration

Numbered `.sql` files, applied in filename order. No Alembic: there is one
schema with no history and no branching, and autogenerate would add a second
source of truth for a table definition `docs/ARCHITECTURE.md` §5 already writes
out in full.
