-- Documents, page geometry and the ingestion queue.
-- Requirements: 5.8, 6.2

create table documents (
  id               uuid primary key default gen_random_uuid(),
  project_id       uuid not null references projects(id) on delete cascade,
  filename         text not null,
  content_hash     text,                       -- sha256 of file bytes, set on completion
  byte_size        bigint,
  page_count       int,
  status           text not null check (status in (
                     'awaiting_upload', 'pending_parse', 'parsing',
                     'indexing', 'ready', 'failed', 'deleted', 'purged'
                   )),
  failure_reason   text,
  s3_key_original  text not null,
  uploaded_by      uuid not null references users(id),
  created_at       timestamptz not null default now(),
  ready_at         timestamptz,
  deleted_at       timestamptz,
  purge_after      timestamptz
);

-- Duplicate detection is per project, and a deleted document does not block a
-- fresh upload of the same content. Requirement 5.8.
create unique index documents_project_content_uniq
  on documents (project_id, content_hash)
  where deleted_at is null and content_hash is not null;

create index documents_project_status_idx on documents (project_id, status);
create index documents_purge_idx on documents (purge_after) where deleted_at is not null;

create table document_pages (
  document_id  uuid not null references documents(id) on delete cascade,
  page_no      int not null check (page_no >= 1),
  width        numeric not null check (width > 0),   -- PDF points
  height       numeric not null check (height > 0),
  image_key    text not null,
  primary key (document_id, page_no)
);

create table ingestion_jobs (
  id            bigserial primary key,
  document_id   uuid not null references documents(id) on delete cascade,
  stage         text not null check (stage in ('parse', 'index')),
  status        text not null check (status in ('queued', 'running', 'done', 'failed')),
  attempts      int not null default 0,
  max_attempts  int not null default 3,
  last_error    text,
  run_after     timestamptz not null default now(),
  claimed_at    timestamptz,
  claimed_by    text,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

-- Partial indexes: the claim query only ever looks at queued rows, and the
-- reaper only ever looks at running rows.
create index ingestion_jobs_queued_idx on ingestion_jobs (run_after) where status = 'queued';
create index ingestion_jobs_running_idx on ingestion_jobs (claimed_at) where status = 'running';
create index ingestion_jobs_document_idx on ingestion_jobs (document_id);
