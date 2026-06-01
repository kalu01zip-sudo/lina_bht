alter table saved_routines
add column if not exists is_completed boolean not null default false,
add column if not exists completed_at timestamptz;
