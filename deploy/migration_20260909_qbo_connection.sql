-- QuickBooks Online integration (qbo_client.py). Two tables:
--
-- qbo_oauth_states: short-lived, one row per in-flight "Connect
-- QuickBooks" attempt. Exists only to survive the redirect round trip
-- to Intuit and back — state is the CSRF token Intuit echoes back on
-- /v1/qbo/oauth/callback, and it's what tells the callback which
-- installation this connection is for (Intuit's callback carries no
-- other identifying info we control). Deleted once consumed, or simply
-- left to expire (expires_at) if the tenant never completes consent.
--
-- qbo_connections: the tenant-specific OAuth tokens, one row per
-- installation once they've actually connected. realm_id is QBO's own
-- company identifier, required on every subsequent API call. Same "no
-- encryption at rest" honesty already disclosed for everything else in
-- this database (DATA_PROCESSING_DISCLOSURE.md) — not a new gap, just
-- the same one applying to a new table.
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
