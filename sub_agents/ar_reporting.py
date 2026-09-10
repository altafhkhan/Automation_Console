"""
AR Reporting & Forecast sub-agent (CONTEXT.md Section 2.7) —
deterministic metric calculations only. Pure functions: no database, no
I/O. ar_agent.py supplies the raw data and persists whatever this
returns.

CONTEXT.md is explicit this sub-agent has no approval gate at all
("Approval gates: None — read-only") and that every figure must be
reproducible and auditable — "an LLM never computes a figure." There is
no AI/LLM call anywhere in this module; explaining numbers in plain
English, anomaly narration, and drafting a weekly summary email are all
listed as AI-assisted in CONTEXT.md 2.7 and aren't built here — see
narrate_report() below.

Two of the five named metrics — DSO and CEI — need period-over-period
sales/receivables history this project doesn't track anywhere (no
historical AR snapshots, no revenue/sales data source). Rather than
compute either from a guessed baseline, both report {"value": None,
"reason": ...} — the same honesty pattern used throughout this project
for missing ERP data.
"""

from datetime import date
from typing import Any

from sub_agents.collections_dunning import aging_bucket

AGING_BUCKETS_ORDER = ["current", "1-30", "31-60", "61-90", "90+"]


def aging_summary(open_invoices: list[dict[str, Any]], today: date) -> dict[str, float]:
    summary = {bucket: 0.0 for bucket in AGING_BUCKETS_ORDER}
    for invoice in open_invoices:
        due_date = invoice["due_date"]
        if isinstance(due_date, str):
            due_date = date.fromisoformat(due_date)
        bucket = aging_bucket(due_date, today)
        summary[bucket] += invoice.get("amount_due", 0)
    return summary


def dso() -> dict[str, Any]:
    """
    Days Sales Outstanding = (Average AR / Total Credit Sales) x Period
    Days. No sales/revenue history is tracked anywhere in this project —
    computing this from a guessed sales figure would be a fabrication,
    not a deterministic calculation.
    """
    return {"value": None, "reason": "no_sales_data_source"}


def cei() -> dict[str, Any]:
    """
    Collection Effectiveness Index needs beginning/ending receivables and
    period credit sales — the same missing history as dso() above.
    """
    return {"value": None, "reason": "no_period_over_period_data"}


def dispute_aging(dispute_cases: list[Any], today: date) -> dict[str, Any]:
    """dispute_cases: rows from the dispute_cases table (dict-like)."""
    open_cases = [c for c in dispute_cases if c["status"] != "resolved"]
    if not open_cases:
        return {"open_count": 0, "sla_breached_count": 0, "average_days_open": 0.0, "total_disputed_amount": 0.0}

    days_open = []
    for c in open_cases:
        opened = c["opened_at"]
        opened_date = opened.date() if hasattr(opened, "date") else opened
        days_open.append((today - opened_date).days)

    return {
        "open_count": len(open_cases),
        "sla_breached_count": sum(1 for c in open_cases if c["status"] == "sla_breached"),
        "average_days_open": sum(days_open) / len(days_open),
        "total_disputed_amount": float(sum(c["amount"] for c in open_cases)),
    }


def cash_inflow_forecast(open_invoices: list[dict[str, Any]], today: date) -> dict[str, float]:
    """
    Simplified, disclosed: projects invoices at face value by due date —
    doesn't adjust for promise-to-pay commitments or historical
    payment-behavior timing (CONTEXT.md's "from due dates + promises +
    payment-behavior history"), since neither is tracked anywhere in
    this project yet (Collections & Dunning doesn't record
    promise-to-pay dates in its current form).
    """
    buckets = {"overdue": 0.0, "this_week": 0.0, "next_week": 0.0, "2_4_weeks": 0.0, "30_plus_days": 0.0}
    for invoice in open_invoices:
        due_date = invoice["due_date"]
        if isinstance(due_date, str):
            due_date = date.fromisoformat(due_date)
        days_until_due = (due_date - today).days
        amount = invoice.get("amount_due", 0)
        if days_until_due < 0:
            buckets["overdue"] += amount
        elif days_until_due <= 7:
            buckets["this_week"] += amount
        elif days_until_due <= 14:
            buckets["next_week"] += amount
        elif days_until_due <= 30:
            buckets["2_4_weeks"] += amount
        else:
            buckets["30_plus_days"] += amount
    return buckets


def narrate_report(report: dict[str, Any]) -> str | None:
    """
    TODO: needs an LLM call — CONTEXT.md's own examples ("what's driving
    the jump in 60+ days?", plain-English explanations, anomaly
    narration, drafting the weekly summary email). No model integration
    exists anywhere in this project. None is honest, not a guess.
    """
    return None
