-- Project invites and the minimal real-authentication primitives needed to
-- accept one: a password on users, and a session table.
-- Requirements: 1.1, 1.5, 2.2, 5.1, 5.2 (project-invites spec)

alter table users
  add column password_hash text;  -- null until a password is first set

create table invites (
  id            uuid primary key default gen_random_uuid(),
  project_id    uuid not null references projects(id) on delete cascade,
  email         text not null,
  role          text not null check (role in ('editor', 'viewer')),
  invited_by    uuid not null references users(id),
  status        text not null check (status in ('pending', 'accepted', 'cancelled')),
  token_hash    text not null unique,
  created_at    timestamptz not null default now(),
  accepted_at   timestamptz,
  cancelled_at  timestamptz
);

-- At most one pending invite per (project, email). Requirement 1.5.
create unique index invites_pending_project_email_uniq
  on invites (project_id, email)
  where status = 'pending';

create index invites_project_idx on invites (project_id);

create table sessions (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references users(id) on delete cascade,
  token_hash  text not null unique,
  created_at  timestamptz not null default now(),
  expires_at  timestamptz not null
);

create index sessions_user_idx on sessions (user_id);
