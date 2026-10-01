-- Extensions and access control.
-- Requirements: 2.1, 3.6, 4.1

create extension if not exists vector;
create extension if not exists pgcrypto;

create table users (
  id              uuid primary key default gen_random_uuid(),
  email           text unique not null,
  external_id     text unique not null,          -- identity provider subject
  is_super_admin  boolean not null default false,
  created_at      timestamptz not null default now()
);

create table projects (
  id          uuid primary key default gen_random_uuid(),
  name        text not null,
  created_by  uuid not null references users(id),
  created_at  timestamptz not null default now()
);

-- Roles come from membership alone. There is deliberately no per-user
-- permission table: Requirement 3.6 is enforced by its absence.
create table project_members (
  project_id  uuid not null references projects(id) on delete cascade,
  user_id     uuid not null references users(id) on delete cascade,
  role        text not null check (role in ('admin', 'editor', 'viewer')),
  added_by    uuid not null references users(id),
  added_at    timestamptz not null default now(),
  primary key (project_id, user_id)
);

create index project_members_user_idx on project_members (user_id);
