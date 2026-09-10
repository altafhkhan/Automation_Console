create table if not exists dunning_sequences (
    sequence_id         uuid primary key default gen_random_uuid(),
    installation_id     uuid not null references installations(installation_id),
    invoice_id          text not null,
    contact             text,
    status              text not null default 'pending_activation',
    aging_bucket        text,
    current_step        text,
    last_action_at      timestamptz,
    created_at          timestamptz not null default now(),
    unique (installation_id, invoice_id)
);
create index if not exists idx_dunning_sequences_installation_id on dunning_sequences(installation_id);
