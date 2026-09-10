"""
AR Agent — module-level orchestrator (CONTEXT.md Section 1, ARCHITECTURE.md).

Does not perform AR work itself. Per tenant installation, it:
  - pulls unprocessed work (cash_application_events, invoice_delivery_events,
    customer_inbox_events) and, for Collections & Dunning/Credit Risk,
    re-evaluates open invoices every cycle rather than reacting to a
    connector-pushed event
  - breaks it into tasks and routes each to the right specialized sub-agent
  - checks every action against this tenant's policy before treating it
    as resolved
  - parks anything gated by policy in the approval queue for a human, or
    flags it as an exception when a human has to fix something upstream
    instead (e.g. in the ERP) — there's no approve/reject decision to make
  - records every decision for Monitoring & Audit

All seven sub-agents from CONTEXT.md Section 2 now exist (all in
sub_agents/): Cash Application, Invoice Delivery & Validation,
Collections & Dunning, Dispute & Deduction, Credit Risk, AR Reporting &
Forecast, and Customer Inbox.

One sub-agent — AR Reporting & Forecast — is different from the rest:
CONTEXT.md is explicit it has no approval gate at all ("read-only"), so
it never touches ar_tasks/approval_queue; it only computes metrics and
refreshes a snapshot in ar_reports.

Not an HTTP endpoint — invoked by master_agent.py on a schedule.
"""

from datetime import date, datetime, timezone
from typing import Any

import asyncpg

import qbo_client

from sub_agents import (
    ar_reporting,
    cash_application,
    collections_dunning,
    credit_risk,
    customer_inbox,
    dispute_deduction,
    invoice_delivery,
)


async def _log_audit(
    conn: asyncpg.Connection,
    installation_id: Any,
    agent: str,
    action: str,
    detail: dict[str, Any],
) -> None:
    await conn.execute(
        "insert into audit_log (installation_id, agent, action, detail) values ($1, $2, $3, $4)",
        installation_id,
        agent,
        action,
        detail,
    )


QBO_ERP_TYPE_ALIASES = {"quickbooks online", "qbo", "quickbooks_online"}


async def _get_qbo_credentials(conn: asyncpg.Connection, installation_id: Any) -> tuple[str, str] | None:
    """
    Returns (access_token, realm_id) for this installation's QuickBooks
    Online connection, refreshing the access token first if it's
    expired (QBO access tokens last ~1 hour by Intuit's own design).
    None if there's no connection at all (never connected via
    /v1/qbo/oauth/connect), or if the refresh itself failed — most
    commonly because the refresh token expired from ~100 days of
    inactivity, which means the tenant has to reconnect through the
    consent screen again. That failure is logged, not silently retried
    every cycle forever.
    """
    row = await conn.fetchrow(
        "select * from qbo_connections where installation_id = $1", installation_id
    )
    if not row:
        return None

    if datetime.now(timezone.utc) < row["access_token_expires_at"]:
        return row["access_token"], row["realm_id"]

    try:
        token_response = await qbo_client.refresh_access_token(row["refresh_token"])
    except Exception as e:
        await _log_audit(conn, installation_id, "qbo_integration", "token_refresh_failed", {"error": str(e)})
        return None

    access_expires, refresh_expires = qbo_client.token_expiry_from_response(token_response)
    await conn.execute(
        """
        update qbo_connections set access_token = $1, refresh_token = $2,
            access_token_expires_at = $3, refresh_token_expires_at = $4, updated_at = $5
        where installation_id = $6
        """,
        token_response["access_token"], token_response["refresh_token"],
        access_expires, refresh_expires, datetime.now(timezone.utc), installation_id,
    )
    return token_response["access_token"], row["realm_id"]


async def _fetch_open_invoices(conn: asyncpg.Connection, installation: asyncpg.Record) -> list[dict[str, Any]]:
    """
    Real data source for QuickBooks Online (added 2026-09-09,
    qbo_client.py) — the first ERP this project actually reads from
    instead of stubbing, direct-fetched Console-to-cloud since QBO's
    REST API is reachable from here directly. erp_type matching is a
    simple case-insensitive string check, not an enum — this project
    has never normalized erp_type anywhere else either
    (installer_skeleton.py's ask_manually() is free text).

    Every other erp_type (added 2026-09-10, for Microsoft Dynamics GP —
    see skills.md) reads from `open_invoices` instead: GP has no public
    API a Console can reach directly, so a connector-push path is the
    only option — see console_registration_api.py's
    /v1/sync/open-invoices and connector_daemon.py's sync_open_invoices().
    Honestly empty ([]) until a real connector actually pushes
    something, same meaning as before this existed. This generalizes
    past GP specifically: Oracle EBS/QuickBooks Desktop get the same
    real path the moment either gets a connector-side adapter, with no
    further change here.

    Expected shape (unchanged since before any real adapter existed):
    {"invoice_id": str, "reference": str, "amount_due": float,
    "due_date": "YYYY-MM-DD", "bill_to_contact": str, "customer_id": str}.
    customer_id is what Credit Risk groups exposure by, and what
    invoice_delivery's contract lookup keys on.
    """
    erp_type = (installation["erp_type"] or "").strip().lower()

    if erp_type not in QBO_ERP_TYPE_ALIASES:
        rows = await conn.fetch(
            "select invoice_id, reference, amount_due, due_date, bill_to_contact, customer_id "
            "from open_invoices where installation_id = $1",
            installation["installation_id"],
        )
        return [
            {
                "invoice_id": r["invoice_id"],
                "reference": r["reference"],
                "amount_due": float(r["amount_due"]),
                "due_date": r["due_date"].isoformat() if r["due_date"] else None,
                "bill_to_contact": r["bill_to_contact"],
                "customer_id": r["customer_id"],
            }
            for r in rows
        ]

    credentials = await _get_qbo_credentials(conn, installation["installation_id"])
    if not credentials:
        return []
    access_token, realm_id = credentials

    try:
        return await qbo_client.fetch_open_invoices(access_token, realm_id)
    except Exception as e:
        await _log_audit(conn, installation["installation_id"], "qbo_integration", "fetch_invoices_failed",
                          {"error": str(e)})
        return []


async def _sync_customers(conn: asyncpg.Connection, installation: asyncpg.Record) -> int:
    """
    Real customer contact data (added 2026-09-09) — QBO-only for now,
    same erp_type gate as _fetch_open_invoices(). Upserts into
    `customers`, the first table in this project holding an actual
    name/email/phone rather than a bare customer_id string. Returns 0
    (not an error) for any non-QBO erp_type, or if there's no
    connection, or if the fetch itself fails — all logged, none of them
    treated as fatal to the rest of the AR Agent cycle.
    """
    erp_type = (installation["erp_type"] or "").strip().lower()
    if erp_type not in QBO_ERP_TYPE_ALIASES:
        return 0

    credentials = await _get_qbo_credentials(conn, installation["installation_id"])
    if not credentials:
        return 0
    access_token, realm_id = credentials

    try:
        customers = await qbo_client.fetch_customers(access_token, realm_id)
    except Exception as e:
        await _log_audit(conn, installation["installation_id"], "qbo_integration", "fetch_customers_failed",
                          {"error": str(e)})
        return 0

    for customer in customers:
        await conn.execute(
            """
            insert into customers (installation_id, customer_id, display_name, email, phone, updated_at)
            values ($1, $2, $3, $4, $5, $6)
            on conflict (installation_id, customer_id) do update set
                display_name = excluded.display_name, email = excluded.email,
                phone = excluded.phone, updated_at = excluded.updated_at
            """,
            installation["installation_id"], customer["customer_id"], customer["display_name"],
            customer["email"], customer["phone"], datetime.now(timezone.utc),
        )
    return len(customers)


async def _sync_qbo_payments(conn: asyncpg.Connection, installation: asyncpg.Record) -> int:
    """
    Real Cash Application input for QuickBooks Online (added
    2026-09-09) — before this, cash_application_events only ever got
    rows from a connector's /v1/sync/cash-application push, and no
    connector exists for a QBO-only tenant. Turns each real, not-yet-
    seen QBO payment/invoice-line into a normal cash_application_events
    row so the existing, unchanged _process_cash_application_event() /
    cash_application.match() pipeline runs on it exactly like any other
    event — no special-cased matching logic for QBO.

    Resolves each payment line's linked QBO invoice Id to that
    invoice's real DocNumber (via this cycle's _fetch_open_invoices())
    before handing it to match() as "reference" — match() only ever
    knew how to compare a text reference, and QBO's own structured
    Line[].LinkedTxn is a strictly more reliable source for that
    reference than a bank-feed memo string ever was. An invoice not in
    the currently-open set (e.g. already fully paid off in an earlier
    cycle) resolves to no reference — an honest, minor limitation, not
    a fabricated match.

    qbo_synced_payments is what stops the same real payment line from
    becoming a brand new event every cycle forever.
    """
    erp_type = (installation["erp_type"] or "").strip().lower()
    if erp_type not in QBO_ERP_TYPE_ALIASES:
        return 0

    credentials = await _get_qbo_credentials(conn, installation["installation_id"])
    if not credentials:
        return 0
    access_token, realm_id = credentials

    try:
        payment_lines = await qbo_client.fetch_payments(access_token, realm_id)
    except Exception as e:
        await _log_audit(conn, installation["installation_id"], "qbo_integration", "fetch_payments_failed",
                          {"error": str(e)})
        return 0

    open_invoices = await _fetch_open_invoices(conn, installation)
    reference_by_qbo_invoice_id = {inv["invoice_id"]: inv["reference"] for inv in open_invoices}

    created = 0
    for line in payment_lines:
        already_synced = await conn.fetchrow(
            "select 1 from qbo_synced_payments where installation_id = $1 and qbo_payment_id = $2 and qbo_line_key = $3",
            installation["installation_id"], line["qbo_payment_id"], line["qbo_line_key"],
        )
        if already_synced:
            continue

        payload = {
            "amount": line["amount"],
            "reference": reference_by_qbo_invoice_id.get(line["qbo_invoice_id"]),
            "received_date": line["received_date"],
            "qbo_payment_id": line["qbo_payment_id"],
            "qbo_invoice_id": line["qbo_invoice_id"],
        }
        async with conn.transaction():
            event_id = await conn.fetchval(
                "insert into cash_application_events (installation_id, payload) values ($1, $2) returning event_id",
                installation["installation_id"], payload,
            )
            await conn.execute(
                "insert into qbo_synced_payments (installation_id, qbo_payment_id, qbo_line_key, event_id) values ($1, $2, $3, $4)",
                installation["installation_id"], line["qbo_payment_id"], line["qbo_line_key"], event_id,
            )
        created += 1
    return created


async def _fetch_customer_contact_email(conn: asyncpg.Connection, installation_id: Any, customer_id: Any) -> str | None:
    row = await conn.fetchrow(
        "select email from customers where installation_id = $1 and customer_id = $2",
        installation_id, customer_id,
    )
    return row["email"] if row else None


async def _fetch_contract_terms(conn: asyncpg.Connection, tenant_id: Any, customer_id: Any) -> dict[str, Any] | None:
    """
    TODO: real implementation reads this customer's contracted terms from
    a contracts data source — none exists yet. None means "can't verify,"
    which sub_agents/invoice_delivery.py's validate() treats as
    unverifiable rather than a failure.
    """
    return None


async def _check_requires_approval(
    conn: asyncpg.Connection, installation: asyncpg.Record, action_type: str, amount: float
) -> tuple[bool, str | None]:
    """
    Looks up this installation's approval_rules (set during the policy
    questionnaire in installer_skeleton.py) for `action_type`. Threshold
    semantics match that questionnaire's own wording ("Dollar threshold
    requiring their approval — blank = always"): approval is required
    once amount >= threshold, or always if threshold is blank.

    No rule configured for `action_type` -> fails closed: requires
    approval with no approver assigned. That's a real policy gap (the
    installer's questionnaire only asks about credit_memo,
    credit_limit_change, and dunning_activation today — not
    cash-application outcomes), surfaced honestly rather than silently
    auto-approved.
    """
    policy = await conn.fetchrow(
        "select approval_rules from policy_configs where policy_config_id = $1",
        installation["policy_config_id"],
    )
    rules = policy["approval_rules"] if policy else []
    for rule in rules:
        if rule.get("action") == action_type:
            threshold = rule.get("threshold")
            requires = True if threshold is None else amount >= threshold
            return requires, rule.get("approver")
    return True, None


async def _create_approval_request(
    conn: asyncpg.Connection,
    installation_id: Any,
    task_id: Any,
    action_type: str,
    amount: float,
    approver: str | None,
) -> None:
    await conn.execute(
        """
        insert into approval_queue (installation_id, task_id, action_type, amount, approver)
        values ($1, $2, $3, $4, $5)
        """,
        installation_id,
        task_id,
        action_type,
        amount,
        approver,
    )


async def _open_dispute_case(
    conn: asyncpg.Connection, installation: asyncpg.Record, invoice_id: Any, amount: float
) -> Any:
    """
    Called from _process_cash_application_event() when a short-pay is
    detected. The partial unique index on dispute_cases (installation_id,
    invoice_id) where status != 'resolved' means a duplicate insert here
    would fail — checked explicitly first so a repeat short-pay signal
    for the same still-open case just returns the existing case_id
    instead of erroring.
    """
    existing = await conn.fetchrow(
        "select case_id from dispute_cases where installation_id = $1 and invoice_id = $2 and status != 'resolved'",
        installation["installation_id"], str(invoice_id),
    )
    if existing:
        return existing["case_id"]

    reason_code = dispute_deduction.classify_reason(None)  # no claim text available yet — see the module docstring
    owner = dispute_deduction.suggest_owner(amount)

    case_id = await conn.fetchval(
        """
        insert into dispute_cases (installation_id, invoice_id, reason_code, amount, suggested_owner)
        values ($1, $2, $3, $4, $5)
        returning case_id
        """,
        installation["installation_id"], str(invoice_id), reason_code, amount, owner,
    )
    await _log_audit(conn, installation["installation_id"], "dispute_deduction_sub_agent", "case_opened",
                      {"case_id": str(case_id), "invoice_id": str(invoice_id), "amount": amount,
                       "suggested_owner": owner, "reason_code": reason_code})
    return case_id


async def _check_dispute_slas(conn: asyncpg.Connection, installation: asyncpg.Record) -> int:
    """
    Run every cycle over open cases — 'open' is excluded from the select
    once flipped to 'sla_breached', so this only logs the transition
    once per case, not every cycle.
    """
    cases = await conn.fetch(
        "select * from dispute_cases where installation_id = $1 and status = 'open'",
        installation["installation_id"],
    )
    today = datetime.now(timezone.utc).date()
    for case in cases:
        if dispute_deduction.is_sla_breached(case["opened_at"].date(), today):
            await conn.execute(
                "update dispute_cases set status = 'sla_breached' where case_id = $1",
                case["case_id"],
            )
            await _log_audit(conn, installation["installation_id"], "dispute_deduction_sub_agent", "sla_breached",
                              {"case_id": str(case["case_id"]), "invoice_id": case["invoice_id"]})
    return len(cases)


async def _lookup_approver(conn: asyncpg.Connection, installation: asyncpg.Record, action_type: str) -> str | None:
    """
    Looks up the configured approver for action_type with NO threshold
    gate — for credit_memo/write_off, where CONTEXT.md 2.4 requires
    approval unconditionally ("no exceptions in V1"). Unlike
    _check_requires_approval(), there's no fail-closed-if-absent branch
    to reason about: approval is always required either way, this only
    decides who's asked.
    """
    policy = await conn.fetchrow(
        "select approval_rules from policy_configs where policy_config_id = $1",
        installation["policy_config_id"],
    )
    rules = policy["approval_rules"] if policy else []
    for rule in rules:
        if rule.get("action") == action_type:
            return rule.get("approver")
    return None


async def request_dispute_resolution(
    conn: asyncpg.Connection, installation: asyncpg.Record, case_id: Any, resolution_type: str, forgiven_amount: float
) -> Any:
    """
    Proposes resolving a dispute case via credit memo or write-off.
    Always requires approval, unconditionally — CONTEXT.md 2.4: "Every
    credit memo and write-off requires human approval — no exceptions in
    V1." Nothing in this project calls this automatically yet: deciding
    WHICH resolution to propose needs either a human or the AI-assisted
    drafting CONTEXT.md 2.4 describes, neither of which exists. Built and
    tested directly (see skills.md) so the gate is proven correct ahead
    of whatever eventually calls it — a human via a future Console UI,
    or an AI drafting step later.

    forgiven_amount may be less than the case's full amount — a partial
    credit memo/write-off, leaving a genuine remainder still owed. It's
    what goes to the approver as the approval_queue amount (the actual
    dollar decision being signed off on, not the dispute's total size),
    and what _apply_approval_decision() later uses to decide whether the
    linked dunning_sequences row should stay settled or resume on the
    remainder. Must not exceed the case's amount — the database also
    enforces this via a check constraint, but failing here gives a
    clearer error than a constraint violation at approval time.

    Simplification disclosed: CONTEXT.md describes amount-tiered
    approvers (analyst/controller/CFO). This project's policy model
    (installer_skeleton.py's questionnaire) supports only one configured
    approver per action_type, not a tier list — that approver is used
    regardless of amount. suggest_owner() in sub_agents/dispute_deduction.py
    still computes the tier as an informational routing suggestion,
    stored on the case, not as the actual approver.
    """
    if resolution_type not in ("credit_memo", "write_off"):
        raise ValueError(f"Unknown resolution_type: {resolution_type}")

    case = await conn.fetchrow("select amount from dispute_cases where case_id = $1", case_id)
    if not case:
        raise ValueError(f"No dispute_cases row for case_id={case_id}")
    if forgiven_amount < 0 or forgiven_amount > float(case["amount"]):
        raise ValueError(
            f"forgiven_amount {forgiven_amount} out of range for case amount {case['amount']}"
        )

    approver = await _lookup_approver(conn, installation, resolution_type)
    task_id = await conn.fetchval(
        """
        insert into ar_tasks (installation_id, sub_agent, task_type, status, result)
        values ($1, 'dispute_deduction', $2, 'needs_approval', $3)
        returning task_id
        """,
        installation["installation_id"], resolution_type,
        {"case_id": str(case_id), "amount": float(case["amount"]), "forgiven_amount": forgiven_amount},
    )
    await _create_approval_request(
        conn, installation["installation_id"], task_id, resolution_type, forgiven_amount, approver
    )
    await conn.execute(
        "update dispute_cases set status = 'pending_resolution' where case_id = $1",
        case_id,
    )
    await _log_audit(conn, installation["installation_id"], "ar_agent", f"{resolution_type}_pending_approval",
                      {"task_id": str(task_id), "case_id": str(case_id), "approver": approver,
                       "forgiven_amount": forgiven_amount})
    return task_id


async def _fetch_customer_language(conn: asyncpg.Connection, installation_id: Any, customer_id: Any) -> str | None:
    """
    Durable per-customer language/locale (customer_locales, added
    2026-09-09) — None means "unknown," same honest-default meaning as
    _fetch_credit_limit() below. Used to attach a known language to a
    Customer Inbox message/exception for routing and triage — NOT to
    narrow needs_escalation()'s own keyword check, which always checks
    every language regardless (see that call site's comment for why).
    """
    row = await conn.fetchrow(
        "select language from customer_locales where installation_id = $1 and customer_id = $2",
        installation_id, customer_id,
    )
    return row["language"] if row else None


async def _upsert_customer_language(conn: asyncpg.Connection, installation_id: Any, customer_id: Any, language: str) -> None:
    await conn.execute(
        """
        insert into customer_locales (installation_id, customer_id, language, updated_at)
        values ($1, $2, $3, $4)
        on conflict (installation_id, customer_id) do update set
            language = excluded.language, updated_at = excluded.updated_at
        """,
        installation_id, customer_id, language, datetime.now(timezone.utc),
    )


async def _fetch_credit_limit(conn: asyncpg.Connection, installation_id: Any, customer_id: Any) -> float | None:
    """
    Real data source now: credit_limits, populated by the connector via
    /v1/sync/credit-limits (_pull_credit_limits() in connector_daemon.py
    — a stub returning [] until a real ERP adapter exists, so this still
    returns None in real production today, honestly). None means
    "unknown"; the caller skips risk assessment entirely for a customer
    with no known limit rather than guessing one.
    """
    row = await conn.fetchrow(
        "select credit_limit from credit_limits where installation_id = $1 and customer_id = $2",
        installation_id, customer_id,
    )
    return float(row["credit_limit"]) if row else None


async def _recommend_credit_action(
    conn: asyncpg.Connection, installation: asyncpg.Record, customer_id: str, action_type: str, exposure: float
) -> Any:
    """
    Shared by hold and release recommendations. Always requires
    approval, unconditionally — CONTEXT.md 2.5: "All credit limit
    changes, holds, and releases are recommendations only — a human
    executes or approves them." Reuses _lookup_approver()'s
    threshold-free lookup, same pattern as Dispute & Deduction's
    credit_memo/write_off gate.
    """
    approver = await _lookup_approver(conn, installation, action_type)
    task_id = await conn.fetchval(
        """
        insert into ar_tasks (installation_id, sub_agent, task_type, status, result)
        values ($1, 'credit_risk', $2, 'needs_approval', $3)
        returning task_id
        """,
        installation["installation_id"], action_type,
        {"customer_id": customer_id, "exposure": exposure},
    )
    await _create_approval_request(conn, installation["installation_id"], task_id, action_type, exposure, approver)
    await _log_audit(conn, installation["installation_id"], "ar_agent", f"{action_type}_recommended",
                      {"task_id": str(task_id), "customer_id": customer_id, "approver": approver})
    return task_id


async def recommend_credit_limit_change(
    conn: asyncpg.Connection, installation: asyncpg.Record, customer_id: str, proposed_limit: float, reason: str
) -> Any:
    """
    Not auto-triggered by anything today — deciding a NEW limit needs
    payment-behavior-trend data (DSO, broken promises) this project
    doesn't track (Collections & Dunning doesn't record promises-to-pay
    in its current form). Built and tested directly so the gate is
    proven correct ahead of whatever eventually calls it, same as
    request_dispute_resolution().
    """
    approver = await _lookup_approver(conn, installation, "credit_limit_change")
    task_id = await conn.fetchval(
        """
        insert into ar_tasks (installation_id, sub_agent, task_type, status, result)
        values ($1, 'credit_risk', 'credit_limit_change', 'needs_approval', $2)
        returning task_id
        """,
        installation["installation_id"],
        {"customer_id": customer_id, "proposed_limit": proposed_limit, "reason": reason},
    )
    await _create_approval_request(
        conn, installation["installation_id"], task_id, "credit_limit_change", proposed_limit, approver
    )
    await _log_audit(conn, installation["installation_id"], "ar_agent", "credit_limit_change_recommended",
                      {"task_id": str(task_id), "customer_id": customer_id, "approver": approver,
                       "proposed_limit": proposed_limit})
    return task_id


async def _process_credit_risk_for_customer(
    conn: asyncpg.Connection, installation: asyncpg.Record, customer_id: str, exposure: float
) -> None:
    credit_limit = await _fetch_credit_limit(conn, installation["installation_id"], customer_id)
    if credit_limit is None or credit_limit <= 0:
        return  # can't assess without a known limit — no data source exists yet, skip silently

    assessed = credit_risk.assess_risk(exposure, credit_limit)
    risk_level = assessed["risk_level"]

    existing = await conn.fetchrow(
        "select * from credit_risk_assessments where installation_id = $1 and customer_id = $2",
        installation["installation_id"], customer_id,
    )
    previous_level = existing["risk_level"] if existing else "normal"
    previous_recommendation = existing["last_recommendation"] if existing else None

    if existing:
        assessment_id = existing["assessment_id"]
        await conn.execute(
            """
            update credit_risk_assessments
            set exposure = $1, credit_limit = $2, risk_level = $3, last_assessed_at = $4
            where assessment_id = $5
            """,
            exposure, credit_limit, risk_level, datetime.now(timezone.utc), assessment_id,
        )
    else:
        assessment_id = await conn.fetchval(
            """
            insert into credit_risk_assessments (installation_id, customer_id, exposure, credit_limit, risk_level)
            values ($1, $2, $3, $4, $5)
            returning assessment_id
            """,
            installation["installation_id"], customer_id, exposure, credit_limit, risk_level,
        )

    if risk_level == previous_level:
        return  # no transition — numbers already updated above, nothing else to do

    if risk_level == "over_limit":
        await _recommend_credit_action(conn, installation, customer_id, "credit_hold", exposure)
        await conn.execute(
            "update credit_risk_assessments set last_recommendation = 'credit_hold' where assessment_id = $1",
            assessment_id,
        )
    elif previous_level == "over_limit" and previous_recommendation == "credit_hold":
        await _recommend_credit_action(conn, installation, customer_id, "credit_release", exposure)
        await conn.execute(
            "update credit_risk_assessments set last_recommendation = 'credit_release' where assessment_id = $1",
            assessment_id,
        )
    elif risk_level == "elevated" and previous_level == "normal":
        await _log_audit(conn, installation["installation_id"], "credit_risk_sub_agent", "risk_elevated",
                          {"customer_id": customer_id, "exposure": exposure, "credit_limit": credit_limit})
    # elevated -> normal recovery (without ever reaching over_limit): no
    # action beyond the numbers already updated above.


async def run_credit_risk(conn: asyncpg.Connection, installation: asyncpg.Record) -> int:
    open_invoices = await _fetch_open_invoices(conn, installation)
    exposure_by_customer: dict[str, float] = {}
    for invoice in open_invoices:
        customer_id = invoice.get("customer_id")
        if not customer_id:
            continue
        exposure_by_customer[customer_id] = exposure_by_customer.get(customer_id, 0) + invoice.get("amount_due", 0)

    for customer_id, exposure in exposure_by_customer.items():
        await _process_credit_risk_for_customer(conn, installation, customer_id, exposure)

    return len(exposure_by_customer)


async def _process_cash_application_event(
    conn: asyncpg.Connection, installation: asyncpg.Record, event: asyncpg.Record
) -> None:
    task_id = await conn.fetchval(
        """
        insert into ar_tasks (installation_id, sub_agent, task_type, status, input_event_id)
        values ($1, 'cash_application', 'cash_application_match', 'pending', $2)
        returning task_id
        """,
        installation["installation_id"],
        event["event_id"],
    )

    open_invoices = await _fetch_open_invoices(conn, installation)
    result = cash_application.match(event["payload"], open_invoices)
    amount = event["payload"].get("amount", 0)

    if result["match_type"] == "exact":
        # Deterministic, not AI-proposed — CONTEXT.md 2.2 only gates
        # AI-proposed matches below threshold, so this resolves without
        # a human. "Completed" means resolved by the AR Agent, not
        # actually posted to the ERP — no ERP write-back adapter exists
        # yet either.
        await conn.execute(
            "update ar_tasks set status = 'completed', result = $1, resolved_at = $2 where task_id = $3",
            result, datetime.now(timezone.utc), task_id,
        )
        await _log_audit(conn, installation["installation_id"], "cash_application_sub_agent",
                          "auto_resolved_exact_match", {"task_id": str(task_id), **result})
    elif result["match_type"] == "short_pay":
        # Hands off to the Dispute & Deduction sub-agent instead of the
        # normal cash_application_match/unidentified_cash approval path
        # below — CONTEXT.md 2.4: "Detects short-pays and disputes (from
        # Cash Application exceptions ...)". Opening the case itself
        # isn't gated (CONTEXT.md 2.4's approval gate is on *resolving*
        # via credit memo/write-off, not on opening); this task just
        # records that Cash Application handed it off.
        await conn.execute(
            "update ar_tasks set status = 'completed', result = $1, resolved_at = $2 where task_id = $3",
            result, datetime.now(timezone.utc), task_id,
        )
        case_id = await _open_dispute_case(conn, installation, result["invoice_id"], result["shortfall"])
        await _log_audit(conn, installation["installation_id"], "cash_application_sub_agent",
                          "short_pay_detected_dispute_opened",
                          {"task_id": str(task_id), "case_id": str(case_id), **result})
    else:
        # tolerance/amount_only matches (AI-proposed, per CONTEXT.md) and
        # fully unmatched cash both go through a policy check before
        # they're treated as resolved.
        action_type = "cash_application_match" if result["match_type"] != "unmatched" else "unidentified_cash"
        requires_approval, approver = await _check_requires_approval(conn, installation, action_type, amount)

        if requires_approval:
            await conn.execute(
                "update ar_tasks set status = 'needs_approval', result = $1 where task_id = $2",
                result, task_id,
            )
            await _create_approval_request(
                conn, installation["installation_id"], task_id, action_type, amount, approver
            )
            await _log_audit(conn, installation["installation_id"], "ar_agent", "parked_for_approval",
                              {"task_id": str(task_id), "action_type": action_type, "approver": approver, **result})
        else:
            await conn.execute(
                "update ar_tasks set status = 'completed', result = $1, resolved_at = $2 where task_id = $3",
                result, datetime.now(timezone.utc), task_id,
            )
            await _log_audit(conn, installation["installation_id"], "ar_agent", "auto_resolved",
                              {"task_id": str(task_id), **result})

    await conn.execute(
        "update cash_application_events set processed_at = $1 where event_id = $2",
        datetime.now(timezone.utc), event["event_id"],
    )


async def _queue_outbound_email(
    conn: asyncpg.Connection, installation_id: Any, to_address: str | None, subject: str, body: str
) -> None:
    """
    Every AR-facing email in this project goes through here, never a
    direct send — ARCHITECTURE.md: "Never request customer passwords."
    The Console doesn't hold SMTP/IMAP credentials, so it can only queue
    a request; connector_daemon.py's sync_outbound_email() polls
    GET /v1/outbox/pending and sends via the customer's own mail server.
    A missing to_address still queues (rather than silently dropping)
    so the gap is visible in the outbox/admin dashboard, not invisible.
    """
    await conn.execute(
        "insert into outbound_emails (installation_id, to_address, subject, body) values ($1, $2, $3, $4)",
        installation_id, to_address or "unknown", subject, body,
    )


async def _send_invoice_email(conn: asyncpg.Connection, installation_id: Any, to_contact: str | None, invoice_id: Any) -> None:
    # TODO: real content would attach the actual invoice (PDF/portal
    # link) — no such generation exists yet, this is a plain-text
    # placeholder body, disclosed rather than pretending it's complete.
    await _queue_outbound_email(
        conn, installation_id, to_contact,
        subject=f"Invoice {invoice_id}",
        body=f"Please find your invoice {invoice_id} attached. (placeholder body — no real invoice document generation exists yet)",
    )


async def _process_invoice_delivery_event(
    conn: asyncpg.Connection, installation: asyncpg.Record, event: asyncpg.Record
) -> None:
    task_id = await conn.fetchval(
        """
        insert into ar_tasks (installation_id, sub_agent, task_type, status, input_event_id)
        values ($1, 'invoice_delivery', 'invoice_validation', 'pending', $2)
        returning task_id
        """,
        installation["installation_id"],
        event["event_id"],
    )

    invoice = event["payload"]
    contract_terms = await _fetch_contract_terms(
        conn, installation["tenant_id"], invoice.get("customer_id")
    )
    result = invoice_delivery.validate(invoice, contract_terms)

    if result["valid"]:
        # No approval gate for standard delivery (CONTEXT.md 2.1).
        # "Completed" means the AR Agent decided to deliver — the actual
        # send is queued to outbound_emails and happens on the
        # connector's next sync cycle via the customer's own mail server.
        await conn.execute(
            "update ar_tasks set status = 'completed', result = $1, resolved_at = $2 where task_id = $3",
            result, datetime.now(timezone.utc), task_id,
        )
        await _send_invoice_email(conn, installation["installation_id"], invoice.get("bill_to_contact"), invoice.get("invoice_id"))
        await _log_audit(conn, installation["installation_id"], "invoice_delivery_sub_agent",
                          "delivered", {"task_id": str(task_id), **result})
    else:
        # A failed validation isn't a decision for a human to approve or
        # reject — it's a defect a human has to go fix in the ERP
        # (CONTEXT.md: "Any correction to an invoice goes back to the
        # ERP through a human"). 'exception' surfaces it as a worklist
        # item (same idea as needs_version_follow_up elsewhere in this
        # project), not an approval_queue entry.
        await conn.execute(
            "update ar_tasks set status = 'exception', result = $1 where task_id = $2",
            result, task_id,
        )
        await _log_audit(conn, installation["installation_id"], "ar_agent", "invoice_validation_exception",
                          {"task_id": str(task_id), **result})

    await conn.execute(
        "update invoice_delivery_events set processed_at = $1 where event_id = $2",
        datetime.now(timezone.utc), event["event_id"],
    )


async def _process_customer_inbox_event(
    conn: asyncpg.Connection, installation: asyncpg.Record, event: asyncpg.Record
) -> None:
    message = event["payload"]
    subject = message.get("subject", "")
    body = message.get("body", "")

    # Per-message language, for real routing/triage — not for narrowing
    # needs_escalation() below, which always checks every language in
    # ESCALATION_KEYWORDS regardless (see that call site). `language` on
    # the message itself is the highest-precision signal when the
    # connector/mail system happens to supply it (honestly rare today —
    # no real source sets it yet); `customer_id`, when present, both
    # remembers a declared language for next time and falls back to
    # whatever's already known about that customer when this message
    # doesn't declare one itself. Both are optional, honest-None reads —
    # nothing in this project produces either field yet (message-to-
    # customer linkage is the same unbuilt AI extraction step
    # classify_message() already discloses), but the plumbing is real
    # the moment a connector does start sending them.
    customer_id = message.get("customer_id")
    language = message.get("language")
    if language and customer_id:
        await _upsert_customer_language(conn, installation["installation_id"], customer_id, language)
    elif not language and customer_id:
        language = await _fetch_customer_language(conn, installation["installation_id"], customer_id)

    task_id = await conn.fetchval(
        """
        insert into ar_tasks (installation_id, sub_agent, task_type, status, input_event_id)
        values ($1, 'customer_inbox', 'inbound_message', 'pending', $2)
        returning task_id
        """,
        installation["installation_id"],
        event["event_id"],
    )

    if customer_inbox.needs_escalation(subject, body):
        # Deterministic per CONTEXT.md's own column split — a keyword
        # match on "angry, legal-sounding" content, not an AI judgment
        # call. 'exception': a human has to read and handle this
        # personally, there's no approve/reject decision to make.
        # `language`, when known, rides along so whoever triages this
        # exception (or a future reply-drafting step) knows what
        # language they're dealing with — it did not affect whether
        # this message escalated in the first place.
        result = {"reason": "escalation_keyword_match", "language": language}
        await conn.execute(
            "update ar_tasks set status = 'exception', result = $1 where task_id = $2",
            result, task_id,
        )
        await _log_audit(conn, installation["installation_id"], "customer_inbox_sub_agent", "escalated_to_human",
                          {"task_id": str(task_id), **result})
    else:
        classification = customer_inbox.classify_message(subject, body)
        if classification is None:
            # No AI classifier exists anywhere in this project — every
            # real message hits this branch today. That's the correct
            # behavior per CONTEXT.md's own wording ("escalates anything
            # ... unclassifiable to a human"), not a gap being papered
            # over.
            result = {"reason": "unclassifiable_no_ai_classifier", "language": language}
            await conn.execute(
                "update ar_tasks set status = 'exception', result = $1 where task_id = $2",
                result, task_id,
            )
            await _log_audit(conn, installation["installation_id"], "customer_inbox_sub_agent", "escalated_to_human",
                              {"task_id": str(task_id), **result})
        else:
            # Only reachable today via a monkey-patched classify_message
            # in testing. Even a successful classification still can't
            # auto-send: no tenant has a configured auto-send whitelist,
            # so CONTEXT.md's "draft-only mode" default applies —
            # everything drafted goes through the same unconditional
            # approval gate as Dispute & Deduction's credit_memo/write_off.
            routed_to = customer_inbox.route_classification(classification)
            draft = customer_inbox.draft_reply(classification, message)
            approver = await _lookup_approver(conn, installation, "customer_inbox_reply")
            result = {"classification": classification, "routed_to": routed_to, "draft": draft, "language": language}
            await conn.execute(
                "update ar_tasks set status = 'needs_approval', result = $1 where task_id = $2",
                result, task_id,
            )
            await _create_approval_request(
                conn, installation["installation_id"], task_id, "customer_inbox_reply", 0, approver
            )
            await _log_audit(conn, installation["installation_id"], "customer_inbox_sub_agent", "classified_and_drafted",
                              {"task_id": str(task_id), "approver": approver, **result})

    await conn.execute(
        "update customer_inbox_events set processed_at = $1 where event_id = $2",
        datetime.now(timezone.utc), event["event_id"],
    )


async def _has_payment_landed(conn: asyncpg.Connection, installation_id: Any, invoice_id: Any) -> bool:
    """
    Real check, using data this project actually has: a completed Cash
    Application match against this invoice counts as payment landed
    (CONTEXT.md 2.3: dunning "pauses ... automatically when ... a
    payment lands"). Only ever True today if a real match exists for
    this invoice_id — in production that needs _fetch_open_invoices to
    return real data first, same gap as everywhere else in this file.

    Only 'exact' and 'tolerance' count as the invoice actually being
    paid — both require the payment to reference this specific invoice
    and be at (or within $1 of) the full amount_due. 'short_pay' also
    reaches status='completed', but that only means Cash Application's
    own job (matching and handing off to Dispute & Deduction) is done,
    not that the invoice was paid off — real money is still owed.
    'amount_only' is excluded too, more conservatively: it's a
    confidence-0.6 coincidental amount match with no reference match to
    this invoice at all (per sub_agents/cash_application.py), too weak
    a signal to treat as confirmation that *this* invoice was paid.
    """
    row = await conn.fetchrow(
        """
        select 1 from ar_tasks
        where installation_id = $1 and sub_agent = 'cash_application'
          and status = 'completed' and result->>'invoice_id' = $2
          and result->>'match_type' in ('exact', 'tolerance')
        """,
        installation_id, str(invoice_id),
    )
    return row is not None


async def _has_open_dispute(conn: asyncpg.Connection, installation_id: Any, invoice_id: Any) -> bool:
    """
    Real check now that dispute_cases exists (Dispute & Deduction
    sub-agent). 'resolved' is the only terminal status — 'open',
    'sla_breached', and 'pending_resolution' all count as still disputed
    for dunning-pause purposes.
    """
    row = await conn.fetchrow(
        "select 1 from dispute_cases where installation_id = $1 and invoice_id = $2 and status != 'resolved'",
        installation_id, str(invoice_id),
    )
    return row is not None


STEP_SUBJECTS = {
    "reminder": "Friendly reminder: invoice {invoice_id} is now overdue",
    "follow_up": "Follow-up: invoice {invoice_id} remains unpaid",
    "escalation": "Second notice: invoice {invoice_id} requires attention",
    "final_demand": "Final demand: invoice {invoice_id}",
}


async def _send_dunning_email(conn: asyncpg.Connection, installation_id: Any, contact: str | None, invoice_id: Any, step: str) -> None:
    # TODO: real content needs the tone/wording CONTEXT.md 2.3 calls
    # AI-assisted, drafted within approved templates — no such drafting
    # exists yet, this is a fixed placeholder body per step.
    subject = STEP_SUBJECTS.get(step, f"Regarding invoice {invoice_id}").format(invoice_id=invoice_id)
    await _queue_outbound_email(
        conn, installation_id, contact, subject,
        body=f"This is a {step} notice regarding invoice {invoice_id}. (placeholder body — no AI drafting exists yet)",
    )


async def _create_dunning_task_and_sequence(
    conn: asyncpg.Connection,
    installation: asyncpg.Record,
    invoice_id: Any,
    contact: str | None,
    bucket: str,
    step: str,
    task_type: str,
    action_type: str,
    requires_approval: bool,
    approver: str | None,
    amount: float,
) -> None:
    """
    First-time creation of a dunning_sequences row, gated or not — shared
    by the plain-activation path and the "first contact already at
    final_demand severity" path in _process_dunning_for_invoice(), which
    both need to create the row but differ only in which gate applies.
    """
    task_id = await conn.fetchval(
        """
        insert into ar_tasks (installation_id, sub_agent, task_type, status, result)
        values ($1, 'collections_dunning', $2, $3, $4)
        returning task_id
        """,
        installation["installation_id"], task_type,
        "needs_approval" if requires_approval else "completed",
        {"invoice_id": str(invoice_id), "step": step, "bucket": bucket},
    )
    pending_status = "pending_final_demand" if task_type == "final_demand_letter" else "pending_activation"
    await conn.execute(
        """
        insert into dunning_sequences (installation_id, invoice_id, contact, status, aging_bucket, current_step)
        values ($1, $2, $3, $4, $5, $6)
        """,
        installation["installation_id"], str(invoice_id), contact,
        pending_status if requires_approval else "active",
        bucket, None if requires_approval else step,
    )
    if requires_approval:
        await _create_approval_request(
            conn, installation["installation_id"], task_id, action_type, amount, approver
        )
        await _log_audit(conn, installation["installation_id"], "ar_agent", f"{task_type}_pending_approval",
                          {"task_id": str(task_id), "invoice_id": str(invoice_id), "approver": approver})
    else:
        await _send_dunning_email(conn, installation["installation_id"], contact, invoice_id, step)
        await _log_audit(conn, installation["installation_id"], "collections_dunning_sub_agent",
                          "sequence_activated", {"task_id": str(task_id), "invoice_id": str(invoice_id), "step": step})


async def _process_dunning_for_invoice(
    conn: asyncpg.Connection, installation: asyncpg.Record, invoice: dict[str, Any]
) -> None:
    invoice_id = invoice["invoice_id"]

    sequence = await conn.fetchrow(
        "select * from dunning_sequences where installation_id = $1 and invoice_id = $2",
        installation["installation_id"], str(invoice_id),
    )

    if sequence and sequence["status"] in ("completed", "paused"):
        return  # terminal for this MVP — no automatic resume without real dispute-tracking

    if await _has_payment_landed(conn, installation["installation_id"], invoice_id):
        if sequence:
            await conn.execute(
                "update dunning_sequences set status = 'completed', last_action_at = $1 where sequence_id = $2",
                datetime.now(timezone.utc), sequence["sequence_id"],
            )
            await _log_audit(conn, installation["installation_id"], "collections_dunning_sub_agent",
                              "sequence_completed_payment_landed", {"invoice_id": str(invoice_id)})
        return

    if await _has_open_dispute(conn, installation["installation_id"], invoice_id):
        if sequence and sequence["status"] == "active":
            await conn.execute(
                "update dunning_sequences set status = 'paused', last_action_at = $1 where sequence_id = $2",
                datetime.now(timezone.utc), sequence["sequence_id"],
            )
            await _log_audit(conn, installation["installation_id"], "collections_dunning_sub_agent",
                              "sequence_paused_dispute_open", {"invoice_id": str(invoice_id)})
        return

    due_date = invoice["due_date"]
    if isinstance(due_date, str):
        due_date = date.fromisoformat(due_date)
    bucket = collections_dunning.aging_bucket(due_date, datetime.now(timezone.utc).date())
    step = collections_dunning.target_step(bucket)

    if step is None:
        return  # not yet overdue, nothing to do

    amount = invoice.get("amount_due", 0)
    contact = invoice.get("bill_to_contact")
    if not contact and invoice.get("customer_id"):
        # Falls back to the customer's own contact email (from `customers`,
        # added 2026-09-09) when the invoice itself has no BillEmail set —
        # otherwise dunning would silently have nowhere to send to even
        # though a real contact exists at the customer level.
        contact = await _fetch_customer_contact_email(conn, installation["installation_id"], invoice["customer_id"])

    if sequence is None:
        # First-time activation — CONTEXT.md 2.3's own action_type, and
        # the only sub-agent so far where a pre-configured policy rule
        # can actually apply: "dunning_activation" is one of the three
        # action types installer_skeleton.py's collect_policy_config()
        # already asks about, unlike unidentified_cash/cash_application_match
        # /final_demand_letter, which always fail closed with no approver.
        requires_activation, activation_approver = await _check_requires_approval(
            conn, installation, "dunning_activation", amount
        )
        if requires_activation:
            await _create_dunning_task_and_sequence(
                conn, installation, invoice_id, contact, bucket, step,
                "dunning_activation", "dunning_activation", True, activation_approver, amount,
            )
            return

        # Activation is clear. CONTEXT.md lists final-demand letters as
        # their own separate gate — a severely overdue invoice seen for
        # the very first time still lands on the final_demand step, and
        # that gate applies just the same; a clear activation doesn't
        # supersede it.
        if step == "final_demand":
            requires_fd, fd_approver = await _check_requires_approval(
                conn, installation, "final_demand_letter", amount
            )
            await _create_dunning_task_and_sequence(
                conn, installation, invoice_id, contact, bucket, step,
                "final_demand_letter", "final_demand_letter", requires_fd, fd_approver, amount,
            )
            return

        await _create_dunning_task_and_sequence(
            conn, installation, invoice_id, contact, bucket, step,
            "dunning_activation", "dunning_activation", False, None, amount,
        )
        return

    if sequence["status"] in ("pending_activation", "pending_final_demand"):
        return  # waiting on a human — no auto-promotion without an approval-resolution flow

    if step == sequence["current_step"]:
        return  # no transition since last cycle, nothing new to do

    if step == "final_demand":
        requires_approval, approver = await _check_requires_approval(
            conn, installation, "final_demand_letter", amount
        )
        task_id = await conn.fetchval(
            """
            insert into ar_tasks (installation_id, sub_agent, task_type, status, result)
            values ($1, 'collections_dunning', 'final_demand_letter', $2, $3)
            returning task_id
            """,
            installation["installation_id"],
            "needs_approval" if requires_approval else "completed",
            {"invoice_id": str(invoice_id), "step": step, "bucket": bucket},
        )
        if requires_approval:
            await conn.execute(
                "update dunning_sequences set status = 'pending_final_demand', last_action_at = $1 where sequence_id = $2",
                datetime.now(timezone.utc), sequence["sequence_id"],
            )
            await _create_approval_request(
                conn, installation["installation_id"], task_id, "final_demand_letter", amount, approver
            )
            await _log_audit(conn, installation["installation_id"], "ar_agent", "final_demand_pending_approval",
                              {"task_id": str(task_id), "invoice_id": str(invoice_id), "approver": approver})
            return
        # else: falls through to the same advance-and-notify path below

    await conn.execute(
        "update dunning_sequences set current_step = $1, aging_bucket = $2, last_action_at = $3 where sequence_id = $4",
        step, bucket, datetime.now(timezone.utc), sequence["sequence_id"],
    )
    await _send_dunning_email(conn, installation["installation_id"], contact, invoice_id, step)
    await _log_audit(conn, installation["installation_id"], "collections_dunning_sub_agent",
                      "sequence_advanced", {"invoice_id": str(invoice_id), "step": step})


async def _generate_ar_report(conn: asyncpg.Connection, installation: asyncpg.Record) -> dict[str, Any]:
    """
    No approval gate, no ar_tasks, no audit_log spam every cycle — this
    sub-agent only computes and refreshes a snapshot (CONTEXT.md 2.7:
    "Approval gates: None — read-only"). Runs unconditionally every
    cycle, even when there's nothing open: an all-zero report is still an
    honest answer ("no open AR data available right now"), not noise.
    """
    today = datetime.now(timezone.utc).date()
    open_invoices = await _fetch_open_invoices(conn, installation)
    dispute_rows = await conn.fetch(
        "select * from dispute_cases where installation_id = $1", installation["installation_id"]
    )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "aging_summary": ar_reporting.aging_summary(open_invoices, today),
        "total_open_ar": sum(inv.get("amount_due", 0) for inv in open_invoices),
        "dso": ar_reporting.dso(),
        "cei": ar_reporting.cei(),
        "dispute_aging": ar_reporting.dispute_aging(dispute_rows, today),
        "cash_inflow_forecast": ar_reporting.cash_inflow_forecast(open_invoices, today),
    }

    await conn.execute(
        """
        insert into ar_reports (installation_id, report, generated_at)
        values ($1, $2, $3)
        on conflict (installation_id) do update set
            report = excluded.report, generated_at = excluded.generated_at
        """,
        installation["installation_id"], report, datetime.now(timezone.utc),
    )
    return report


async def run_ar_agent(conn: asyncpg.Connection, installation: asyncpg.Record) -> int:
    """
    Processes all unprocessed work for one installation. Returns the
    number of items processed (0 is normal — most cycles find nothing new).
    """
    await _sync_qbo_payments(conn, installation)  # before the fetch below, so new events get picked up this same cycle

    cash_events = await conn.fetch(
        "select * from cash_application_events where installation_id = $1 and processed_at is null",
        installation["installation_id"],
    )
    for event in cash_events:
        await _process_cash_application_event(conn, installation, event)

    invoice_events = await conn.fetch(
        "select * from invoice_delivery_events where installation_id = $1 and processed_at is null",
        installation["installation_id"],
    )
    for event in invoice_events:
        await _process_invoice_delivery_event(conn, installation, event)

    inbox_events = await conn.fetch(
        "select * from customer_inbox_events where installation_id = $1 and processed_at is null",
        installation["installation_id"],
    )
    for event in inbox_events:
        await _process_customer_inbox_event(conn, installation, event)

    await _sync_customers(conn, installation)  # before dunning below, so its contact fallback has fresh data

    open_invoices = await _fetch_open_invoices(conn, installation)
    for invoice in open_invoices:
        await _process_dunning_for_invoice(conn, installation, invoice)

    disputes_checked = await _check_dispute_slas(conn, installation)
    credit_customers = await run_credit_risk(conn, installation)
    await _generate_ar_report(conn, installation)  # no approval gate — always runs, never counted as "processed work"

    return (len(cash_events) + len(invoice_events) + len(inbox_events)
            + len(open_invoices) + disputes_checked + credit_customers)
