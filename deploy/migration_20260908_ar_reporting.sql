create table if not exists ar_reports (
    installation_id     uuid primary key references installations(installation_id),
    report              jsonb not null,
    generated_at        timestamptz not null default now()
);
