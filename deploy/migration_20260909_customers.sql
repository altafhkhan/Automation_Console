-- Real customer master data (added 2026-09-09) — the first table in
-- this project where customer_id resolves to an actual name/email/
-- phone rather than being a bare string everywhere else references by
-- id alone. QBO-sourced for now (qbo_client.py's fetch_customers()),
-- via the same real-ERP-adapter path as _fetch_open_invoices(). Used
-- as a fallback contact for dunning when an invoice itself has no
-- BillEmail set — see ar_agent.py's _process_dunning_for_invoice().
create table customers (
    installation_id     uuid not null references installations(installation_id),
    customer_id           text not null,
    display_name          text,
    email                  text,
    phone                  text,
    updated_at             timestamptz not null default now(),
    primary key (installation_id, customer_id)
);

create index idx_customers_installation_id on customers(installation_id);
