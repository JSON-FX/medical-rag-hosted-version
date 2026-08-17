-- Schema for the hosted profile (ARCHITECTURE.md §5, ADR-002).
--
-- Dense and lexical retrieval hit the SAME ROW. That is the whole point of
-- putting both halves in one database: the local build's split between ChromaDB
-- and SQLite FTS5 made partial-ingestion states possible, where a chunk was
-- searchable lexically but not densely. A single row cannot disagree with
-- itself, so that entire class of bug — and the compensating deletes and
-- reconciliation command written to repair it — does not exist here.

create extension if not exists vector;

create table document (
  id            text primary key,       -- slug, e.g. 'metformin'
  title         text not null,
  source_set_id text,                   -- openFDA set_id, pinning the label revision
  ingested_at   timestamptz not null default now()
);

create table chunk (
  id          text primary key,         -- '{document_id}_{ordinal}', see contracts.make_chunk_id
  document_id text not null references document(id) on delete cascade,
  ordinal     int  not null,
  anchor      text not null,            -- page number, for citation resolution
  content     text not null,
  embedding   vector(768) not null,

  -- Two-argument to_tsvector with a LITERAL config. The one-argument form reads
  -- default_text_search_config and is therefore only STABLE, not IMMUTABLE, so
  -- Postgres rejects it in a generated column — with an error about
  -- immutability that does not mention the argument count.
  --
  -- 'english' is Porter-stemmed, matching the local build's FTS5
  -- tokenize='porter unicode61'. Tokenisation is close to like-for-like between
  -- the two profiles; it is the RANKING function that differs (ts_rank_cd is
  -- not BM25 — ADR-002), and that difference is measured in TICKET-9 rather
  -- than assumed negligible.
  tsv tsvector generated always as (to_tsvector('english', content)) stored,

  -- Makes ingestion idempotent: re-running upserts in place rather than
  -- duplicating. Equivalent to the primary key given how ids are constructed,
  -- and kept because it states the invariant the id shape merely implies.
  unique (document_id, ordinal)
);

-- The operator class must match the operator used at query time. An index built
-- with vector_cosine_ops is only used by ORDER BY ... <=> ...; querying with
-- <-> (L2) silently ignores this index AND returns a different metric, which
-- would make every gate threshold mean something other than what it says.
create index chunk_embedding_hnsw on chunk using hnsw (embedding vector_cosine_ops);
create index chunk_tsv_gin        on chunk using gin  (tsv);

-- The embedding model is part of the schema, not a runtime setting. On startup
-- the service compares the configured embedder against this row and refuses to
-- serve if they disagree: silently querying an index built by a different model
-- returns plausible-looking garbage, which is the worst failure mode available.
create table index_manifest (
  id                 int  primary key default 1,
  embedding_model_id text not null,
  dimension          int  not null,
  ingested_at        timestamptz not null,

  -- ARCHITECTURE.md §5 gives `id int primary key default 1`, which permits rows
  -- 2, 3, ... and makes "the" manifest ambiguous. There is exactly one index,
  -- so there is exactly one manifest.
  constraint index_manifest_is_a_singleton check (id = 1)
);
