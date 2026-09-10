create table if not exists dispute_cases (
    case_id             uuid primary key default gen_random_uuid(),
    installation_id     uuid not null references installations(installation_id),
    invoice_id          text not null,
    reason_code         text,
    amount              numeric not null,
    suggested_owner     text,
    status              text not null default 'open',
    resolution_type     text,
    opened_at           timestamptz not null default now(),
    resolved_at         timestamptz
);
create index if not exists idx_dispute_cases_installation_id on dispute_cases(installation_id);
create unique index if not exists idx_dispute_cases_open_per_invoice on dispute_cases(installation_id, invoice_id) where status != 'resolved';
