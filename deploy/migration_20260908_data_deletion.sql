create table if not exists data_deletion_log (
    deletion_id         uuid primary key default gen_random_uuid(),
    tenant_id           uuid not null,
    company_name        text not null,
    signup_email        text not null,
    requested_by        text not null,
    deleted_at          timestamptz not null default now(),
    row_counts          jsonb not null
);
