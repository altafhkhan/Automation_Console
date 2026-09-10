-- Durable per-customer language/locale preference, keyed the same way
-- as credit_limits/credit_risk_assessments (installation_id +
-- customer_id). Lets a real, known language attach to a specific
-- debtor instead of every message being treated as unknown-language —
-- see ar_agent.py's _fetch_customer_language() and
-- _process_customer_inbox_event().
create table customer_locales (
    installation_id     uuid not null references installations(installation_id),
    customer_id          text not null,
    language              text not null,
    updated_at            timestamptz not null default now(),
    primary key (installation_id, customer_id)
);

create index idx_customer_locales_installation_id on customer_locales(installation_id);
