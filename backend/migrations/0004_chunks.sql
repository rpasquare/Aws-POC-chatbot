-- Searchable chunks and the regions that let a citation be highlighted on a
-- page image.
-- Requirements: 7.6, 7.7

create table chunks (
  id            uuid primary key default gen_random_uuid(),
  document_id   uuid not null references documents(id) on delete cascade,
  project_id    uuid not null,                 -- denormalised for RLS and filtering
  ordinal       int not null,                  -- position within document
  text          text not null,
  heading_path  text,
  page_from     int not null,
  page_to       int not null,
  token_count   int not null,
  embedding     vector(512) not null,
  text_search   tsvector generated always as (to_tsvector('english', text)) stored,
  embed_model   text not null,                 -- guards Requirement 7.7
  created_at    timestamptz not null default now()
);

create table chunk_regions (
  id        bigserial primary key,
  chunk_id  uuid not null references chunks(id) on delete cascade,
  page_no   int not null,
  x0        numeric not null,
  y0        numeric not null,
  x1        numeric not null,
  y1        numeric not null
);

-- Approximate nearest-neighbour search over chunk embeddings. HNSW over
-- IVFFlat: better recall and latency at the cost of slower index build and
-- more memory, which is irrelevant at expected corpus sizes.
create index chunks_embedding_hnsw_idx on chunks using hnsw (embedding vector_cosine_ops);

-- Full-text search side of hybrid retrieval (Requirement 7.6).
create index chunks_text_search_idx on chunks using gin (text_search);

-- Project-scoped queries, and per-document chunk deletion ahead of a
-- rebuild (Requirement 6.12/11.3).
create index chunks_project_idx on chunks (project_id);
create index chunks_document_idx on chunks (document_id);

create index chunk_regions_chunk_idx on chunk_regions (chunk_id);
