-- Schema for the Console registration API (installation-registration-design.md
-- Section 3). Runs automatically on first Postgres container creation via
-- docker-entrypoint-initdb.d — NOT a migration tool. Once the volume exists,
-- editing this file has no effect on a running deployment; a real migration
-- tool (Alembic, etc.) is the next step before this needs to change often.
--
-- Two intentional deviations from the design doc's data model, both noted
-- inline below: a `signup_codes` table (implementation detail, not in the
-- doc's model) and no `token_id` column on installations (see `credentials`).

create extension if not exists pgcrypto;  -- for gen_random_uuid()

create table tenants (
    tenant_id           uuid primary key default gen_random_uuid(),
    company_name        text not null,
    signup_email        text not null,
    lead_source         text,
    created_at          timestamptz not null default now()
);

-- Not in the design doc's data model — it documents the lead -> signup-code
-- flow but not how the one-time code itself is stored. Deleted on successful
-- registration (one-time use), same as the in-memory dict it replaces.
create table signup_codes (
    code                text primary key,
    tenant_id           uuid not null references tenants(tenant_id),
    created_at          timestamptz not null default now()
);

create table policy_configs (
    policy_config_id    uuid primary key default gen_random_uuid(),
    tenant_id           uuid not null references tenants(tenant_id),
    version             int not null default 1,
    approval_rules      jsonb not null,
    created_at          timestamptz not null default now()
);

create table installations (
    installation_id     uuid primary key default gen_random_uuid(),
    tenant_id           uuid not null references tenants(tenant_id),
    erp_type            text not null,
    erp_version         text not null,
    erp_version_source  text not null,
    erp_version_precision text not null default 'exact',
    needs_version_follow_up boolean not null default false,
    connector_module    text not null,
    connector_version   text not null,
    connectivity_mode   text not null default 'outbound_only',
    ar_agent_version    text not null,
    sub_agent_versions  jsonb not null default '{}'::jsonb,
    policy_config_id    uuid references policy_configs(policy_config_id),
    install_date        timestamptz not null default now(),
    last_heartbeat_at   timestamptz,
    demo_start          timestamptz not null,
    demo_end            timestamptz not null,
    plan                text not null default 'trial',
    payment_date        timestamptz,
    stripe_customer_id  text,
    stripe_subscription_id text,
    license_status      text not null default 'trial',
    renewal_reminders_sent int not null default 0,
    last_reminder_sent_at   timestamptz,
    acknowledged_security_notice_at timestamptz,
    created_at          timestamptz not null default now()
    -- No token_id column: the design doc describes it as "a reference to
    -- issued credential, never the raw token." Since a token pair maps 1:1
    -- to an installation (there's exactly one active pair at a time, see
    -- `credentials` below), that reference IS installation_id — a separate
    -- token_id column would just be a second name for the same row.
);

create index idx_installations_tenant_id on installations(tenant_id);

-- One active access/refresh token pair per installation. Rotated in place
-- on every refresh (design doc Section 9) rather than accumulating rows —
-- there is deliberately no history of past token pairs. Tokens themselves
-- are never stored: only a sha256 hash, so a database read alone can't be
-- replayed as a working credential.
create table credentials (
    installation_id            uuid primary key references installations(installation_id),
    access_token_hash           text not null,
    access_token_expires_at     timestamptz not null,
    refresh_token_hash          text not null,
    refresh_token_expires_at    timestamptz not null,
    updated_at                  timestamptz not null default now()
);

create index idx_credentials_access_token_hash on credentials(access_token_hash);
create index idx_credentials_refresh_token_hash on credentials(refresh_token_hash);

create table error_events (
    error_id            uuid primary key default gen_random_uuid(),
    installation_id     uuid not null references installations(installation_id),
    erp_type            text not null,
    erp_version         text not null,
    connector_version   text not null,
    ar_agent_version    text not null,
    sub_agent           text,
    error_code          text not null,
    error_detail        text,
    occurred_at         timestamptz not null default now()
);

-- error_events deliberately denormalizes the version fingerprint onto every
-- row (design doc Section 3) so "every error on EBS 10.2.8 + connector
-- v1.4.2" is one indexed query, not a join, during triage.
create index idx_error_events_fingerprint on error_events(erp_type, erp_version, connector_version);
create index idx_error_events_installation_id on error_events(installation_id);

-- What the connector pushes for the Cash Application sub-agent
-- (CONTEXT.md Section 2.2). ar_agent.py processes unprocessed rows
-- (processed_at is null) and stamps processed_at once a task exists for
-- them — see ar_tasks below for the actual matching outcome.
create table cash_application_events (
    event_id            uuid primary key default gen_random_uuid(),
    installation_id     uuid not null references installations(installation_id),
    payload             jsonb not null,
    received_at         timestamptz not null default now(),
    processed_at        timestamptz
);

create index idx_cash_application_events_installation_id on cash_application_events(installation_id);

-- What the connector pushes for the Invoice Delivery & Validation
-- sub-agent (CONTEXT.md Section 2.1). Same shape as
-- cash_application_events — archive-then-process, per sub-agent.
create table invoice_delivery_events (
    event_id            uuid primary key default gen_random_uuid(),
    installation_id     uuid not null references installations(installation_id),
    payload             jsonb not null,
    received_at         timestamptz not null default now(),
    processed_at        timestamptz
);

create index idx_invoice_delivery_events_installation_id on invoice_delivery_events(installation_id);

-- What the connector pushes for the Customer Inbox sub-agent (CONTEXT.md
-- Section 2.6) — inbound AR mailbox messages. Same archive-then-process
-- shape as cash_application_events/invoice_delivery_events.
create table customer_inbox_events (
    event_id            uuid primary key default gen_random_uuid(),
    installation_id     uuid not null references installations(installation_id),
    payload             jsonb not null,
    received_at         timestamptz not null default now(),
    processed_at        timestamptz
);

create index idx_customer_inbox_events_installation_id on customer_inbox_events(installation_id);

-- Per-task workflow state (CONTEXT.md: the AR Agent "maintains workflow
-- state per tenant"). One row per unit of work the AR Agent hands to a
-- sub-agent. input_event_id deliberately has no foreign key — it points
-- at a row in whichever *_events table this task_type's sub-agent reads
-- from (cash_application_events, invoice_delivery_events, ...), which
-- varies per sub-agent. A fixed FK per event-source table doesn't scale
-- to seven sub-agents; the source table is implied by sub_agent/task_type.
-- Status meanings: 'needs_approval' = the AR Agent could execute this but
-- policy requires sign-off first (e.g. applying unidentified cash);
-- 'exception' = the AR Agent can't resolve this at all — a human must
-- fix something upstream (e.g. in the ERP), there's no approve/reject
-- decision to make here.
create table ar_tasks (
    task_id             uuid primary key default gen_random_uuid(),
    installation_id     uuid not null references installations(installation_id),
    sub_agent           text not null,
    task_type           text not null,
    status              text not null default 'pending',  -- 'pending' | 'needs_approval' | 'completed' | 'exception'
    input_event_id      uuid,
    result              jsonb,
    created_at          timestamptz not null default now(),
    resolved_at         timestamptz
);

create index idx_ar_tasks_installation_id on ar_tasks(installation_id);

-- Per-invoice dunning state (CONTEXT.md 2.3). Unlike cash_application/
-- invoice_delivery, this sub-agent isn't event-driven by a connector push
-- — the AR Agent runs it every cycle over open invoices (_fetch_open_invoices)
-- and this table is what stops it re-sending the same reminder every 10
-- minutes: action only fires on a genuine status/step transition.
-- Status: 'pending_activation' | 'active' | 'pending_final_demand' |
-- 'paused' | 'completed'. 'pending_activation'/'pending_final_demand' are
-- the two CONTEXT.md approval gates (first activation, final-demand
-- letters) — the sequence sits there until a human resolves the linked
-- approval_queue row (no auto-promotion; there's no approval-resolution
-- UI yet, same gap noted elsewhere in this project).
create table dunning_sequences (
    sequence_id         uuid primary key default gen_random_uuid(),
    installation_id     uuid not null references installations(installation_id),
    invoice_id          text not null,
    contact             text,
    status              text not null default 'pending_activation',
    aging_bucket        text,
    current_step        text,  -- 'reminder' | 'follow_up' | 'escalation' | 'final_demand'
    last_action_at      timestamptz,
    created_at          timestamptz not null default now(),
    unique (installation_id, invoice_id)
);

create index idx_dunning_sequences_installation_id on dunning_sequences(installation_id);

-- Dispute case lifecycle (CONTEXT.md 2.4). Opened automatically when Cash
-- Application detects a short-pay (reference matches an invoice but
-- materially less was received) — see sub_agents/cash_application.py's
-- "short_pay" match_type and ar_agent.py's _open_dispute_case(). The
-- partial unique index (not a plain unique constraint) allows a new
-- dispute to reopen on the same invoice later, but never two open ones
-- at once. reason_code is null until real claim-text classification
-- exists (no AI/LLM wired up anywhere in this project yet).
-- forgiven_amount (added 2026-09-08): how much of `amount` a
-- credit_memo/write_off actually forgave. Null until resolved; may be
-- less than `amount` for a partial resolution, leaving amount -
-- forgiven_amount still genuinely owed — see console_registration_api.py's
-- _apply_approval_decision(), which resumes the linked dunning_sequences
-- row on that remainder instead of leaving it paused forever.
create table dispute_cases (
    case_id             uuid primary key default gen_random_uuid(),
    installation_id     uuid not null references installations(installation_id),
    invoice_id          text not null,
    reason_code         text,
    amount              numeric not null,
    suggested_owner     text,
    status              text not null default 'open',  -- 'open' | 'sla_breached' | 'pending_resolution' | 'resolved'
    resolution_type     text,  -- 'credit_memo' | 'write_off' | 'dismissed', set only on resolution
    forgiven_amount     numeric check (forgiven_amount is null or (forgiven_amount >= 0 and forgiven_amount <= amount)),
    opened_at           timestamptz not null default now(),
    resolved_at         timestamptz
);

create index idx_dispute_cases_installation_id on dispute_cases(installation_id);
create unique index idx_dispute_cases_open_per_invoice on dispute_cases(installation_id, invoice_id) where status != 'resolved';

-- Per-customer credit risk state (CONTEXT.md 2.5). Same anti-spam
-- purpose as dunning_sequences: the AR Agent re-evaluates exposure every
-- cycle, and this table is what stops it re-recommending a hold on every
-- 10-minute tick — a recommendation only fires on a genuine risk_level
-- *transition*, not on every cycle the customer happens to still be
-- over_limit. last_recommendation tracks whether a hold is currently
-- outstanding, so a recovery below the limit only recommends a release
-- if one was actually held.
create table credit_risk_assessments (
    assessment_id       uuid primary key default gen_random_uuid(),
    installation_id     uuid not null references installations(installation_id),
    customer_id         text not null,
    exposure            numeric not null,
    credit_limit        numeric not null,
    risk_level          text not null default 'normal',  -- 'normal' | 'elevated' | 'over_limit'
    last_recommendation text,  -- 'credit_hold' | 'credit_release' | null
    last_assessed_at    timestamptz not null default now(),
    unique (installation_id, customer_id)
);

create index idx_credit_risk_assessments_installation_id on credit_risk_assessments(installation_id);

-- The human-in-the-loop gate (INSTRUCTIONS.md: "Require human approval
-- for high-risk financial actions"). approver = null means no approval
-- rule was configured for this action_type at install time — a real
-- policy gap the AR Agent surfaces rather than silently auto-approving.
create table approval_queue (
    approval_id         uuid primary key default gen_random_uuid(),
    installation_id     uuid not null references installations(installation_id),
    task_id             uuid references ar_tasks(task_id),
    action_type         text not null,
    amount              numeric,
    approver            text,
    status              text not null default 'pending',  -- 'pending' | 'approved' | 'rejected'
    created_at          timestamptz not null default now(),
    resolved_at         timestamptz,
    resolved_by         text
);

create index idx_approval_queue_installation_id on approval_queue(installation_id);

-- Monitoring & Audit employee agent (ARCHITECTURE.md). Every Master
-- Agent / AR Agent / sub-agent decision lands here — this is the
-- "comprehensive audit logs" security requirement, not a nice-to-have.
create table audit_log (
    audit_id            uuid primary key default gen_random_uuid(),
    installation_id     uuid references installations(installation_id),
    agent               text not null,
    action              text not null,
    detail              jsonb not null default '{}'::jsonb,
    occurred_at         timestamptz not null default now()
);

create index idx_audit_log_installation_id on audit_log(installation_id);

-- Latest AR Reporting & Forecast snapshot (CONTEXT.md 2.7) — one row per
-- installation, refreshed (upserted) every Master Agent cycle. No
-- approval gate at all for this sub-agent ("Approval gates: None —
-- read-only" per CONTEXT.md), so unlike every other sub-agent's tables
-- this one never touches ar_tasks/approval_queue. Only the latest
-- snapshot is kept — a history-of-reports table (for real trending over
-- time) isn't built; generated_at is this table's own audit trail.
create table ar_reports (
    installation_id     uuid primary key references installations(installation_id),
    report              jsonb not null,
    generated_at        timestamptz not null default now()
);

-- Credit Risk's data source (CONTEXT.md 2.5, "Integrations: ERP ...").
-- Connector-pushed like cash_application_events, via /v1/sync/credit-limits
-- and connector_daemon.py's _pull_credit_limits() — a stub returning []
-- until a real ERP adapter exists, same honesty pattern as everywhere
-- else. ar_agent.py's _fetch_credit_limit() reads this instead of
-- returning None unconditionally.
create table credit_limits (
    installation_id     uuid not null references installations(installation_id),
    customer_id          text not null,
    credit_limit          numeric not null,
    updated_at            timestamptz not null default now(),
    primary key (installation_id, customer_id)
);

-- Durable per-customer language/locale preference (added 2026-09-09),
-- same key shape as credit_limits above. Populated from real signal
-- only: /v1/sync/credit-limits entries may optionally carry a
-- `language` field (ERP customer-master records commonly have a
-- correspondence-language attribute — same "real once an ERP adapter
-- exists" honesty as credit_limit itself), and a Customer Inbox
-- message that declares both `customer_id` and `language` in its own
-- payload upserts here too. ar_agent.py's _fetch_customer_language()
-- reads it; _process_customer_inbox_event() uses it to attach a known
-- language to an exception/approval for routing/triage, NOT to narrow
-- needs_escalation()'s own keyword check, which deliberately still
-- checks every language in ESCALATION_KEYWORDS regardless of any
-- known customer language — narrowing that check would reduce recall
-- (e.g. a Spanish-speaking customer code-switching in an English legal
-- term) for no real benefit.
create table customer_locales (
    installation_id     uuid not null references installations(installation_id),
    customer_id          text not null,
    language              text not null,
    updated_at            timestamptz not null default now(),
    primary key (installation_id, customer_id)
);

create index idx_customer_locales_installation_id on customer_locales(installation_id);

-- QuickBooks Online integration (added 2026-09-09, qbo_client.py) — the
-- first real ERP adapter in this project. QBO is a cloud REST API with
-- OAuth2 reachable directly from the Console, unlike Oracle EBS/
-- QuickBooks Desktop (ARCHITECTURE.md's on-prem "what runs locally"
-- split): there's no local connector step for QBO data itself, only
-- for this installation's other duties (heartbeat, email). qbo_oauth_states
-- is short-lived, existing only to survive the redirect round trip to
-- Intuit and back: `state` is the CSRF token Intuit echoes back on
-- /v1/qbo/oauth/callback, and it's how that callback knows which
-- installation this connection is for. qbo_connections is the durable
-- per-installation result once connected — realm_id is QBO's own
-- company identifier, required on every subsequent API call. Same "no
-- encryption at rest" honesty already disclosed for the rest of this
-- database (DATA_PROCESSING_DISCLOSURE.md), not a new gap.
create table qbo_oauth_states (
    state               text primary key,
    installation_id     uuid not null references installations(installation_id),
    created_at          timestamptz not null default now(),
    expires_at          timestamptz not null
);

create table qbo_connections (
    installation_id             uuid primary key references installations(installation_id),
    realm_id                     text not null,
    access_token                 text not null,
    refresh_token                text not null,
    access_token_expires_at      timestamptz not null,
    refresh_token_expires_at     timestamptz not null,
    connected_at                  timestamptz not null default now(),
    updated_at                    timestamptz not null default now()
);

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

-- The send-side of email integration. ARCHITECTURE.md: "Never request
-- customer passwords" — so the Console never holds SMTP/IMAP credentials
-- and can never send directly. It only records a request here; the
-- connector (which does hold those credentials, locally, on the
-- customer's machine) polls GET /v1/outbox/pending and sends via the
-- customer's own mail server, then POSTs back status via
-- /v1/outbox/{id}/ack. Every *_email() call site in ar_agent.py and
-- console_registration_api.py inserts here instead of actually sending.
create table outbound_emails (
    email_id            uuid primary key default gen_random_uuid(),
    installation_id     uuid not null references installations(installation_id),
    to_address           text not null,
    subject               text not null,
    body                  text not null,
    status                text not null default 'pending',  -- 'pending' | 'sent' | 'failed'
    detail                text,
    created_at            timestamptz not null default now(),
    sent_at               timestamptz
);

create index idx_outbound_emails_installation_id on outbound_emails(installation_id);
create index idx_outbound_emails_pending on outbound_emails(installation_id, status) where status = 'pending';

-- Evidentiary record of a tenant data-deletion request (closes Section 7
-- of DATA_PROCESSING_DISCLOSURE.md — a real erasure mechanism, not a
-- documented promise with nothing behind it). Deliberately NOT linked by
-- foreign key to tenants — by the time this row is queried, that tenant
-- row is gone. company_name/signup_email are snapshotted here because
-- there is nowhere else left to look them up afterward. row_counts is
-- the actual per-table count of what was deleted, proof the request did
-- something rather than just flipping a flag.
create table data_deletion_log (
    deletion_id         uuid primary key default gen_random_uuid(),
    tenant_id           uuid not null,
    company_name        text not null,
    signup_email        text not null,
    requested_by        text not null,
    deleted_at          timestamptz not null default now(),
    row_counts          jsonb not null
);

-- Tenant self-service portal (added 2026-09-10). Fixes a real gap: only
-- one shared ADMIN_API_KEY (the vendor's own operator) could ever
-- approve anything for any tenant, which doesn't match CONTEXT.md's own
-- intent that approval authority belongs to the tenant's own staff
-- (its "human approval queue" / amount-tiered analyst-controller-CFO
-- language). tenant_sessions mirrors the existing `credentials` table's
-- pattern (random token, only its hash stored, TTL expiry) rather than
-- inventing a new scheme. Authority granularity in v1: any of a
-- tenant's own users can act on any item in their own tenant's queue —
-- no per-named-approver enforcement yet (a disclosed, deliberate
-- simplification, matching how the single admin key's granularity
-- already worked; see console_registration_api.py's /v1/portal/*
-- endpoints).
create table tenant_users (
    user_id             uuid primary key default gen_random_uuid(),
    tenant_id           uuid not null references tenants(tenant_id),
    email               text not null,
    role                text,  -- informational only (e.g. "controller") -- not enforced in v1
    password_hash       text,  -- null until the invite is accepted
    invite_token_hash   text,
    invite_expires_at   timestamptz,
    created_at          timestamptz not null default now(),
    last_login_at       timestamptz,
    unique (tenant_id, email)
);

create table tenant_sessions (
    session_token_hash  text primary key,
    user_id             uuid not null references tenant_users(user_id),
    expires_at          timestamptz not null,
    created_at          timestamptz not null default now()
);

create index idx_tenant_users_tenant_id on tenant_users(tenant_id);
create index idx_tenant_sessions_user_id on tenant_sessions(user_id);

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
