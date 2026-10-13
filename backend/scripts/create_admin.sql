-- Creates (or updates) a real admin account with a password, so you can
-- sign in via the real /sign-in page instead of the X-User-Id placeholder.
--
-- Replace the email and password hash below with your own before running.
-- Generate a hash with:
--   .venv/bin/python -c "from app.passwords import hash_password; print(hash_password('your-password'))"
--
-- Run against your local Postgres, e.g.:
--   docker exec -i <your-postgres-container> psql -U deal -d deal_workspace < backend/scripts/create_admin.sql
-- or paste the body into psql directly.

insert into users (email, external_id, password_hash)
values (
  'admin@example.com',
  'admin@example.com',
  '<paste your generated Argon2id hash here>'
)
on conflict (email) do update set password_hash = excluded.password_hash;

-- Make this user an admin on the existing dev project (same project id
-- used by dev_seed.sql). Adjust the project_id if you're using a
-- different one.
insert into project_members (project_id, user_id, role, added_by)
select
  '00000000-0000-0000-0000-0000000000a1',
  u.id,
  'admin',
  u.id
from users u
where u.email = 'admin@example.com'
on conflict (project_id, user_id) do update set role = excluded.role;
