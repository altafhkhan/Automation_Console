alter table cash_application_events add column if not exists processed_at timestamptz;

create table if not exists ar_tasks (
    task_id             uuid primary key default gen_random_uuid(),
    installation_id     uuid not null references installations(installation_id),
    sub_agent           text not null,
    task_type           text not null,
    status              text not null default 'pending',
    input_event_id      uuid references cash_application_events(event_id),
    result              jsonb,
    created_at          timestamptz not null default now(),
    resolved_at         timestamptz
);
create index if not exists idx_ar_tasks_installation_id on ar_tasks(installation_id);

create table if not exists approval_queue (
    approval_id         uuid primary key default gen_random_uuid(),
    installation_id     uuid not null references installations(installation_id),
    task_id             uuid references ar_tasks(task_id),
    action_type         text not null,
    amount              numeric,
    approver            text,
    status              text not null default 'pending',
    created_at          timestamptz not null default now(),
    resolved_at         timestamptz,
    resolved_by         text
);
create index if not exists idx_approval_queue_installation_id on approval_queue(installation_id);

create table if not exists audit_log (
    audit_id            uuid primary key default gen_random_uuid(),
    installation_id     uuid references installations(installation_id),
    agent               text not null,
    action               text not null,
    detail                jsonb not null default '{}'::jsonb,
    occurred_at           timestamptz not null default now()
);
create index if not exists idx_audit_log_installation_id on audit_log(installation_id);
