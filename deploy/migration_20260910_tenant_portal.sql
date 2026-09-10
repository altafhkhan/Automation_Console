-- Tenant self-service portal (added 2026-09-10). Fixes a real gap: only
-- one shared ADMIN_API_KEY (the vendor's own operator) could ever
-- approve anything for any tenant, which doesn't match CONTEXT.md's own
-- intent that approval authority belongs to the tenant's own staff
-- (its "human approval queue" / amount-tiered analyst-controller-CFO
-- language). tenant_sessions mirrors the existing `credentials` table's
-- pattern (random token, only its hash stored, TTL expiry) rather than
-- inventing a new scheme.
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
