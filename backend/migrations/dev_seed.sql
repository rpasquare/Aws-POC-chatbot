-- Development fixtures only. Fixed identifiers so later sprints can run
-- against a known user and project before authentication exists.
-- Rerunnable.

insert into users (id, email, external_id, is_super_admin)
values ('00000000-0000-0000-0000-000000000001', 'dev@example.com', 'dev-external-id', true)
on conflict (id) do nothing;

insert into projects (id, name, created_by)
values (
  '00000000-0000-0000-0000-0000000000a1',
  'Development Project',
  '00000000-0000-0000-0000-000000000001'
)
on conflict (id) do nothing;

insert into project_members (project_id, user_id, role, added_by)
values (
  '00000000-0000-0000-0000-0000000000a1',
  '00000000-0000-0000-0000-000000000001',
  'admin',
  '00000000-0000-0000-0000-000000000001'
)
on conflict (project_id, user_id) do nothing;
