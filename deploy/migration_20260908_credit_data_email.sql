create table if not exists credit_limits (
    installation_id     uuid not null references installations(installation_id),
    customer_id          text not null,
    credit_limit          numeric not null,
    updated_at            timestamptz not null default now(),
    primary key (installation_id, customer_id)
);

create table if not exists outbound_emails (
    email_id            uuid primary key default gen_random_uuid(),
    installation_id     uuid not null references installations(installation_id),
    to_address           text not null,
    subject               text not null,
    body                  text not null,
    status                text not null default 'pending',
    detail                text,
    created_at            timestamptz not null default now(),
    sent_at               timestamptz
);
create index if not exists idx_outbound_emails_installation_id on outbound_emails(installation_id);
create index if not exists idx_outbound_emails_pending on outbound_emails(installation_id, status) where status = 'pending';
