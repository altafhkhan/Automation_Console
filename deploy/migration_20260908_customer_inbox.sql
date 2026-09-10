create table if not exists customer_inbox_events (
    event_id            uuid primary key default gen_random_uuid(),
    installation_id     uuid not null references installations(installation_id),
    payload             jsonb not null,
    received_at         timestamptz not null default now(),
    processed_at        timestamptz
);
create index if not exists idx_customer_inbox_events_installation_id on customer_inbox_events(installation_id);
