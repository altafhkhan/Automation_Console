"""
Console-side registration API — scaffold, not production-ready.

Implements the four core endpoints from installation-registration-design.md:
  POST /v1/leads
  POST /v1/registrations
  POST /v1/token/refresh
  POST /v1/heartbeat
  POST /v1/errors

Plus one internal scheduled job (not an HTTP endpoint — run via cron /
APScheduler / Celery beat, hourly):
  run_license_watcher() — sends up to 3 renewal reminders before expiry
  (CCing the owner), then flips license_status to 'expired' once demo_end
  has passed. See design doc Section 6. Invoke it standalone via
  `python console_registration_api.py` (see the __main__ block) — that's
  what deploy/DEPLOY.md's cron entry calls.

Stack: FastAPI + asyncpg against Postgres (schema in deploy/schema.sql,
matching installation-registration-design.md Section 3, with two
deviations documented inline in that file). Auth, rate limiting, and the
billing webhook are stubbed with TODOs — fill in once the payment
provider is chosen.

Run: uvicorn console_registration_api:app --reload
(requires DATABASE_URL pointing at a Postgres instance with schema.sql applied)
"""

from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any
import hashlib
import json
import os
import secrets

import asyncpg
import bcrypt
import stripe
from fastapi import Cookie, Depends, FastAPI, HTTPException, Header, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import ar_agent
import qbo_client
from console_config import CONSOLE_BASE_URL
from db import create_pool
from qbo_config import qbo_configured
from stripe_config import (
    RENEWAL_TERM_DAYS,
    STRIPE_PRICE_ID,
    STRIPE_SECRET_KEY,
    STRIPE_WEBHOOK_SECRET,
    stripe_configured,
)

stripe.api_key = STRIPE_SECRET_KEY  # no-op until deploy/.env sets a real key

DEMO_PERIOD_DAYS = 7
OWNER_NOTIFICATION_EMAIL = "zoya@yourcompany.com"  # TODO: pull from real config/secrets, not hardcoded
REMINDER_THRESHOLDS_DAYS = [3, 1, 0]  # reminder N fires when days_remaining <= REMINDER_THRESHOLDS_DAYS[N]
ACCESS_TOKEN_TTL = timedelta(hours=1)
REFRESH_TOKEN_TTL = timedelta(days=30)
PORTAL_SESSION_TTL = timedelta(days=7)
PORTAL_INVITE_TTL = timedelta(days=7)
PORTAL_SESSION_COOKIE = "portal_session"

# Operator-only dashboard access — one shared secret, not a real user
# system. This is NOT tenant-facing auth for day-to-day AR data — see
# /v1/portal/* below for that (added 2026-09-10, tenant_users/
# tenant_sessions) — but it's still how a vendor operator invites a
# tenant's first portal user and handles support/ops work outside the
# portal's scope (tenant management, deletion, QBO connect, credit
# limits — none of that is tenant self-service, by design).
ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await create_pool()
    yield
    await app.state.pool.close()


app = FastAPI(title="AR Automation Console — Registration API", lifespan=lifespan)
app.mount("/admin", StaticFiles(directory="static/admin", html=True), name="admin")
app.mount("/portal", StaticFiles(directory="static/portal", html=True), name="portal")


async def get_db():
    async with app.state.pool.acquire() as conn:
        yield conn


def _require_admin(authorization: str | None = Header(default=None)) -> None:
    if not ADMIN_API_KEY:
        raise HTTPException(500, "ADMIN_API_KEY not configured on the server")
    if not authorization or authorization.removeprefix("Bearer ") != ADMIN_API_KEY:
        raise HTTPException(401, "Invalid admin credentials")


async def _require_tenant_user(
    conn: asyncpg.Connection = Depends(get_db),
    portal_session: str | None = Cookie(default=None),
) -> asyncpg.Record:
    """
    Parallels _require_admin()/_require_installation() — the tenant
    portal's own auth gate (added 2026-09-10). Real accountability, not
    a shared secret: the session cookie resolves to exactly one
    tenant_users row, and every /v1/portal/* endpoint scopes its data
    to that row's own tenant_id, never a client-supplied one.
    """
    if not portal_session:
        raise HTTPException(401, "Not logged in")
    row = await conn.fetchrow(
        """
        select u.* from tenant_sessions s
        join tenant_users u on u.user_id = s.user_id
        where s.session_token_hash = $1 and s.expires_at > $2
        """,
        _hash_token(portal_session), datetime.now(timezone.utc),
    )
    if not row:
        raise HTTPException(401, "Session expired or invalid — please log in again")
    return row


async def _tenant_installation_ids(conn: asyncpg.Connection, tenant_id: Any) -> list[Any]:
    rows = await conn.fetch("select installation_id from installations where tenant_id = $1", tenant_id)
    return [r["installation_id"] for r in rows]


async def _approval_authorized(conn: asyncpg.Connection, approver: str | None, user: asyncpg.Record) -> bool:
    """
    Exact-approver enforcement (added 2026-09-10). `approver` is
    whatever ar_agent.py's _lookup_approver()/_check_requires_approval()
    resolved at the time the item was created (stored on
    approval_queue.approver), sourced from policy_configs — which the
    installer questionnaire now asks for as a real email specifically
    so it can be matched here.

    Two honest fallbacks to "any of this tenant's own users," not just
    one: no approver was ever configured (most items before
    installer_skeleton.py's questionnaire covered all action types),
    OR the stored value isn't a real account for this tenant (a name
    instead of an email from before that fix, or simply a typo) — both
    treated the same as "nothing specific to enforce," never a dead end
    nobody can act on.
    """
    if not approver:
        return True
    match = await conn.fetchrow(
        "select 1 from tenant_users where tenant_id = $1 and lower(email) = lower($2)",
        user["tenant_id"], approver,
    )
    if not match:
        return True
    return approver.lower() == user["email"].lower()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class LeadRequest(BaseModel):
    email: str
    company_name: str
    source: str = "marketing_site"


class RegistrationRequest(BaseModel):
    signup_code: str
    erp_type: str
    erp_version: str
    erp_version_source: str = Field(pattern="^(auto_detected|user_confirmed|manual_entry)$")
    erp_version_precision: str = Field(default="exact", pattern="^(exact|major_only)$")
    other_tools: list[str] = []
    connector_module: str
    connector_version: str
    policy_config: dict[str, Any]
    acknowledged_security_notice: bool = False


class HeartbeatRequest(BaseModel):
    connector_version: str
    status: str
    metrics: dict[str, Any] = {}


class ErrorReportRequest(BaseModel):
    sub_agent: str
    error_code: str
    error_detail: str = ""


class RefreshRequest(BaseModel):
    refresh_token: str


class CashApplicationSyncRequest(BaseModel):
    bank_feed_entries: list[dict[str, Any]]


class InvoiceDeliverySyncRequest(BaseModel):
    invoices: list[dict[str, Any]]


class CustomerInboxSyncRequest(BaseModel):
    messages: list[dict[str, Any]]


class CreditLimitsSyncRequest(BaseModel):
    credit_limits: list[dict[str, Any]]  # [{"customer_id": str, "credit_limit": float, "language"?: str}, ...]


class OpenInvoicesSyncRequest(BaseModel):
    # [{"invoice_id": str, "reference"?: str, "amount_due": float,
    #   "due_date"?: "YYYY-MM-DD", "bill_to_contact"?: str, "customer_id"?: str}, ...]
    open_invoices: list[dict[str, Any]]


class CustomersSyncRequest(BaseModel):
    # [{"customer_id": str, "display_name"?: str, "email"?: str, "phone"?: str}, ...]
    customers: list[dict[str, Any]]


class OutboxAckRequest(BaseModel):
    status: str = Field(pattern="^(sent|failed)$")
    detail: str = ""


class InboxResolveRequest(BaseModel):
    action: str = Field(pattern="^(send|reject)$")
    reply_body: str = ""
    resolved_by: str


class ApprovalResolveRequest(BaseModel):
    action: str = Field(pattern="^(approve|reject)$")
    resolved_by: str


class TenantDeletionRequest(BaseModel):
    requested_by: str
    confirm_company_name: str


class CreditLimitSetRequest(BaseModel):
    credit_limit: float = Field(ge=0)


class AdminTenantCreateRequest(BaseModel):
    company_name: str = Field(min_length=1)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class TenantUserInviteRequest(BaseModel):
    email: str
    role: str = ""


class InviteAcceptRequest(BaseModel):
    invite_token: str
    password: str = Field(min_length=8)


class PortalLoginRequest(BaseModel):
    email: str
    password: str


class PortalApprovalResolveRequest(BaseModel):
    action: str = Field(pattern="^(approve|reject)$")


class PortalInboxResolveRequest(BaseModel):
    action: str = Field(pattern="^(send|reject)$")
    reply_body: str = ""


class PortalCreditLimitRequest(BaseModel):
    proposed_limit: float = Field(ge=0)
    reason: str = ""


# ---------------------------------------------------------------------------
# Token helpers — raw tokens are never persisted, only a sha256 hash. A
# leaked database read alone can't be replayed as a working credential.
# ---------------------------------------------------------------------------
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def _issue_token_pair(conn: asyncpg.Connection, installation_id: str) -> dict[str, Any]:
    # TODO: replace bare random tokens with signed JWTs. Random strings are
    # a placeholder for wiring the access/refresh flow together — a real
    # implementation should sign+verify rather than do a hash lookup.
    now = datetime.now(timezone.utc)

    access_token = secrets.token_urlsafe(32)
    refresh_token = secrets.token_urlsafe(32)
    access_token_expires_at = now + ACCESS_TOKEN_TTL
    refresh_token_expires_at = now + REFRESH_TOKEN_TTL

    await conn.execute(
        """
        insert into credentials (
            installation_id, access_token_hash, access_token_expires_at,
            refresh_token_hash, refresh_token_expires_at, updated_at
        ) values ($1, $2, $3, $4, $5, $6)
        on conflict (installation_id) do update set
            access_token_hash = excluded.access_token_hash,
            access_token_expires_at = excluded.access_token_expires_at,
            refresh_token_hash = excluded.refresh_token_hash,
            refresh_token_expires_at = excluded.refresh_token_expires_at,
            updated_at = excluded.updated_at
        """,
        installation_id,
        _hash_token(access_token),
        access_token_expires_at,
        _hash_token(refresh_token),
        refresh_token_expires_at,
        now,
    )

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "access_token_expires_at": access_token_expires_at.isoformat(),
    }


async def _require_installation(
    conn: asyncpg.Connection, authorization: str | None
) -> asyncpg.Record:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token")
    token = authorization.removeprefix("Bearer ")

    row = await conn.fetchrow(
        """
        select i.*, c.access_token_expires_at
        from credentials c
        join installations i on i.installation_id = c.installation_id
        where c.access_token_hash = $1
        """,
        _hash_token(token),
    )
    if not row:
        raise HTTPException(401, "Invalid access token")
    if datetime.now(timezone.utc) > row["access_token_expires_at"]:
        raise HTTPException(401, "Access token expired — use /v1/token/refresh")
    return row


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
async def _create_tenant_with_signup_code(
    conn: asyncpg.Connection, company_name: str, email: str, source: str
) -> tuple[Any, str]:
    """
    A tenant row plus the one-time signup code its installer will
    register with — shared by the public lead form and the Admin
    console's "Add Customer" button so both produce the same thing.
    """
    async with conn.transaction():
        tenant_id = await conn.fetchval(
            """
            insert into tenants (company_name, signup_email, lead_source)
            values ($1, $2, $3)
            returning tenant_id
            """,
            company_name,
            email,
            source,
        )
        signup_code = secrets.token_hex(8)
        await conn.execute(
            "insert into signup_codes (code, tenant_id) values ($1, $2)",
            signup_code,
            tenant_id,
        )
    return tenant_id, signup_code


@app.post("/v1/leads")
async def create_lead(req: LeadRequest, conn: asyncpg.Connection = Depends(get_db)):
    tenant_id, signup_code = await _create_tenant_with_signup_code(conn, req.company_name, req.email, req.source)

    # TODO: send email with install-link + signup_code via your mail provider.
    # send_install_email(req.email, signup_code)
    print(f"[lead created] tenant_id={tenant_id} signup_code={signup_code} (would be emailed to {req.email})")

    return {"tenant_id": str(tenant_id)}


@app.post("/v1/registrations")
async def register_installation(req: RegistrationRequest, conn: asyncpg.Connection = Depends(get_db)):
    if not req.acknowledged_security_notice:
        raise HTTPException(400, "Security & data-use notice must be acknowledged before registration")

    now = datetime.now(timezone.utc)

    async with conn.transaction():
        signup_row = await conn.fetchrow(
            "select tenant_id from signup_codes where code = $1", req.signup_code
        )
        if not signup_row:
            raise HTTPException(400, "Invalid or already-used signup code")
        tenant_id = signup_row["tenant_id"]

        policy_config_id = await conn.fetchval(
            """
            insert into policy_configs (tenant_id, version, approval_rules)
            values ($1, $2, $3)
            returning policy_config_id
            """,
            tenant_id,
            req.policy_config.get("version", 1),
            req.policy_config.get("approval_rules", []),
        )

        installation_id = await conn.fetchval(
            """
            insert into installations (
                tenant_id, erp_type, erp_version, erp_version_source,
                erp_version_precision, needs_version_follow_up,
                connector_module, connector_version, ar_agent_version,
                policy_config_id, demo_start, demo_end,
                acknowledged_security_notice_at
            ) values (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13
            )
            returning installation_id
            """,
            tenant_id,
            req.erp_type,
            req.erp_version,
            req.erp_version_source,
            req.erp_version_precision,
            req.erp_version_precision == "major_only",
            req.connector_module,
            req.connector_version,
            "v1.0",  # TODO: pull current default from Version Management
            policy_config_id,
            now,
            now + timedelta(days=DEMO_PERIOD_DAYS),
            now,
        )

        # One-time use — deleting inside the same transaction as the insert
        # above means a retried/duplicate request can't register twice.
        await conn.execute("delete from signup_codes where code = $1", req.signup_code)

        token = await _issue_token_pair(conn, installation_id)

    installation = await conn.fetchrow(
        "select * from installations where installation_id = $1", installation_id
    )
    return {
        "installation_id": str(installation_id),
        **token,
        "entitlements": _entitlements_for(installation),
    }


@app.post("/v1/token/refresh")
async def refresh_token(req: RefreshRequest, conn: asyncpg.Connection = Depends(get_db)):
    row = await conn.fetchrow(
        "select installation_id, refresh_token_expires_at from credentials where refresh_token_hash = $1",
        _hash_token(req.refresh_token),
    )
    if not row:
        raise HTTPException(401, "Invalid refresh token")
    if datetime.now(timezone.utc) > row["refresh_token_expires_at"]:
        raise HTTPException(401, "Refresh token expired — installation must re-register")

    # Rotate: the old refresh token's hash is overwritten by _issue_token_pair
    # (on conflict do update), so a captured old one can't be replayed even
    # if it was intercepted in transit before this call completed.
    return await _issue_token_pair(conn, row["installation_id"])


@app.post("/v1/heartbeat")
async def heartbeat(
    req: HeartbeatRequest,
    authorization: str | None = Header(default=None),
    conn: asyncpg.Connection = Depends(get_db),
):
    installation = await _require_installation(conn, authorization)

    # Expiry check happens server-side on every heartbeat — the connector
    # never decides its own license status locally.
    await _refresh_license_status(conn, installation)

    await conn.execute(
        """
        update installations
        set last_heartbeat_at = $1, connector_version = $2
        where installation_id = $3
        """,
        datetime.now(timezone.utc),
        req.connector_version,
        installation["installation_id"],
    )

    installation = await conn.fetchrow(
        "select * from installations where installation_id = $1", installation["installation_id"]
    )
    policy_config_version = await conn.fetchval(
        "select version from policy_configs where policy_config_id = $1",
        installation["policy_config_id"],
    )

    return {
        "entitlements": _entitlements_for(installation),
        "policy_config_version": policy_config_version,
        "agent_versions": {
            "ar_agent": installation["ar_agent_version"],
            **installation["sub_agent_versions"],
        },
    }


@app.post("/v1/errors")
async def report_error(
    req: ErrorReportRequest,
    authorization: str | None = Header(default=None),
    conn: asyncpg.Connection = Depends(get_db),
):
    installation = await _require_installation(conn, authorization)

    # Version fingerprint is filled server-side from the installation
    # record, never taken from the client payload — this is the guarantee
    # that error_events data is trustworthy for triage.
    await conn.execute(
        """
        insert into error_events (
            installation_id, erp_type, erp_version, connector_version,
            ar_agent_version, sub_agent, error_code, error_detail
        ) values ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        installation["installation_id"],
        installation["erp_type"],
        installation["erp_version"],
        installation["connector_version"],
        installation["ar_agent_version"],
        req.sub_agent,
        req.error_code,
        req.error_detail,
    )
    return {"status": "recorded"}


@app.post("/v1/sync/cash-application")
async def sync_cash_application(
    req: CashApplicationSyncRequest,
    authorization: str | None = Header(default=None),
    conn: asyncpg.Connection = Depends(get_db),
):
    installation = await _require_installation(conn, authorization)

    # No matching logic here — this endpoint only archives what the
    # connector pushed. ar_agent.py's scheduled cycle does the actual
    # matching against cash_application_events rows with processed_at is null.
    for entry in req.bank_feed_entries:
        await conn.execute(
            "insert into cash_application_events (installation_id, payload) values ($1, $2)",
            installation["installation_id"],
            entry,
        )

    return {"status": "recorded", "count": len(req.bank_feed_entries)}


@app.post("/v1/sync/invoice-delivery")
async def sync_invoice_delivery(
    req: InvoiceDeliverySyncRequest,
    authorization: str | None = Header(default=None),
    conn: asyncpg.Connection = Depends(get_db),
):
    installation = await _require_installation(conn, authorization)

    # Same archive-then-process split as /v1/sync/cash-application —
    # ar_agent.py's scheduled cycle does the actual validation.
    for invoice in req.invoices:
        await conn.execute(
            "insert into invoice_delivery_events (installation_id, payload) values ($1, $2)",
            installation["installation_id"],
            invoice,
        )

    return {"status": "recorded", "count": len(req.invoices)}


@app.post("/v1/sync/customer-inbox")
async def sync_customer_inbox(
    req: CustomerInboxSyncRequest,
    authorization: str | None = Header(default=None),
    conn: asyncpg.Connection = Depends(get_db),
):
    installation = await _require_installation(conn, authorization)

    # Same archive-then-process split as the other /v1/sync/* endpoints —
    # ar_agent.py's scheduled cycle does the actual escalation/classification.
    for message in req.messages:
        await conn.execute(
            "insert into customer_inbox_events (installation_id, payload) values ($1, $2)",
            installation["installation_id"],
            message,
        )

    return {"status": "recorded", "count": len(req.messages)}


@app.post("/v1/sync/credit-limits")
async def sync_credit_limits(
    req: CreditLimitsSyncRequest,
    authorization: str | None = Header(default=None),
    conn: asyncpg.Connection = Depends(get_db),
):
    installation = await _require_installation(conn, authorization)

    # Reference data, not an event queue — upserted directly rather than
    # archived-then-processed like the other /v1/sync/* endpoints.
    # ar_agent.py's Credit Risk sub-agent reads this table each cycle.
    for entry in req.credit_limits:
        await conn.execute(
            """
            insert into credit_limits (installation_id, customer_id, credit_limit, updated_at)
            values ($1, $2, $3, $4)
            on conflict (installation_id, customer_id) do update set
                credit_limit = excluded.credit_limit, updated_at = excluded.updated_at
            """,
            installation["installation_id"], entry["customer_id"], entry["credit_limit"], datetime.now(timezone.utc),
        )
        # Optional — most ERP customer-master records carry a
        # correspondence-language field where credit limits don't say
        # anything about language at all; take it when the connector
        # happens to send it, same "real once the ERP side sends it"
        # honesty as credit_limit itself.
        language = entry.get("language")
        if language:
            await ar_agent._upsert_customer_language(
                conn, installation["installation_id"], entry["customer_id"], language
            )

    return {"status": "recorded", "count": len(req.credit_limits)}


@app.post("/v1/sync/open-invoices")
async def sync_open_invoices(
    req: OpenInvoicesSyncRequest,
    authorization: str | None = Header(default=None),
    conn: asyncpg.Connection = Depends(get_db),
):
    """
    Added 2026-09-10 for Microsoft Dynamics GP — the first connector-
    push path for ar_agent.py's _fetch_open_invoices(), which before
    this only ever had a direct-fetch path for QBO (Console-to-cloud)
    or an unconditional [] for everything else (no connector could
    reach GP's SQL Server FROM the Console the way it can Intuit's
    public API). Full-replace, not upsert, and deliberately so: unlike
    credit_limits/customers (which don't "expire"), an invoice that's
    no longer in this payload has been paid off or otherwise closed in
    the ERP, and should stop being open AR the instant the connector
    says so — same effective behavior QBO's own `WHERE Balance > 0`
    already has (a paid invoice just silently stops appearing).
    """
    installation = await _require_installation(conn, authorization)

    async with conn.transaction():
        await conn.execute("delete from open_invoices where installation_id = $1", installation["installation_id"])
        for entry in req.open_invoices:
            due_date_str = entry.get("due_date")
            due_date = date.fromisoformat(due_date_str) if due_date_str else None
            await conn.execute(
                """
                insert into open_invoices (installation_id, invoice_id, reference, amount_due,
                    due_date, bill_to_contact, customer_id, updated_at)
                values ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                installation["installation_id"], str(entry["invoice_id"]), entry.get("reference"),
                entry["amount_due"], due_date, entry.get("bill_to_contact"),
                entry.get("customer_id"), datetime.now(timezone.utc),
            )

    return {"status": "recorded", "count": len(req.open_invoices)}


@app.post("/v1/sync/customers")
async def sync_customers_endpoint(
    req: CustomersSyncRequest,
    authorization: str | None = Header(default=None),
    conn: asyncpg.Connection = Depends(get_db),
):
    """
    Added 2026-09-10 for Microsoft Dynamics GP — a connector-push
    twin of ar_agent.py's QBO-only _sync_customers(), feeding the exact
    same `customers` table. Upsert, not full-replace (unlike
    open-invoices above): a customer doesn't stop being a customer just
    because this cycle's payload happens to omit them.
    """
    installation = await _require_installation(conn, authorization)

    for entry in req.customers:
        await conn.execute(
            """
            insert into customers (installation_id, customer_id, display_name, email, phone, updated_at)
            values ($1, $2, $3, $4, $5, $6)
            on conflict (installation_id, customer_id) do update set
                display_name = excluded.display_name, email = excluded.email,
                phone = excluded.phone, updated_at = excluded.updated_at
            """,
            installation["installation_id"], str(entry["customer_id"]), entry.get("display_name"),
            entry.get("email"), entry.get("phone"), datetime.now(timezone.utc),
        )

    return {"status": "recorded", "count": len(req.customers)}


@app.get("/v1/outbox/pending")
async def outbox_pending(
    authorization: str | None = Header(default=None),
    conn: asyncpg.Connection = Depends(get_db),
):
    """
    Polled by the connector, not the browser — this is how a 'send this
    email' decision made server-side (ar_agent.py, run_license_watcher())
    actually leaves the building. The Console never holds SMTP/IMAP
    credentials (ARCHITECTURE.md: "Never request customer passwords"),
    so it can only ask the connector to send on its behalf.
    """
    installation = await _require_installation(conn, authorization)
    rows = await conn.fetch(
        "select email_id, to_address, subject, body from outbound_emails where installation_id = $1 and status = 'pending' order by created_at",
        installation["installation_id"],
    )
    return [dict(r) for r in rows]


@app.post("/v1/outbox/{email_id}/ack")
async def outbox_ack(
    email_id: str,
    req: OutboxAckRequest,
    authorization: str | None = Header(default=None),
    conn: asyncpg.Connection = Depends(get_db),
):
    installation = await _require_installation(conn, authorization)
    await conn.execute(
        """
        update outbound_emails set status = $1, detail = $2, sent_at = $3
        where email_id = $4 and installation_id = $5
        """,
        req.status, req.detail, datetime.now(timezone.utc), email_id, installation["installation_id"],
    )
    return {"status": "acknowledged"}


@app.post("/v1/billing/webhook")
async def billing_webhook(request: Request, conn: asyncpg.Connection = Depends(get_db)):
    """
    Called directly by Stripe's servers, not by the portal or admin
    frontend — no session/API-key auth involved. Authenticity comes from
    the signature header instead, verified against the RAW request body
    (Stripe signs the exact bytes it sent; re-serializing parsed JSON
    would not reproduce the same signature). Same pattern as the
    reference implementation in ocrdocflow's billing.py.

    demo_end is the one field the rest of this codebase already checks
    for entitlement (_refresh_license_status, _entitlements_for,
    run_license_watcher) — a Stripe payment just pushes demo_end another
    RENEWAL_TERM_DAYS out, the same field a trial's demo_end already is,
    rather than inventing a second "is this installation paid up" signal.
    """
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(500, "STRIPE_WEBHOOK_SECRET not configured on the server")

    raw_body = await request.body()
    sig_header = request.headers.get("stripe-signature")
    try:
        stripe.Webhook.construct_event(raw_body, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        raise HTTPException(400, "Invalid Stripe signature")

    # construct_event() above only proves the payload is genuinely from
    # Stripe — re-parsed here as a plain dict for reading, same reason
    # ocrdocflow's webhook does this instead of using the returned
    # StripeObject.
    event = json.loads(raw_body)
    event_type = event["type"]
    data = event["data"]["object"]
    now = datetime.now(timezone.utc)

    if event_type == "checkout.session.completed":
        installation_id = data.get("client_reference_id") or (data.get("metadata") or {}).get("installation_id")
        if installation_id:
            await conn.execute(
                """
                update installations
                set stripe_subscription_id = $1, plan = 'paid', payment_date = $2,
                    license_status = 'active', demo_end = $2 + $3::interval,
                    renewal_reminders_sent = 0
                where installation_id = $4
                """,
                data.get("subscription"), now, f"{RENEWAL_TERM_DAYS} days", installation_id,
            )

    elif event_type == "invoice.payment_succeeded":
        # The first invoice on a brand-new subscription (billing_reason
        # "subscription_create") is already handled above by
        # checkout.session.completed — only real renewals extend demo_end
        # here, so a new subscription doesn't get double-extended.
        if data.get("billing_reason") == "subscription_cycle":
            await conn.execute(
                """
                update installations
                set payment_date = $1, license_status = 'active',
                    demo_end = $1 + $2::interval, renewal_reminders_sent = 0
                where stripe_subscription_id = $3
                """,
                now, f"{RENEWAL_TERM_DAYS} days", data.get("subscription"),
            )

    elif event_type == "customer.subscription.deleted":
        # Stripe's own retry/dunning cycle for a failed card has already
        # played out by the time this fires — this is the final "this
        # tenant is no longer paying" signal. Reuses the existing
        # 'expired' status rather than a new one, so _entitlements_for's
        # connector_should_run check (which only special-cases 'expired')
        # doesn't need to change to recognize it.
        await conn.execute(
            "update installations set license_status = 'expired' where stripe_subscription_id = $1",
            data.get("id"),
        )

    return {"status": "received"}


# ---------------------------------------------------------------------------
# Admin read-only endpoints — operator dashboard at /admin. Every one of
# these is read-only by construction (no INSERT/UPDATE/DELETE, ever) and
# gated by _require_admin(). See skills.md: this is the first and only
# way anyone can see the audit trail, approval queue, or AR reports
# without direct psql access on the droplet.
# ---------------------------------------------------------------------------
@app.get("/v1/admin/tenants")
async def admin_list_tenants(conn: asyncpg.Connection = Depends(get_db), _: None = Depends(_require_admin)):
    rows = await conn.fetch(
        """
        select t.tenant_id, t.company_name, t.signup_email, t.lead_source, t.created_at,
               i.installation_id, i.erp_type, i.erp_version, i.license_status, i.plan,
               i.install_date, i.last_heartbeat_at, i.demo_end, i.connector_version,
               qc.connected_at as qbo_connected_at,
               -- Still present = the installer hasn't registered yet
               -- (/v1/registrations deletes it on use).
               (select sc.code from signup_codes sc where sc.tenant_id = t.tenant_id
                order by sc.created_at desc limit 1) as signup_code
        from tenants t
        left join installations i on i.tenant_id = t.tenant_id
        left join qbo_connections qc on qc.installation_id = i.installation_id
        order by t.created_at desc
        """
    )
    return [dict(r) for r in rows]


@app.post("/v1/admin/tenants")
async def admin_create_tenant(
    req: AdminTenantCreateRequest,
    conn: asyncpg.Connection = Depends(get_db),
    _: None = Depends(_require_admin),
):
    """
    The operator-side twin of /v1/leads: registers a customer company by
    hand and returns its signup code for the operator to relay (nothing
    is emailed — same reason as /v1/leads and the portal invite link).
    The customer's installer then fills in everything else at
    /v1/registrations.
    """
    tenant_id, signup_code = await _create_tenant_with_signup_code(
        conn, req.company_name.strip(), req.email.strip(), "admin_console"
    )
    return {"tenant_id": str(tenant_id), "signup_code": signup_code}


@app.get("/v1/admin/audit-log")
async def admin_audit_log(
    installation_id: str | None = None,
    limit: int = 200,
    conn: asyncpg.Connection = Depends(get_db),
    _: None = Depends(_require_admin),
):
    if installation_id:
        rows = await conn.fetch(
            "select * from audit_log where installation_id = $1 order by occurred_at desc limit $2",
            installation_id, limit,
        )
    else:
        rows = await conn.fetch("select * from audit_log order by occurred_at desc limit $1", limit)
    return [dict(r) for r in rows]


@app.get("/v1/admin/approval-queue")
async def admin_approval_queue(
    status: str | None = None,
    conn: asyncpg.Connection = Depends(get_db),
    _: None = Depends(_require_admin),
):
    if status:
        rows = await conn.fetch("select * from approval_queue where status = $1 order by created_at desc", status)
    else:
        rows = await conn.fetch("select * from approval_queue order by created_at desc")
    return [dict(r) for r in rows]


@app.get("/v1/admin/tasks")
async def admin_tasks(
    installation_id: str | None = None,
    status: str | None = None,
    conn: asyncpg.Connection = Depends(get_db),
    _: None = Depends(_require_admin),
):
    query = "select * from ar_tasks where true"
    params: list[Any] = []
    if installation_id:
        params.append(installation_id)
        query += f" and installation_id = ${len(params)}"
    if status:
        params.append(status)
        query += f" and status = ${len(params)}"
    query += " order by created_at desc limit 200"
    rows = await conn.fetch(query, *params)
    return [dict(r) for r in rows]


@app.get("/v1/admin/activity-summary")
async def admin_activity_summary(
    installation_id: str | None = None,
    days: int = 30,
    conn: asyncpg.Connection = Depends(get_db),
    _: None = Depends(_require_admin),
):
    """
    Added 2026-09-10 — daily transaction-volume counts per sub-agent
    per tenant, from ar_tasks (one row per unit of work the AR Agent
    ever handed to a sub-agent, already exactly "a transaction" by this
    project's own existing definition — see schema.sql's own comment on
    that table). No new table: this is a pure aggregation over data
    that already exists and is already correct, not a new data-
    collection mechanism. by_status stays a real breakdown
    (completed/needs_approval/exception/pending) rather than collapsing
    to one number — a day where an agent did 50 things but 8 are stuck
    as exceptions is a different, more actionable story than "58 done."
    Cross-tenant when installation_id is omitted, same optional-filter
    convention as admin_tasks() above — every row still carries its own
    installation_id, never blindly summed across tenants.
    """
    query = """
        with counts as (
            select installation_id, date_trunc('day', created_at)::date as day, sub_agent, status, count(*) as cnt
            from ar_tasks
            where created_at >= now() - make_interval(days => $1)
    """
    params: list[Any] = [days]
    if installation_id:
        params.append(installation_id)
        query += f" and installation_id = ${len(params)}"
    query += """
            group by installation_id, day, sub_agent, status
        )
        select installation_id, day, sub_agent, sum(cnt)::int as total, jsonb_object_agg(status, cnt) as by_status
        from counts
        group by installation_id, day, sub_agent
        order by day desc, sub_agent
    """
    rows = await conn.fetch(query, *params)
    return [dict(r) for r in rows]


@app.get("/v1/admin/ar-reports")
async def admin_ar_reports(conn: asyncpg.Connection = Depends(get_db), _: None = Depends(_require_admin)):
    rows = await conn.fetch("select * from ar_reports order by generated_at desc")
    return [dict(r) for r in rows]


@app.get("/v1/admin/outbox")
async def admin_outbox(
    installation_id: str | None = None,
    status: str | None = None,
    conn: asyncpg.Connection = Depends(get_db),
    _: None = Depends(_require_admin),
):
    query = "select * from outbound_emails where true"
    params: list[Any] = []
    if installation_id:
        params.append(installation_id)
        query += f" and installation_id = ${len(params)}"
    if status:
        params.append(status)
        query += f" and status = ${len(params)}"
    query += " order by created_at desc limit 200"
    rows = await conn.fetch(query, *params)
    return [dict(r) for r in rows]


@app.get("/v1/admin/inbox/pending")
async def admin_inbox_pending(conn: asyncpg.Connection = Depends(get_db), _: None = Depends(_require_admin)):
    """
    Only 'needs_approval' Customer Inbox tasks — the classified,
    non-escalated ones (see ar_agent.py's _process_customer_inbox_event()).
    Escalated/unclassifiable messages ('exception' status) don't belong
    here: CONTEXT.md's own wording is that a human handles those
    personally (outside this system, e.g. by phone or their own email
    client), not by approving a drafted reply through this UI — they're
    still visible read-only via the existing Tasks tab.
    """
    rows = await conn.fetch(
        """
        select t.task_id, t.installation_id, t.created_at, t.result,
               e.payload as message,
               aq.approval_id, aq.approver, aq.status as approval_status
        from ar_tasks t
        join customer_inbox_events e on e.event_id = t.input_event_id
        left join approval_queue aq on aq.task_id = t.task_id
        where t.sub_agent = 'customer_inbox' and t.status = 'needs_approval'
        order by t.created_at
        """
    )
    return [dict(r) for r in rows]


@app.post("/v1/admin/inbox/{task_id}/resolve")
async def admin_resolve_inbox(
    task_id: str,
    req: InboxResolveRequest,
    conn: asyncpg.Connection = Depends(get_db),
    _: None = Depends(_require_admin),
):
    """
    The one place in this project where a human actually resolves an
    approval_queue entry — everything else in this system still just
    accumulates in 'pending' with no UI to act on it (a real, disclosed
    gap; see skills.md). Scoped to Customer Inbox specifically because
    it's the one action type that involves reviewing message content and
    composing a reply, not just approving/rejecting a financial action —
    the other 8+ action types stay view-only for now rather than getting
    inconsistently shallow resolution logic in the same pass.
    """
    task = await conn.fetchrow(
        "select * from ar_tasks where task_id = $1 and sub_agent = 'customer_inbox' and status = 'needs_approval'",
        task_id,
    )
    if not task:
        raise HTTPException(404, "No pending customer_inbox task with that id")

    event = await conn.fetchrow("select * from customer_inbox_events where event_id = $1", task["input_event_id"])
    message = event["payload"] if event else {}

    async with conn.transaction():
        if req.action == "send":
            if not req.reply_body.strip():
                raise HTTPException(400, "reply_body is required to send")
            original_subject = message.get("subject", "")
            reply_subject = original_subject if original_subject.lower().startswith("re:") else f"Re: {original_subject}"
            # Reuses the exact same outbox the connector already polls for
            # every other AR email — this reply goes out through the
            # customer's own mailbox on the next sync cycle, same as a
            # dunning reminder or invoice delivery.
            await conn.execute(
                "insert into outbound_emails (installation_id, to_address, subject, body) values ($1, $2, $3, $4)",
                task["installation_id"], message.get("sender", "unknown"), reply_subject, req.reply_body,
            )
            result = {**(task["result"] or {}), "resolution": "sent", "reply_body": req.reply_body}
        else:
            result = {**(task["result"] or {}), "resolution": "rejected"}

        await conn.execute(
            "update ar_tasks set status = 'completed', result = $1, resolved_at = $2 where task_id = $3",
            result, datetime.now(timezone.utc), task_id,
        )
        await conn.execute(
            "update approval_queue set status = $1, resolved_at = $2, resolved_by = $3 where task_id = $4",
            "approved" if req.action == "send" else "rejected",
            datetime.now(timezone.utc), req.resolved_by, task_id,
        )
        await conn.execute(
            "insert into audit_log (installation_id, agent, action, detail) values ($1, 'ar_agent', $2, $3)",
            task["installation_id"],
            "inbox_reply_sent" if req.action == "send" else "inbox_reply_rejected",
            {"task_id": str(task_id), "resolved_by": req.resolved_by},
        )

    return {"status": "resolved", "action": req.action}


async def _apply_approval_decision(
    conn: asyncpg.Connection, approval: asyncpg.Record, task: asyncpg.Record | None, approved: bool
) -> None:
    """
    Per-action_type downstream effect of a human's decision — the actual
    reason a generic "flip approval_queue.status" endpoint alone would be
    misleading: without this, clicking Approve on a dunning_activation
    would say "approved" while the sequence stayed stuck in
    pending_activation forever and nothing was ever sent.
    """
    action_type = approval["action_type"]
    installation_id = approval["installation_id"]
    result = (task["result"] if task and task["result"] else {}) or {}

    if action_type in ("dunning_activation", "final_demand_letter"):
        invoice_id = result.get("invoice_id")
        step = result.get("step")
        sequence = await conn.fetchrow(
            "select * from dunning_sequences where installation_id = $1 and invoice_id = $2",
            installation_id, str(invoice_id),
        ) if invoice_id else None
        if sequence:
            if approved:
                await conn.execute(
                    "update dunning_sequences set status = 'active', current_step = $1, last_action_at = $2 where sequence_id = $3",
                    step, datetime.now(timezone.utc), sequence["sequence_id"],
                )
                if sequence["contact"]:
                    await ar_agent._send_dunning_email(conn, installation_id, sequence["contact"], invoice_id, step)
            else:
                # Terminal, not a return to pending — an operator who
                # rejects an activation/final-demand recommendation has
                # decided against it, not merely deferred it.
                await conn.execute(
                    "update dunning_sequences set status = 'rejected', last_action_at = $1 where sequence_id = $2",
                    datetime.now(timezone.utc), sequence["sequence_id"],
                )

    elif action_type in ("credit_memo", "write_off"):
        case_id = result.get("case_id")
        if case_id:
            case = await conn.fetchrow(
                "select invoice_id, amount from dispute_cases where case_id = $1", case_id
            )
            if approved:
                # forgiven_amount may be less than the case's full amount —
                # a partial credit memo/write-off. Missing here only for a
                # task predating this field; treat that the same as the
                # old behavior (fully forgiven), rather than erroring on
                # something already in flight.
                forgiven_amount = result.get("forgiven_amount")
                if forgiven_amount is None:
                    forgiven_amount = float(case["amount"]) if case else 0.0
                await conn.execute(
                    "update dispute_cases set status = 'resolved', resolution_type = $1, "
                    "forgiven_amount = $2, resolved_at = $3 where case_id = $4",
                    action_type, forgiven_amount, datetime.now(timezone.utc), case_id,
                )
                # Interaction decision: a credit memo cancels the forgiven
                # portion by crediting it, a write-off cancels it by
                # abandoning collection — either way that portion is done.
                # remaining_amount is what's left after that (0 for a full
                # resolution, same as before this field existed). Fully
                # settled -> the linked dunning_sequences row (if any) is
                # done for good, same as always. A genuine remainder ->
                # dunning must not stay paused forever just because SOME
                # of the dispute was resolved; if a sequence was paused for
                # this dispute, resume it so the remainder gets dunned like
                # any other overdue balance (_process_dunning_for_invoice's
                # normal step logic picks it up from here next cycle).
                if case:
                    remaining_amount = float(case["amount"]) - float(forgiven_amount)
                    if remaining_amount <= 0:
                        updated = await conn.execute(
                            "update dunning_sequences set status = 'completed', last_action_at = $1 "
                            "where installation_id = $2 and invoice_id = $3 and status != 'completed'",
                            datetime.now(timezone.utc), installation_id, case["invoice_id"],
                        )
                        if updated != "UPDATE 0":
                            await ar_agent._log_audit(
                                conn, installation_id, "ar_agent", "sequence_completed_dispute_resolved",
                                {"invoice_id": case["invoice_id"], "case_id": str(case_id),
                                 "resolution_type": action_type, "forgiven_amount": float(forgiven_amount),
                                 "remaining_amount": remaining_amount},
                            )
                    else:
                        updated = await conn.execute(
                            "update dunning_sequences set status = 'active', last_action_at = $1 "
                            "where installation_id = $2 and invoice_id = $3 and status = 'paused'",
                            datetime.now(timezone.utc), installation_id, case["invoice_id"],
                        )
                        if updated != "UPDATE 0":
                            await ar_agent._log_audit(
                                conn, installation_id, "ar_agent", "sequence_resumed_partial_dispute_resolution",
                                {"invoice_id": case["invoice_id"], "case_id": str(case_id),
                                 "resolution_type": action_type, "forgiven_amount": float(forgiven_amount),
                                 "remaining_amount": remaining_amount},
                            )
            else:
                # Rejecting a proposed resolution doesn't close the
                # dispute — it just means this specific resolution wasn't
                # accepted; back to 'open' for a different one to be
                # proposed. Dunning stays exactly as it is: still paused,
                # because _has_open_dispute() still sees an open case — no
                # change needed here for that to hold.
                await conn.execute(
                    "update dispute_cases set status = 'open' where case_id = $1", case_id,
                )

    elif action_type == "credit_limit_change":
        # Added 2026-09-10, alongside moving credit-limit requests into
        # the tenant portal (ar_agent.recommend_credit_limit_change()
        # creates these). Previously this was a pure recommendation —
        # "a human executes it themselves, outside this system" per
        # CONTEXT.md 2.5 — with nothing here to actually write the new
        # limit; now that a real, approved request exists, approving it
        # writes straight into credit_limits via the exact same upsert
        # admin_set_credit_limit() already uses for direct entry. Not a
        # new "write-back to a real ERP" — credit_limits was always
        # this project's own reference table, never the ERP's.
        if approved:
            customer_id = result.get("customer_id")
            proposed_limit = result.get("proposed_limit")
            if customer_id is not None and proposed_limit is not None:
                await conn.execute(
                    """
                    insert into credit_limits (installation_id, customer_id, credit_limit, updated_at)
                    values ($1, $2, $3, $4)
                    on conflict (installation_id, customer_id) do update set
                        credit_limit = excluded.credit_limit, updated_at = excluded.updated_at
                    """,
                    installation_id, customer_id, proposed_limit, datetime.now(timezone.utc),
                )

    # credit_hold / credit_release: CONTEXT.md 2.5 — "recommendations
    # only... a human executes or approves them." The human executes it
    # themselves, outside this system (no ERP write-back exists to do
    # it automatically either way) — there's nothing further for this
    # system to do beyond recording the decision below.
    #
    # cash_application_match / unidentified_cash: no further table to
    # update — the ar_tasks status change below is the whole effect.

    if task:
        # Rejecting cash_application_match/unidentified_cash has nowhere
        # else to route to (unlike the two branches above, which have
        # their own tables to reflect the decision in) — 'exception'
        # flags it for a human to handle directly, same meaning as
        # everywhere else in this project. Every other rejection still
        # completes its task: the decision itself (dunning declined,
        # dispute reopened, recommendation declined) IS the resolution.
        if not approved and action_type in ("cash_application_match", "unidentified_cash"):
            new_status = "exception"
        else:
            new_status = "completed"
        await conn.execute(
            "update ar_tasks set status = $1, resolved_at = $2 where task_id = $3",
            new_status, datetime.now(timezone.utc), task["task_id"],
        )


@app.post("/v1/admin/approval-queue/{approval_id}/resolve")
async def admin_resolve_approval(
    approval_id: str,
    req: ApprovalResolveRequest,
    conn: asyncpg.Connection = Depends(get_db),
    _: None = Depends(_require_admin),
):
    """
    Covers every action_type except customer_inbox_reply, which has its
    own dedicated flow (composing a reply is a different shape of
    decision than approving a financial action) — see
    admin_resolve_inbox() above.
    """
    approval = await conn.fetchrow(
        "select * from approval_queue where approval_id = $1 and status = 'pending'", approval_id
    )
    if not approval:
        raise HTTPException(404, "No pending approval with that id")
    if approval["action_type"] == "customer_inbox_reply":
        raise HTTPException(400, "Use /v1/admin/inbox/{task_id}/resolve for customer_inbox_reply")

    task = await conn.fetchrow("select * from ar_tasks where task_id = $1", approval["task_id"]) if approval["task_id"] else None
    approved = req.action == "approve"

    async with conn.transaction():
        await _apply_approval_decision(conn, approval, task, approved)
        await conn.execute(
            "update approval_queue set status = $1, resolved_at = $2, resolved_by = $3 where approval_id = $4",
            "approved" if approved else "rejected", datetime.now(timezone.utc), req.resolved_by, approval_id,
        )
        await conn.execute(
            "insert into audit_log (installation_id, agent, action, detail) values ($1, 'ar_agent', $2, $3)",
            approval["installation_id"],
            f"{approval['action_type']}_{'approved' if approved else 'rejected'}",
            {"approval_id": str(approval_id), "resolved_by": req.resolved_by},
        )

    return {"status": "resolved", "action": req.action}


# Every table with an installation_id column, in FK-safe order — children
# before parents. approval_queue references ar_tasks, so it goes first;
# everything else here only references installations, so order among the
# rest doesn't matter. Not schema-level ON DELETE CASCADE, deliberately —
# an explicit, ordered, application-level delete is auditable and can't
# accidentally cascade from an unrelated delete elsewhere in the codebase.
DELETION_TABLES_BY_INSTALLATION = [
    "approval_queue", "ar_tasks", "audit_log",
    "cash_application_events", "invoice_delivery_events", "customer_inbox_events",
    "dunning_sequences", "dispute_cases", "credit_risk_assessments", "credit_limits",
    "ar_reports", "outbound_emails", "error_events", "credentials",
]


async def _delete_tenant_data(conn: asyncpg.Connection, tenant: asyncpg.Record) -> dict[str, int]:
    """
    Hard-deletes every row this tenant's data ever touched — not a
    soft-delete flag. Closes Section 7 of DATA_PROCESSING_DISCLOSURE.md:
    this is what an erasure request actually does, not a documented
    promise with nothing behind it. Returns per-table row counts, the
    evidentiary record for data_deletion_log — proof something was
    actually erased, since the tenant row itself won't exist afterward
    to check against.

    No backup system exists anywhere in this project to reconcile with
    either — there's nothing to purge there because nothing is backed up.
    """
    tenant_id = tenant["tenant_id"]
    row_counts: dict[str, int] = {}

    installation_ids = [r["installation_id"] for r in await conn.fetch(
        "select installation_id from installations where tenant_id = $1", tenant_id
    )]

    if installation_ids:
        for table in DELETION_TABLES_BY_INSTALLATION:
            status = await conn.execute(
                f"delete from {table} where installation_id = any($1::uuid[])", installation_ids
            )
            row_counts[table] = int(status.split()[-1])

    for table in ("installations", "policy_configs", "signup_codes"):
        status = await conn.execute(f"delete from {table} where tenant_id = $1", tenant_id)
        row_counts[table] = int(status.split()[-1])

    status = await conn.execute("delete from tenants where tenant_id = $1", tenant_id)
    row_counts["tenants"] = int(status.split()[-1])

    return row_counts


@app.post("/v1/admin/tenants/{tenant_id}/delete")
async def admin_delete_tenant(
    tenant_id: str,
    req: TenantDeletionRequest,
    conn: asyncpg.Connection = Depends(get_db),
    _: None = Depends(_require_admin),
):
    """
    Irreversible. confirm_company_name must exactly match the tenant's
    company_name — the same "type the resource's name to confirm"
    pattern real admin UIs use for destructive actions, enforced
    server-side so a scripted call can't skip the dashboard's own prompt.
    """
    tenant = await conn.fetchrow("select * from tenants where tenant_id = $1", tenant_id)
    if not tenant:
        raise HTTPException(404, "No tenant with that id")
    if req.confirm_company_name != tenant["company_name"]:
        raise HTTPException(400, "confirm_company_name does not match this tenant's company name — refusing to delete")

    async with conn.transaction():
        row_counts = await _delete_tenant_data(conn, tenant)
        await conn.execute(
            """
            insert into data_deletion_log (tenant_id, company_name, signup_email, requested_by, row_counts)
            values ($1, $2, $3, $4, $5)
            """,
            tenant_id, tenant["company_name"], tenant["signup_email"], req.requested_by, row_counts,
        )

    return {"status": "deleted", "tenant_id": tenant_id, "row_counts": row_counts}


@app.get("/v1/admin/deletion-log")
async def admin_deletion_log(conn: asyncpg.Connection = Depends(get_db), _: None = Depends(_require_admin)):
    rows = await conn.fetch("select * from data_deletion_log order by deleted_at desc")
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# QuickBooks Online — OAuth2 connect flow (added 2026-09-09). The first
# real ERP adapter in this project; see qbo_client.py's module docstring
# for why this is Console-to-cloud rather than routed through the local
# connector like every other ERP. Available both admin-initiated
# (vendor support) and tenant-initiated (the portal, added 2026-09-10) —
# either way, the caller opens the returned authorization_url for the
# tenant's own QuickBooks admin to complete the actual consent.
# ---------------------------------------------------------------------------
def _require_qbo_erp_type(erp_type: str | None) -> None:
    """
    Added 2026-09-10 after live testing found neither connect endpoint
    checked this at all — a Dynamics GP (or any non-QBO) installation
    could get a real Intuit authorization_url with nothing to actually
    do once connected (ar_agent.py's _fetch_open_invoices() gates on
    erp_type before ever using a qbo_connections row, so this couldn't
    have caused a real data leak, but a tenant could easily click
    "Connect" with no real effect and no reason it should be offered at
    all). Same alias check ar_agent.py's own QBO dispatch already uses,
    reused here rather than duplicated with its own list to drift from.
    """
    if (erp_type or "").strip().lower() not in ar_agent.QBO_ERP_TYPE_ALIASES:
        raise HTTPException(400, f"This installation is tagged '{erp_type}', not QuickBooks Online — nothing to connect")


@app.post("/v1/admin/installations/{installation_id}/qbo/connect")
async def admin_qbo_connect(
    installation_id: str,
    conn: asyncpg.Connection = Depends(get_db),
    _: None = Depends(_require_admin),
):
    if not qbo_configured():
        raise HTTPException(500, "QBO_CLIENT_ID/QBO_CLIENT_SECRET not configured on the server")

    installation = await conn.fetchrow(
        "select installation_id, erp_type from installations where installation_id = $1", installation_id
    )
    if not installation:
        raise HTTPException(404, "No installation with that id")
    _require_qbo_erp_type(installation["erp_type"])

    state = secrets.token_urlsafe(32)
    await conn.execute(
        "insert into qbo_oauth_states (state, installation_id, expires_at) values ($1, $2, $3)",
        state, installation_id, datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    return {"authorization_url": qbo_client.build_authorization_url(state)}


@app.get("/v1/qbo/oauth/callback", response_class=HTMLResponse)
async def qbo_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    realmId: str | None = None,
    error: str | None = None,
    conn: asyncpg.Connection = Depends(get_db),
):
    """
    Public by necessity — Intuit redirects the tenant's own browser here
    after they approve (or deny) access on Intuit's consent screen, and
    Intuit sends no admin credential of ours along with it. `state` is
    what stands in for auth here: it only exists in qbo_oauth_states if
    an admin genuinely initiated a connect attempt for a specific
    installation, and it's single-use (deleted once consumed).
    """
    if error:
        return HTMLResponse(f"<p>QuickBooks did not authorize the connection: {error}</p>", status_code=400)
    if not code or not state or not realmId:
        return HTMLResponse("<p>Missing code/state/realmId from QuickBooks' redirect.</p>", status_code=400)

    pending = await conn.fetchrow(
        "select installation_id from qbo_oauth_states where state = $1 and expires_at > $2",
        state, datetime.now(timezone.utc),
    )
    if not pending:
        return HTMLResponse("<p>This connection attempt has expired or is invalid. Ask your admin to try again.</p>", status_code=400)

    try:
        token_response = await qbo_client.exchange_code_for_tokens(code)
    except Exception as e:
        await ar_agent._log_audit(conn, pending["installation_id"], "qbo_integration", "oauth_exchange_failed",
                                   {"error": str(e)})
        return HTMLResponse("<p>Connecting to QuickBooks failed. Ask your admin to try again.</p>", status_code=502)

    access_expires, refresh_expires = qbo_client.token_expiry_from_response(token_response)
    async with conn.transaction():
        await conn.execute(
            """
            insert into qbo_connections (installation_id, realm_id, access_token, refresh_token,
                access_token_expires_at, refresh_token_expires_at, updated_at)
            values ($1, $2, $3, $4, $5, $6, $7)
            on conflict (installation_id) do update set
                realm_id = excluded.realm_id, access_token = excluded.access_token,
                refresh_token = excluded.refresh_token,
                access_token_expires_at = excluded.access_token_expires_at,
                refresh_token_expires_at = excluded.refresh_token_expires_at,
                updated_at = excluded.updated_at
            """,
            pending["installation_id"], realmId, token_response["access_token"], token_response["refresh_token"],
            access_expires, refresh_expires, datetime.now(timezone.utc),
        )
        await conn.execute("delete from qbo_oauth_states where state = $1", state)
        await ar_agent._log_audit(conn, pending["installation_id"], "qbo_integration", "connected",
                                   {"realm_id": realmId})

    return HTMLResponse("<p>QuickBooks connected. You can close this window.</p>")


@app.get("/v1/admin/installations/{installation_id}/qbo/status")
async def admin_qbo_status(
    installation_id: str,
    conn: asyncpg.Connection = Depends(get_db),
    _: None = Depends(_require_admin),
):
    row = await conn.fetchrow(
        "select realm_id, connected_at, updated_at from qbo_connections where installation_id = $1",
        installation_id,
    )
    if not row:
        return {"connected": False}
    return {"connected": True, **dict(row)}


# ---------------------------------------------------------------------------
# Customers & credit limits (added 2026-09-09). Not QBO-specific — the
# `customers` table happens to be QBO-populated today (via
# ar_agent.py's _sync_customers()), but nothing here assumes that, and
# an installation with no `customers` rows at all (any non-QBO ERP)
# can still have a credit limit set manually against a customer_id it
# already knows about (e.g. from an invoice/dispute/credit-risk row).
# credit_limits itself is ERP-agnostic and already existed — this is
# the missing admin-facing way to WRITE it. Real automatic credit-limit
# sourcing from QBO isn't possible: unlike QuickBooks Desktop, QBO's
# Customer entity has no native credit-limit field at all — manual
# entry is the honest answer here, not a stand-in for a real adapter
# that could exist later.
# ---------------------------------------------------------------------------
@app.get("/v1/admin/installations/{installation_id}/customers")
async def admin_list_customers(
    installation_id: str,
    conn: asyncpg.Connection = Depends(get_db),
    _: None = Depends(_require_admin),
):
    rows = await conn.fetch(
        """
        select c.customer_id, c.display_name, c.email, c.phone,
               cl.credit_limit, cl.updated_at as credit_limit_updated_at
        from customers c
        left join credit_limits cl on cl.installation_id = c.installation_id and cl.customer_id = c.customer_id
        where c.installation_id = $1
        order by c.display_name
        """,
        installation_id,
    )
    return [dict(r) for r in rows]


@app.post("/v1/admin/installations/{installation_id}/customers/{customer_id}/credit-limit")
async def admin_set_credit_limit(
    installation_id: str,
    customer_id: str,
    req: CreditLimitSetRequest,
    conn: asyncpg.Connection = Depends(get_db),
    _: None = Depends(_require_admin),
):
    installation = await conn.fetchrow(
        "select installation_id from installations where installation_id = $1", installation_id
    )
    if not installation:
        raise HTTPException(404, "No installation with that id")

    await conn.execute(
        """
        insert into credit_limits (installation_id, customer_id, credit_limit, updated_at)
        values ($1, $2, $3, $4)
        on conflict (installation_id, customer_id) do update set
            credit_limit = excluded.credit_limit, updated_at = excluded.updated_at
        """,
        installation_id, customer_id, req.credit_limit, datetime.now(timezone.utc),
    )
    return {"status": "set", "installation_id": installation_id, "customer_id": customer_id, "credit_limit": req.credit_limit}


# ---------------------------------------------------------------------------
# Tenant self-service portal (added 2026-09-10). Real approval authority
# for the tenant's own staff — see skills.md and CONTEXT.md's own
# "human approval queue" / amount-tiered analyst-controller-CFO
# language, which never meant the vendor's own operator. Admin-gated
# endpoints below only bootstrap a tenant's first user (invite); every
# actual data/approval endpoint a tenant uses lives under /v1/portal/*,
# gated by _require_tenant_user() instead of _require_admin(), and
# scoped to the caller's own tenant_id via _tenant_installation_ids() —
# never a client-supplied id.
# ---------------------------------------------------------------------------
@app.post("/v1/admin/tenants/{tenant_id}/users")
async def admin_invite_tenant_user(
    tenant_id: str,
    req: TenantUserInviteRequest,
    conn: asyncpg.Connection = Depends(get_db),
    _: None = Depends(_require_admin),
):
    """
    Bootstraps a tenant's own portal access. Returns a real, single-use
    invite URL for the operator to relay to the tenant contact manually
    (email, phone, whatever) — no automated sending exists, same reason
    /v1/leads only ever logs its signup code instead of emailing it: no
    real qumaxa-side outbound mail capability exists yet.
    """
    tenant = await conn.fetchrow("select tenant_id from tenants where tenant_id = $1", tenant_id)
    if not tenant:
        raise HTTPException(404, "No tenant with that id")

    invite_token = secrets.token_urlsafe(32)
    await conn.execute(
        """
        insert into tenant_users (tenant_id, email, role, invite_token_hash, invite_expires_at)
        values ($1, $2, $3, $4, $5)
        on conflict (tenant_id, email) do update set
            role = excluded.role, invite_token_hash = excluded.invite_token_hash,
            invite_expires_at = excluded.invite_expires_at
        """,
        tenant_id, req.email, req.role, _hash_token(invite_token),
        datetime.now(timezone.utc) + PORTAL_INVITE_TTL,
    )
    return {"invite_url": f"{CONSOLE_BASE_URL}/portal/?invite={invite_token}"}


@app.get("/v1/admin/tenants/{tenant_id}/users")
async def admin_list_tenant_users(
    tenant_id: str,
    conn: asyncpg.Connection = Depends(get_db),
    _: None = Depends(_require_admin),
):
    rows = await conn.fetch(
        """
        select user_id, email, role, (password_hash is not null) as activated, created_at, last_login_at
        from tenant_users where tenant_id = $1 order by created_at
        """,
        tenant_id,
    )
    return [dict(r) for r in rows]


@app.post("/v1/portal/invite/accept")
async def portal_accept_invite(req: InviteAcceptRequest, conn: asyncpg.Connection = Depends(get_db)):
    user = await conn.fetchrow(
        "select * from tenant_users where invite_token_hash = $1 and invite_expires_at > $2",
        _hash_token(req.invite_token), datetime.now(timezone.utc),
    )
    if not user:
        raise HTTPException(400, "Invalid or expired invite link — ask your admin to send a new one")

    password_hash = bcrypt.hashpw(req.password.encode(), bcrypt.gensalt()).decode()
    await conn.execute(
        "update tenant_users set password_hash = $1, invite_token_hash = null, invite_expires_at = null where user_id = $2",
        password_hash, user["user_id"],
    )
    return {"status": "activated", "email": user["email"]}


@app.post("/v1/portal/login")
async def portal_login(req: PortalLoginRequest, response: Response, conn: asyncpg.Connection = Depends(get_db)):
    user = await conn.fetchrow("select * from tenant_users where email = $1", req.email)
    if not user or not user["password_hash"] or not bcrypt.checkpw(req.password.encode(), user["password_hash"].encode()):
        raise HTTPException(401, "Invalid email or password")

    session_token = secrets.token_urlsafe(32)
    await conn.execute(
        "insert into tenant_sessions (session_token_hash, user_id, expires_at) values ($1, $2, $3)",
        _hash_token(session_token), user["user_id"], datetime.now(timezone.utc) + PORTAL_SESSION_TTL,
    )
    await conn.execute(
        "update tenant_users set last_login_at = $1 where user_id = $2", datetime.now(timezone.utc), user["user_id"]
    )

    response.set_cookie(
        key=PORTAL_SESSION_COOKIE, value=session_token, httponly=True, secure=True, samesite="lax",
        max_age=int(PORTAL_SESSION_TTL.total_seconds()), path="/",
    )
    return {"status": "logged_in", "email": user["email"]}


@app.post("/v1/portal/logout")
async def portal_logout(
    response: Response,
    portal_session: str | None = Cookie(default=None),
    conn: asyncpg.Connection = Depends(get_db),
):
    if portal_session:
        await conn.execute("delete from tenant_sessions where session_token_hash = $1", _hash_token(portal_session))
    response.delete_cookie(PORTAL_SESSION_COOKIE, path="/")
    return {"status": "logged_out"}


@app.get("/v1/portal/me")
async def portal_me(user: asyncpg.Record = Depends(_require_tenant_user)):
    return {"email": user["email"], "role": user["role"]}


@app.get("/v1/portal/approval-queue")
async def portal_approval_queue(
    status: str | None = "pending",
    user: asyncpg.Record = Depends(_require_tenant_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    installation_ids = await _tenant_installation_ids(conn, user["tenant_id"])
    if not installation_ids:
        return []
    if status:
        rows = await conn.fetch(
            "select * from approval_queue where installation_id = any($1) and status = $2 order by created_at desc",
            installation_ids, status,
        )
    else:
        rows = await conn.fetch(
            "select * from approval_queue where installation_id = any($1) order by created_at desc",
            installation_ids,
        )
    return [dict(r) for r in rows]


@app.post("/v1/portal/approval-queue/{approval_id}/resolve")
async def portal_resolve_approval(
    approval_id: str,
    req: PortalApprovalResolveRequest,
    user: asyncpg.Record = Depends(_require_tenant_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """
    Tenant-scoped twin of admin_resolve_approval() — same
    _apply_approval_decision() so the downstream effect (dunning
    resumed/completed, credit hold recommended, etc.) is identical
    either way. What's different: the approval row must belong to the
    caller's own tenant, and resolved_by is the real logged-in email,
    not a free-typed string — this is the actual accountability fix.
    """
    installation_ids = await _tenant_installation_ids(conn, user["tenant_id"])
    approval = await conn.fetchrow(
        "select * from approval_queue where approval_id = $1 and status = 'pending' and installation_id = any($2)",
        approval_id, installation_ids,
    )
    if not approval:
        raise HTTPException(404, "No pending approval with that id for your tenant")
    if approval["action_type"] == "customer_inbox_reply":
        raise HTTPException(400, "Use /v1/portal/inbox/{task_id}/resolve for customer_inbox_reply")
    if not await _approval_authorized(conn, approval["approver"], user):
        raise HTTPException(403, f"Only {approval['approver']} can act on this item")

    task = await conn.fetchrow("select * from ar_tasks where task_id = $1", approval["task_id"]) if approval["task_id"] else None
    approved = req.action == "approve"

    async with conn.transaction():
        await _apply_approval_decision(conn, approval, task, approved)
        await conn.execute(
            "update approval_queue set status = $1, resolved_at = $2, resolved_by = $3 where approval_id = $4",
            "approved" if approved else "rejected", datetime.now(timezone.utc), user["email"], approval_id,
        )
        await conn.execute(
            "insert into audit_log (installation_id, agent, action, detail) values ($1, 'tenant_portal', $2, $3)",
            approval["installation_id"],
            f"{approval['action_type']}_{'approved' if approved else 'rejected'}",
            {"approval_id": str(approval_id), "resolved_by": user["email"]},
        )

    return {"status": "resolved", "action": req.action}


@app.get("/v1/portal/inbox/pending")
async def portal_inbox_pending(
    user: asyncpg.Record = Depends(_require_tenant_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    installation_ids = await _tenant_installation_ids(conn, user["tenant_id"])
    if not installation_ids:
        return []
    rows = await conn.fetch(
        """
        select t.task_id, t.installation_id, t.created_at, t.result,
               e.payload as message,
               aq.approval_id, aq.approver, aq.status as approval_status
        from ar_tasks t
        join customer_inbox_events e on e.event_id = t.input_event_id
        left join approval_queue aq on aq.task_id = t.task_id
        where t.sub_agent = 'customer_inbox' and t.status = 'needs_approval'
          and t.installation_id = any($1)
        order by t.created_at
        """,
        installation_ids,
    )
    return [dict(r) for r in rows]


@app.post("/v1/portal/inbox/{task_id}/resolve")
async def portal_resolve_inbox(
    task_id: str,
    req: PortalInboxResolveRequest,
    user: asyncpg.Record = Depends(_require_tenant_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    installation_ids = await _tenant_installation_ids(conn, user["tenant_id"])
    task = await conn.fetchrow(
        """
        select * from ar_tasks where task_id = $1 and sub_agent = 'customer_inbox'
          and status = 'needs_approval' and installation_id = any($2)
        """,
        task_id, installation_ids,
    )
    if not task:
        raise HTTPException(404, "No pending customer_inbox task with that id for your tenant")

    approval = await conn.fetchrow("select approver from approval_queue where task_id = $1", task_id)
    if approval and not await _approval_authorized(conn, approval["approver"], user):
        raise HTTPException(403, f"Only {approval['approver']} can act on this item")

    event = await conn.fetchrow("select * from customer_inbox_events where event_id = $1", task["input_event_id"])
    message = event["payload"] if event else {}

    async with conn.transaction():
        if req.action == "send":
            if not req.reply_body.strip():
                raise HTTPException(400, "reply_body is required to send")
            original_subject = message.get("subject", "")
            reply_subject = original_subject if original_subject.lower().startswith("re:") else f"Re: {original_subject}"
            await conn.execute(
                "insert into outbound_emails (installation_id, to_address, subject, body) values ($1, $2, $3, $4)",
                task["installation_id"], message.get("sender", "unknown"), reply_subject, req.reply_body,
            )
            result = {**(task["result"] or {}), "resolution": "sent", "reply_body": req.reply_body}
        else:
            result = {**(task["result"] or {}), "resolution": "rejected"}

        await conn.execute(
            "update ar_tasks set status = 'completed', result = $1, resolved_at = $2 where task_id = $3",
            result, datetime.now(timezone.utc), task_id,
        )
        await conn.execute(
            "update approval_queue set status = $1, resolved_at = $2, resolved_by = $3 where task_id = $4",
            "approved" if req.action == "send" else "rejected",
            datetime.now(timezone.utc), user["email"], task_id,
        )
        await conn.execute(
            "insert into audit_log (installation_id, agent, action, detail) values ($1, 'tenant_portal', $2, $3)",
            task["installation_id"],
            "inbox_reply_sent" if req.action == "send" else "inbox_reply_rejected",
            {"task_id": str(task_id), "resolved_by": user["email"]},
        )

    return {"status": "resolved", "action": req.action}


@app.get("/v1/portal/tasks")
async def portal_tasks(
    status: str | None = None,
    user: asyncpg.Record = Depends(_require_tenant_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    installation_ids = await _tenant_installation_ids(conn, user["tenant_id"])
    if not installation_ids:
        return []
    query = "select * from ar_tasks where installation_id = any($1)"
    params: list[Any] = [installation_ids]
    if status:
        params.append(status)
        query += f" and status = ${len(params)}"
    query += " order by created_at desc limit 200"
    rows = await conn.fetch(query, *params)
    return [dict(r) for r in rows]


@app.get("/v1/portal/activity-summary")
async def portal_activity_summary(
    days: int = 30,
    user: asyncpg.Record = Depends(_require_tenant_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """
    Tenant-scoped twin of admin_activity_summary() — same source
    (ar_tasks), same by_status breakdown. Pooled across the tenant's
    own installations (there's usually just one) rather than broken
    out per installation_id — the tenant doesn't have a concept of
    "installations" as a vendor-internal detail worth splitting by.
    """
    installation_ids = await _tenant_installation_ids(conn, user["tenant_id"])
    if not installation_ids:
        return []
    rows = await conn.fetch(
        """
        with counts as (
            select date_trunc('day', created_at)::date as day, sub_agent, status, count(*) as cnt
            from ar_tasks
            where installation_id = any($1) and created_at >= now() - make_interval(days => $2)
            group by day, sub_agent, status
        )
        select day, sub_agent, sum(cnt)::int as total, jsonb_object_agg(status, cnt) as by_status
        from counts
        group by day, sub_agent
        order by day desc, sub_agent
        """,
        installation_ids, days,
    )
    return [dict(r) for r in rows]


@app.get("/v1/portal/audit-log")
async def portal_audit_log(
    limit: int = 200,
    user: asyncpg.Record = Depends(_require_tenant_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    installation_ids = await _tenant_installation_ids(conn, user["tenant_id"])
    if not installation_ids:
        return []
    rows = await conn.fetch(
        "select * from audit_log where installation_id = any($1) order by occurred_at desc limit $2",
        installation_ids, limit,
    )
    return [dict(r) for r in rows]


@app.get("/v1/portal/ar-reports")
async def portal_ar_reports(
    user: asyncpg.Record = Depends(_require_tenant_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    installation_ids = await _tenant_installation_ids(conn, user["tenant_id"])
    if not installation_ids:
        return []
    rows = await conn.fetch(
        "select * from ar_reports where installation_id = any($1) order by generated_at desc",
        installation_ids,
    )
    return [dict(r) for r in rows]


@app.get("/v1/portal/customers")
async def portal_customers(
    user: asyncpg.Record = Depends(_require_tenant_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """
    Tenant-scoped twin of admin_list_customers(), added 2026-09-10
    alongside moving credit-limit requests into the portal — a tenant
    needs to see their own real customers and current limits before
    they can propose a new one.
    """
    installation_ids = await _tenant_installation_ids(conn, user["tenant_id"])
    if not installation_ids:
        return []
    rows = await conn.fetch(
        """
        select c.installation_id, c.customer_id, c.display_name, c.email, c.phone,
               cl.credit_limit, cl.updated_at as credit_limit_updated_at
        from customers c
        left join credit_limits cl on cl.installation_id = c.installation_id and cl.customer_id = c.customer_id
        where c.installation_id = any($1)
        order by c.display_name
        """,
        installation_ids,
    )
    return [dict(r) for r in rows]


@app.post("/v1/portal/installations/{installation_id}/customers/{customer_id}/credit-limit-requests")
async def portal_request_credit_limit(
    installation_id: str,
    customer_id: str,
    req: PortalCreditLimitRequest,
    user: asyncpg.Record = Depends(_require_tenant_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """
    Proposes a new credit limit — does NOT set it directly. Reuses
    ar_agent.recommend_credit_limit_change() exactly as-is (CONTEXT.md
    2.5: "recommendations only... a human executes or approves them"),
    which creates a real approval_queue item that goes through the
    same exact-approver enforcement as everything else in this portal
    (via that action_type's own policy_configs rule, same as any other
    action). The limit is only actually written once someone approves
    it — see _apply_approval_decision()'s credit_limit_change branch,
    which previously did nothing at all ("a human executes it
    themselves, outside this system") until this endpoint gave it
    something real to execute.
    """
    installation_ids = await _tenant_installation_ids(conn, user["tenant_id"])
    installation = await conn.fetchrow(
        "select * from installations where installation_id = $1 and installation_id = any($2)",
        installation_id, installation_ids,
    )
    if not installation:
        raise HTTPException(404, "No installation with that id for your tenant")

    task_id = await ar_agent.recommend_credit_limit_change(
        conn, installation, customer_id, req.proposed_limit, req.reason
    )
    return {"status": "requested", "task_id": str(task_id)}


@app.get("/v1/portal/installations")
async def portal_installations(
    user: asyncpg.Record = Depends(_require_tenant_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    rows = await conn.fetch(
        """
        select i.installation_id, i.erp_type, i.erp_version, i.license_status, i.connector_version,
               i.plan, i.demo_end, i.payment_date,
               qc.connected_at as qbo_connected_at
        from installations i
        left join qbo_connections qc on qc.installation_id = i.installation_id
        where i.tenant_id = $1
        order by i.install_date
        """,
        user["tenant_id"],
    )
    # payments_live: only a live Stripe key can take real money. Unset or
    # test keys make the portal show a "payments open soon" note instead of
    # a Subscribe button that would fail or run a test-mode checkout.
    payments_live = stripe_configured() and STRIPE_SECRET_KEY.startswith("sk_live_")
    return [dict(r, payments_live=payments_live) for r in rows]


@app.post("/v1/portal/installations/{installation_id}/billing/checkout-session")
async def portal_billing_checkout(
    installation_id: str,
    user: asyncpg.Record = Depends(_require_tenant_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """
    Tenant-initiated Stripe Checkout — same shape as portal_qbo_connect():
    this endpoint only generates a redirect URL. The actual sensitive
    step (entering a card) happens entirely on Stripe's own hosted page,
    never on this server, and the /v1/billing/webhook above — not this
    endpoint's return value or the browser's success-redirect — is the
    source of truth for whether the tenant actually ended up subscribed.
    """
    if not stripe_configured():
        raise HTTPException(500, "STRIPE_SECRET_KEY/STRIPE_PRICE_ID not configured on the server")

    installation_ids = await _tenant_installation_ids(conn, user["tenant_id"])
    installation = await conn.fetchrow(
        "select installation_id, stripe_customer_id, plan, license_status from installations where installation_id = $1 and installation_id = any($2)",
        installation_id, installation_ids,
    )
    if not installation:
        raise HTTPException(404, "No installation with that id for your tenant")

    # Same rule the portal uses to show "Active subscription" instead of
    # the Subscribe button — enforced here too, so a stale tab or a direct
    # API call can't start a second subscription (and a second monthly
    # charge) for an installation that is already paying.
    if installation["plan"] == "paid" and installation["license_status"] != "expired":
        raise HTTPException(409, "This installation already has an active subscription")

    try:
        # Reuse the same Stripe Customer across repeat/renewed checkouts for
        # this installation, rather than creating a new one every time.
        customer_id = installation["stripe_customer_id"]
        if not customer_id:
            customer = stripe.Customer.create(
                email=user["email"],
                metadata={"installation_id": installation_id, "tenant_id": str(user["tenant_id"])},
            )
            customer_id = customer.id
            await conn.execute(
                "update installations set stripe_customer_id = $1 where installation_id = $2",
                customer_id, installation_id,
            )

        session = stripe.checkout.Session.create(
            customer=customer_id,
            mode="subscription",
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            success_url=f"{CONSOLE_BASE_URL}/portal/?billing=success",
            cancel_url=f"{CONSOLE_BASE_URL}/portal/?billing=cancelled",
            # Lets the webhook identify which installation this checkout
            # belongs to without a DB lookup by customer_id.
            client_reference_id=installation_id,
            metadata={"installation_id": installation_id},
        )
    except stripe.error.StripeError:
        raise HTTPException(502, "Could not start checkout with Stripe. Please try again.")

    return {"checkout_url": session.url}


@app.post("/v1/portal/installations/{installation_id}/qbo/connect")
async def portal_qbo_connect(
    installation_id: str,
    user: asyncpg.Record = Depends(_require_tenant_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """
    Tenant-scoped twin of admin_qbo_connect() (added 2026-09-10, by the
    user's own request after moving credit limits into the portal) —
    identical mechanics, just initiated by the tenant's own user
    instead of the vendor operator on their behalf. Not routed through
    approval_queue/_approval_authorized() like credit-limit requests:
    this isn't a financial decision with a tiered approver, it's a
    one-time OAuth setup step, and the actual consent (the part that
    matters) always required the tenant's own Intuit login anyway —
    the vendor was never the one authorizing anything here, just
    generating the link. "Any of the tenant's own users" is the right
    granularity, same as the portal's other non-approval actions.
    """
    if not qbo_configured():
        raise HTTPException(500, "QBO_CLIENT_ID/QBO_CLIENT_SECRET not configured on the server")

    installation_ids = await _tenant_installation_ids(conn, user["tenant_id"])
    installation = await conn.fetchrow(
        "select installation_id, erp_type from installations where installation_id = $1 and installation_id = any($2)",
        installation_id, installation_ids,
    )
    if not installation:
        raise HTTPException(404, "No installation with that id for your tenant")
    _require_qbo_erp_type(installation["erp_type"])

    state = secrets.token_urlsafe(32)
    await conn.execute(
        "insert into qbo_oauth_states (state, installation_id, expires_at) values ($1, $2, $3)",
        state, installation_id, datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    return {"authorization_url": qbo_client.build_authorization_url(state)}


@app.get("/v1/portal/installations/{installation_id}/qbo/status")
async def portal_qbo_status(
    installation_id: str,
    user: asyncpg.Record = Depends(_require_tenant_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    installation_ids = await _tenant_installation_ids(conn, user["tenant_id"])
    row = await conn.fetchrow(
        """
        select qc.realm_id, qc.connected_at, qc.updated_at
        from qbo_connections qc
        where qc.installation_id = $1 and qc.installation_id = any($2)
        """,
        installation_id, installation_ids,
    )
    if not row:
        return {"connected": False}
    return {"connected": True, **dict(row)}


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------
async def _refresh_license_status(conn: asyncpg.Connection, installation: asyncpg.Record) -> None:
    if installation["license_status"] in ("trial", "active"):
        if datetime.now(timezone.utc) > installation["demo_end"]:
            await conn.execute(
                "update installations set license_status = 'expired' where installation_id = $1",
                installation["installation_id"],
            )
            # Caller re-fetches the row after this, so the in-memory
            # `installation` object itself doesn't need patching here.


def _entitlements_for(installation: asyncpg.Record) -> dict[str, Any]:
    # TODO: replace with real plan -> feature-flag mapping (which sub-agents
    # are unlocked, action volume caps, etc).
    #
    # `connector_should_run` is the explicit signal the connector checks on
    # every heartbeat. Expired means full stop — no polling, no syncing,
    # no sub-agent activity — not a degraded/read-only mode.
    return {
        "license_status": installation["license_status"],
        "plan": installation["plan"],
        "demo_end": installation["demo_end"].isoformat(),
        "connectivity_mode": installation["connectivity_mode"],
        "connector_should_run": installation["license_status"] != "expired",
    }


def send_renewal_email(to: str, cc: str, reminder_number: int, days_remaining: int) -> None:
    # TODO: wire up real mail provider (SES/SendGrid/etc). NOT the same
    # concern as outbound_emails/the connector's local SMTP below — this
    # is qumaxa's OWN transactional email, to the tenant, about their
    # subscription. It never goes out under the tenant's identity, so it
    # never touches the tenant's own mailbox or the outbox table.
    print(
        f"[renewal email] to={to} cc={cc} reminder #{reminder_number} "
        f"— license expires in {days_remaining} day(s)"
    )


async def run_license_watcher(pool: asyncpg.Pool) -> None:
    """
    Scheduled job — run hourly (cron / APScheduler / Celery beat, your
    choice). Sends up to 3 reminder emails before expiry, CCing the owner,
    then flips license_status to 'expired' once demo_end has passed.

    Idempotent: renewal_reminders_sent gates re-sends even if this job
    runs more frequently than the reminder cadence itself.
    """
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        installations = await conn.fetch(
            """
            select i.*, t.signup_email
            from installations i
            join tenants t on t.tenant_id = i.tenant_id
            where i.license_status in ('trial', 'active')
            """
        )

        for installation in installations:
            days_remaining = (installation["demo_end"] - now).days
            sent = installation["renewal_reminders_sent"]

            if sent < len(REMINDER_THRESHOLDS_DAYS) and days_remaining <= REMINDER_THRESHOLDS_DAYS[sent]:
                send_renewal_email(
                    to=installation["signup_email"],
                    cc=OWNER_NOTIFICATION_EMAIL,
                    reminder_number=sent + 1,
                    days_remaining=max(days_remaining, 0),
                )
                await conn.execute(
                    """
                    update installations
                    set renewal_reminders_sent = renewal_reminders_sent + 1,
                        last_reminder_sent_at = $1
                    where installation_id = $2
                    """,
                    now,
                    installation["installation_id"],
                )

            if now > installation["demo_end"]:
                await conn.execute(
                    "update installations set license_status = 'expired' where installation_id = $1",
                    installation["installation_id"],
                )


if __name__ == "__main__":
    # Standalone entry point for the license watcher, meant for cron —
    # see deploy/DEPLOY.md. Not invoked when running under uvicorn.
    import asyncio

    async def _main() -> None:
        pool = await create_pool()
        try:
            await run_license_watcher(pool)
        finally:
            await pool.close()

    asyncio.run(_main())
