-- Conversations: sessions, their messages, and the citations attached to
-- assistant messages.
-- Requirements: 8.2, 8.9, 9.1

create table chat_sessions (
  id          uuid primary key default gen_random_uuid(),
  project_id  uuid not null references projects(id) on delete cascade,
  title       text not null,
  created_by  uuid not null references users(id),
  created_at  timestamptz not null default now(),
  last_active timestamptz not null default now()
);

create table chat_messages (
  id          uuid primary key default gen_random_uuid(),
  session_id  uuid not null references chat_sessions(id) on delete cascade,
  role        text not null check (role in ('user', 'assistant')),
  text        text not null,
  author_id   uuid references users(id),       -- null for assistant
  grounded    boolean not null default true,   -- false for refusal responses
  created_at  timestamptz not null default now()
);

create table message_citations (
  id          bigserial primary key,
  message_id  uuid not null references chat_messages(id) on delete cascade,
  marker      int not null,
  chunk_id    uuid references chunks(id) on delete set null,
  document_id uuid references documents(id) on delete set null,
  unique (message_id, marker)
);

-- `chunk_id` is `on delete set null` rather than cascade: Requirement 8.9
-- and 10.6 require that a conversation citing a since-purged document still
-- renders, with that citation marked unavailable. A null chunk reference is
-- the signal for that state, so the row must survive the chunk's deletion.

-- Listing a project's conversations ordered by recency (Requirement 9.3).
create index chat_sessions_project_idx on chat_sessions (project_id, last_active desc);

-- Listing a session's messages in order (Requirement 9.5).
create index chat_messages_session_idx on chat_messages (session_id, created_at);

-- Resolving a citation back to its message, and looking up every citation
-- pointing at a chunk when that chunk's document is purged (Requirement 8.9/10.6).
create index message_citations_message_idx on message_citations (message_id);
create index message_citations_chunk_idx on message_citations (chunk_id);
