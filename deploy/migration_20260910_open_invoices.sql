-- Real ERP adapter infrastructure for connector-push ERPs (added
-- 2026-09-10, for Microsoft Dynamics GP — see skills.md). QBO's server
-- pulls invoices directly from Intuit's API; GP has no such API, so
-- the connector is the only thing that can ever reach it, and there
-- was never a way for any connector to push a full open-AR-ledger
-- snapshot before this. A current-snapshot table like credit_limits/
-- customers, not an event log: ar_agent.py's _fetch_open_invoices()
-- reads this for any erp_type that isn't QBO. Full-replace semantics
-- on every sync (see console_registration_api.py's
-- /v1/sync/open-invoices) — the source of truth is whatever the
-- connector says is open right now, same as QBO's own
-- `WHERE Balance > 0` implicitly dropping paid invoices every cycle.
create table open_invoices (
    installation_id     uuid not null references installations(installation_id),
    invoice_id            text not null,
    reference              text,
    amount_due             numeric not null,
    due_date                date,
    bill_to_contact        text,
    customer_id            text,
    updated_at              timestamptz not null default now(),
    primary key (installation_id, invoice_id)
);

create index idx_open_invoices_installation_id on open_invoices(installation_id);
