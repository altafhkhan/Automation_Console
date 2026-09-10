alter table ar_tasks drop constraint if exists ar_tasks_input_event_id_fkey;

create table if not exists invoice_delivery_events (
    event_id            uuid primary key default gen_random_uuid(),
    installation_id     uuid not null references installations(installation_id),
    payload             jsonb not null,
    received_at         timestamptz not null default now(),
    processed_at        timestamptz
);
create index if not exists idx_invoice_delivery_events_installation_id on invoice_delivery_events(installation_id);
