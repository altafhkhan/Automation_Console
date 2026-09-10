"""
QuickBooks Online integration — the first real ERP adapter in this
project. Deliberately different shape from every other sub_agents/*.py
module: QBO is a cloud REST API with OAuth2, reachable directly from
the Console over the internet, so there's no on-prem connector step —
unlike Oracle EBS/QuickBooks Desktop (ARCHITECTURE.md's "what runs
locally" split), the Console talks to Intuit's API directly, server-
side. See console_registration_api.py's /v1/qbo/* endpoints for the
OAuth connect/callback flow that populates qbo_connections, and
ar_agent.py's _fetch_open_invoices() for where this gets called.

Real network calls, real error surfaces — nothing here fabricates data
if Intuit's API is unreachable or returns an error; callers get an
exception, not a silently-empty result standing in for a real failure.
"""

import base64
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

from qbo_config import QBO_AUTHORIZATION_URL, QBO_CLIENT_ID, QBO_CLIENT_SECRET, QBO_REDIRECT_URI, QBO_SCOPE, qbo_api_base


def build_authorization_url(state: str) -> str:
    params = {
        "client_id": QBO_CLIENT_ID,
        "redirect_uri": QBO_REDIRECT_URI,
        "response_type": "code",
        "scope": QBO_SCOPE,
        "state": state,
    }
    return f"{QBO_AUTHORIZATION_URL}?{urlencode(params)}"


def _basic_auth_header() -> dict[str, str]:
    token = base64.b64encode(f"{QBO_CLIENT_ID}:{QBO_CLIENT_SECRET}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Accept": "application/json"}


async def exchange_code_for_tokens(code: str) -> dict[str, Any]:
    """
    Authorization-code -> token exchange, the one-time step right after
    the tenant approves access on Intuit's own consent screen. Raises
    httpx.HTTPStatusError on failure (expired/reused code, mismatched
    redirect_uri) — the caller (the /v1/qbo/oauth/callback endpoint)
    surfaces that honestly rather than pretending the connection worked.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
            headers=_basic_auth_header(),
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": QBO_REDIRECT_URI},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()


async def refresh_access_token(refresh_token: str) -> dict[str, Any]:
    """
    Access tokens are short-lived (~1 hour) by Intuit's own design;
    refresh tokens roll forward on each use (~100 days). Raises on
    failure — most commonly a refresh token that's expired from 100
    days of inactivity, which means the tenant has to reconnect via the
    consent screen again; there's no way around that, so this doesn't
    try to hide it.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
            headers=_basic_auth_header(),
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()


def token_expiry_from_response(token_response: dict[str, Any]) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    access_expires = now + timedelta(seconds=token_response["expires_in"])
    refresh_expires = now + timedelta(seconds=token_response["x_refresh_token_expires_in"])
    return access_expires, refresh_expires


async def _query(access_token: str, realm_id: str, query: str) -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{qbo_api_base(realm_id)}/query",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            params={"query": query, "minorversion": "65"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()


async def fetch_open_invoices(access_token: str, realm_id: str) -> list[dict[str, Any]]:
    """
    Real QBO data, normalized into the exact shape ar_agent.py's
    _fetch_open_invoices() has documented since before any real ERP
    adapter existed: {"invoice_id", "reference", "amount_due",
    "due_date", "bill_to_contact", "customer_id"}. Balance (not
    TotalAmt) is deliberate — it's what's actually still owed on a
    partially-paid invoice, not the original billed amount.

    QueryResponse omits the "Invoice" key entirely when there are no
    matches (not an empty list) — handled explicitly rather than
    assuming the key exists.
    """
    payload = await _query(access_token, realm_id, "select * from Invoice where Balance > '0'")
    invoices = payload.get("QueryResponse", {}).get("Invoice", [])
    return [
        {
            "invoice_id": inv["Id"],
            "reference": inv.get("DocNumber", inv["Id"]),
            "amount_due": float(inv.get("Balance", 0)),
            "due_date": inv.get("DueDate"),
            "bill_to_contact": (inv.get("BillEmail") or {}).get("Address"),
            "customer_id": (inv.get("CustomerRef") or {}).get("value"),
        }
        for inv in invoices
    ]


async def fetch_customers(access_token: str, realm_id: str) -> list[dict[str, Any]]:
    """
    Real QBO Customer records — the first source of actual customer
    contact data anywhere in this project (everywhere else, customer_id
    has only ever been a bare string). PrimaryEmailAddr and
    PrimaryPhone are both optional on a QBO Customer, so email/phone
    may come back None — not every sandbox/real customer has them set.
    Active = true excludes customers QBO itself has archived.
    """
    payload = await _query(access_token, realm_id, "select * from Customer where Active = true")
    customers = payload.get("QueryResponse", {}).get("Customer", [])
    return [
        {
            "customer_id": c["Id"],
            "display_name": c.get("DisplayName"),
            "email": (c.get("PrimaryEmailAddr") or {}).get("Address"),
            "phone": (c.get("PrimaryPhone") or {}).get("FreeFormNumber"),
        }
        for c in customers
    ]


async def fetch_payments(access_token: str, realm_id: str) -> list[dict[str, Any]]:
    """
    Real QBO Payment records for Cash Application, decomposed to one
    entry per invoice a payment actually applies to. QBO records this
    via each Line's LinkedTxn (a structured, authoritative link to a
    specific invoice's internal Id) rather than a guessed text
    reference — more reliable than what cash_application.match()'s
    reference-matching was originally designed for (a noisy bank-feed
    memo string), so ar_agent.py's _sync_qbo_payments() resolves each
    linked invoice's real DocNumber and feeds that in as "reference",
    letting match()'s existing logic run unchanged.

    One QBO payment can legitimately split across several invoices at
    once — match() has no concept of one event resolving to more than
    one invoice, so each line becomes its own entry here rather than
    trying to redesign match() for N-way splits. `qbo_line_key` (payment
    id + line index) is what ar_agent.py dedups on. A payment with no
    LinkedTxn at all (received but not yet applied to anything in QBO)
    comes through once with `qbo_invoice_id: None`, which
    cash_application.match() already handles honestly as unmatched or
    amount_only.
    """
    payload = await _query(access_token, realm_id, "select * from Payment")
    payments = payload.get("QueryResponse", {}).get("Payment", [])

    entries: list[dict[str, Any]] = []
    for p in payments:
        payment_id = p["Id"]
        received_date = p.get("TxnDate")
        lines = p.get("Line") or []

        applied_lines = [
            (i, line) for i, line in enumerate(lines)
            if any(t.get("TxnType") == "Invoice" for t in (line.get("LinkedTxn") or []))
        ]
        if not applied_lines:
            entries.append({
                "qbo_payment_id": payment_id,
                "qbo_line_key": f"{payment_id}:unapplied",
                "amount": float(p.get("TotalAmt", 0)),
                "qbo_invoice_id": None,
                "received_date": received_date,
            })
            continue

        for i, line in applied_lines:
            invoice_id = next(
                t["TxnId"] for t in line["LinkedTxn"] if t.get("TxnType") == "Invoice"
            )
            entries.append({
                "qbo_payment_id": payment_id,
                "qbo_line_key": f"{payment_id}:{i}",
                "amount": float(line.get("Amount", 0)),
                "qbo_invoice_id": invoice_id,
                "received_date": received_date,
            })
    return entries
