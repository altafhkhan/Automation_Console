-- Dedup tracking for QBO-sourced Cash Application events (added
-- 2026-09-09). One real QBO Payment can apply to several invoices at
-- once (Line[].LinkedTxn) — ar_agent.py's _sync_qbo_payments()
-- decomposes each into its own cash_application_events row, one per
-- (payment, line) pair, since cash_application.match() has no concept
-- of one event resolving to more than one invoice. This table is what
-- stops the same real payment line from being turned into a brand new
-- event every 10-minute cycle forever.
create table qbo_synced_payments (
    installation_id     uuid not null references installations(installation_id),
    qbo_payment_id        text not null,
    qbo_line_key           text not null,
    event_id                uuid not null references cash_application_events(event_id),
    synced_at               timestamptz not null default now(),
    primary key (installation_id, qbo_payment_id, qbo_line_key)
);
