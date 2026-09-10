Lead → Install → License flow — design spec
Status: Draft 0.2 (revised per decisions below) · Owner: Zoya Khan · Component: Console — Tenant Management + Version Management employee agents
Note on how this file persists: memory isn't turned on for this account, so I won't automatically recall this file in a brand-new conversation — but within this conversation, and any time you re-upload or reference this exact file, I'll read, learn from, and revise it as we go. If you'd like me to recall project context automatically across sessions without re-sharing files, that's a toggle in Settings, not something I can turn on from here.
________________________________________
1. End-to-end flow
[Marketing site]
      | fills lead form (email, company, interest)
      v
[Console: Lead API] --creates prospect record, sends install link + one-time signup code (email)--> [Customer inbox]
      |
      v
[Customer runs installer]
      | Step 0: security & data-use notice — shown before anything is collected:
      |         "we never store your passwords" + "why we need the exact version"
      | Step 1: welcome + signup code confirms identity
      | Step 2: auto-detect ERP/software + version (registry, config files, DB probe)
      | Step 3: confirm/edit detected values; add anything undetectable
      | Step 4: policy questionnaire (approval routing, thresholds, approver contacts)
      v
[Installer -> Console: POST /v1/registrations]
      | payload: lead/signup code, erp_type, erp_version, other_tools[], policy_config
      v
[Console]
      | validates signup code
      | creates/updates tenant record
      | sets install_date = now, demo_start = now, demo_end = now + 7 days, status = trial
      | issues signed token (tenant-scoped, short-lived + refreshable)
      v
[Installer]
      | stores token (OS secret store, encrypted at rest)
      | registers connector as background service (systemd / Windows Service)
      | starts service
      v
[Connector service — background, outbound-only]
      | heartbeat every N minutes -> Console (status, connector_version, uptime)
      | pulls: entitlements, policy config, agent/sub-agent versions to run
      | pushes: ERP data (invoices, payments) per sub-agent needs
      v
[Payment event] --webhook--> [Console: Billing API]
      | updates plan, license_status = active, extends end_date per plan term
      | next heartbeat picks up new entitlements — no reinstall needed
Key principle: the connector never opens an inbound port. Every call it makes is outbound to your Console over HTTPS. This is what makes it approvable by a customer's IT team without a firewall exception request, and it's the pattern that lets old on-prem systems (Oracle EBS 10.2.8, QuickBooks Desktop 2020) sit behind a corporate firewall with no special network configuration.
________________________________________
1a. Step 0 — the security & data-use notice
Shown before the signup code prompt and before any technical or policy data is collected. Two claims, made explicitly rather than buried in a EULA:
1.	No password storage. The connector never asks for, transmits, or stores the customer's accounting-system login credentials. It connects through the token issued at registration, not through a password.
2.	Why the version matters. The exact software version is what lets support reproduce and fix an issue quickly instead of guessing — it's framed as a support-quality benefit to the customer, not just data collection for its own sake. This is also the honest reason: it's exactly what feeds the version-fingerprinted error_events table in Section 3.
Record an acknowledged_security_notice: true/false flag (with timestamp) on the registration payload. This gives you an audit trail confirming the disclosure was shown — useful both for support conversations ("yes, we told you upfront we don't touch your password") and for any compliance questionnaire a customer's IT/security team sends you later.
2. Auto-detection strategy (installer, Step 2)
Detection should be attempted before asking — the questionnaire is a fallback and confirmation step, not the primary source.
System	Detection approach	Fallback
Oracle EBS	Query FND_PRODUCT_GROUP / version view via the DB connection the customer provides	Ask customer to paste version string shown in EBS "About" screen
Oracle Fusion Cloud	REST API version endpoint (once that adapter exists)	N/A — API-based, always detectable
QuickBooks Desktop	Read version from local install path / registry key (Windows) if installer runs on the same machine	Dropdown of known versions (2018–2024)
QuickBooks Online	OAuth connection reveals company + version metadata automatically	N/A
Detected values are shown back to the customer for confirmation before submission — never silently trusted, since a wrong version tag poisons your error-correlation data.
When even the customer doesn't know the exact patch version: accept a major-version-only entry (e.g. "10.2.x" instead of "10.2.8"). Set erp_version_precision = major_only and needs_version_follow_up = true. This unblocks installation — a customer shouldn't be stuck at the install screen over a patch number — but it flags the record for a support/success follow-up to pin down the exact version before it's needed for real triage. Surface these flagged installations as a worklist in the Console (Tenant Management) so they don't silently pile up.
________________________________________
3. Data model
create table tenants (
    tenant_id           uuid primary key default gen_random_uuid(),
    company_name        text not null,
    signup_email        text not null,
    lead_source         text,                    -- e.g. 'marketing_site', 'referral'
    created_at          timestamptz not null default now()
);

create table installations (
    installation_id     uuid primary key default gen_random_uuid(),
    tenant_id           uuid not null references tenants(tenant_id),
    erp_type            text not null,            -- 'oracle_ebs', 'quickbooks_desktop', ...
    erp_version         text not null,             -- '10.2.8', or '10.2.x' if major-only
    erp_version_source  text not null,             -- 'auto_detected' | 'user_confirmed' | 'manual_entry'
    erp_version_precision text not null default 'exact',  -- 'exact' | 'major_only'
    needs_version_follow_up boolean not null default false, -- true when precision = 'major_only'
    connector_module    text not null,             -- 'oracle-ebs-adapter'
    connector_version   text not null,             -- 'v1.4.2'
    connectivity_mode   text not null default 'outbound_only', -- 'outbound_only' | 'inbound_allowed' — config flag, not a code change
    ar_agent_version    text not null,
    sub_agent_versions  jsonb not null,            -- {"cash_application": "v1.1", ...}
    policy_config_id    uuid references policy_configs(policy_config_id),
    install_date        timestamptz not null default now(),
    last_heartbeat_at    timestamptz,
    demo_start          timestamptz not null,
    demo_end            timestamptz not null,
    plan                text not null default 'trial',   -- 'trial' | 'starter' | 'pro' | ...
    payment_date        timestamptz,
    license_status      text not null default 'trial',   -- 'trial' | 'active' | 'expired' | 'suspended'
    renewal_reminders_sent  int not null default 0,       -- 0-3, see Section 6
    last_reminder_sent_at   timestamptz,
    acknowledged_security_notice_at timestamptz,
    token_id            uuid,                             -- reference to issued credential, never the raw token
    created_at          timestamptz not null default now()
);

create table policy_configs (
    policy_config_id    uuid primary key default gen_random_uuid(),
    tenant_id           uuid not null references tenants(tenant_id),
    version             int not null default 1,
    approval_rules      jsonb not null,     -- per action type: role, threshold, approver contact
    created_at          timestamptz not null default now()
);

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
error_events deliberately denormalizes the version fingerprint onto every row — so "show me every error on Oracle EBS 10.2.8 with connector v1.4.2" is a single indexed query, not a five-table join, when you're triaging under time pressure.
________________________________________
4. API contract (Console side)
POST /v1/leads
  body: { email, company_name, source }
  -> creates tenant (status=prospect), sends install link + signup_code by email
  returns: { tenant_id }

POST /v1/registrations
  body: {
    signup_code,
    erp_type, erp_version, erp_version_source, erp_version_precision,
    other_tools: [...],
    policy_config: { approval_rules: [...] },
    acknowledged_security_notice: true
  }
  -> validates signup_code, creates installation record,
     sets demo_start=now, demo_end=now+7d, license_status=trial,
     connectivity_mode=outbound_only (default; single config value, see Section 7)
  returns: { installation_id, token, entitlements }

POST /v1/heartbeat
  auth: bearer token (installation-scoped)
  body: { connector_version, status, metrics }
  -> updates last_heartbeat_at; if license_status=expired, entitlements
     tell the connector to halt entirely (see Section 6)
  returns: { entitlements, policy_config_version, agent_versions }
     (this is how a plan upgrade or policy edit reaches the connector —
      no reinstall, it picks it up on the next heartbeat)

POST /v1/errors
  auth: bearer token
  body: { sub_agent, error_code, error_detail }
  -> writes to error_events with the full version fingerprint attached server-side
     (never trust the client to self-report its own version — the server
      fills erp_version/connector_version/ar_agent_version from the
      installation record tied to the token)

POST /v1/billing/webhook
  auth: signature from payment provider
  body: (provider-specific payment event)
  -> on successful payment: license_status=active, plan=<purchased>,
     payment_date=now, extends end_date per plan term,
     resets renewal_reminders_sent=0 for the new term

[Internal, scheduled — not customer-facing]
License watcher job — runs hourly, see Section 6 for full logic
________________________________________
6. License expiry & renewal-reminder workflow
Decision: an expired license stops the connector entirely — not read-only, not degraded. No polling, no syncing, no sub-agent activity of any kind until the license is renewed. This is the simplest, least-ambiguous behavior for both support and the customer: "expired" means off, full stop, no partial states to reason about or explain.
Because that's a hard stop, the reminder workflow upstream of it matters more, not less — a customer who gets surprised by a sudden stop is a support ticket and a trust problem.
License watcher (internal scheduled job, runs hourly):
for each installation where license_status in ('trial', 'active'):
    days_remaining = (demo_end - now) in days

    if days_remaining <= reminder_threshold[renewal_reminders_sent]
       and renewal_reminders_sent < 3:
        send_renewal_email(
            to = tenant.signup_email,
            cc = OWNER_NOTIFICATION_EMAIL,   -- Zoya's address, config value
            reminder_number = renewal_reminders_sent + 1,
            days_remaining = days_remaining,
        )
        installation.renewal_reminders_sent += 1
        installation.last_reminder_sent_at = now

    if now > demo_end:
        installation.license_status = 'expired'
        -- next heartbeat response tells the connector to halt entirely
•	Three reminders before expiry, spaced across the countdown — e.g. at 3 days, 1 day, and the day of expiry (exact spacing is a config array, reminder_threshold, so you can tune it without a code change).
•	Every reminder CCs the owner (OWNER_NOTIFICATION_EMAIL — your address) so you always have visibility into which tenants are about to go dark, without having to check the Console proactively.
•	Idempotent by design — renewal_reminders_sent prevents double-sends if the job runs more often than the reminder cadence.
•	On payment, the billing webhook resets renewal_reminders_sent = 0 for the new term, so the next renewal cycle starts its countdown fresh.
•	This job's natural home is the Tenant Management employee agent, since it already owns demo_start/demo_end/plan — the reminder logic is an extension of data it already tracks, not a new subsystem.
________________________________________
7. Connectivity mode — confirmed outbound-only, implemented as a config value
Outbound-only is confirmed as the default and only supported mode for now. It's implemented as connectivity_mode on the installation record ('outbound_only' | 'inbound_allowed') rather than hardcoded, specifically so that if a future customer's environment genuinely requires something different, that's a config change and a support conversation — not a redesign of the connector or the registration flow. No other part of this spec changes.
________________________________________
9. Token lifetimes — confirmed
Access token: 1 hour. Sent with every heartbeat/sync call. A leak is contained within the hour — the token stops working on its own.
Refresh token: 30 days. Used only to silently obtain a new access token when the old one expires — no human re-entry required. Stored more carefully than the access token (it's the longer-lived credential). If a refresh token is ever known to be compromised, revoking it from the Console immediately kicks that one installation offline without touching any other tenant.
POST /v1/token/refresh
  body: { refresh_token }
  -> validates refresh_token is unexpired and not revoked,
     issues a new access_token (1h) and rotates the refresh_token (30d)
  returns: { access_token, refresh_token, access_token_expires_at }
Rotating the refresh token on every use (not just the access token) means a stolen old refresh token becomes useless the moment the legitimate connector does its next silent refresh — it can't be replayed later.
________________________________________
10. Decisions log
#	Question	Decision
1	Token lifetime & refresh	Resolved: 1 hour access token, 30 day refresh token, refresh token rotates on each use (Section 9)
2	Demo-expiry behavior	Resolved: expired = full stop, no degraded/read-only mode. Pre-expiry reminder workflow added (Section 6) to soften the hard stop
3	Detection-failure handling	Resolved: major-version-only accepted (e.g. "10.2.x"), flagged via needs_version_follow_up for later precision
4	Outbound-only connectivity	Resolved: confirmed as default; implemented as a config value (connectivity_mode), not a structural assumption

