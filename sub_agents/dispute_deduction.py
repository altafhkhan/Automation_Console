"""
Dispute & Deduction sub-agent (CONTEXT.md Section 2.4) — deterministic
routing/SLA logic only. Pure functions: no database, no I/O.

Golden rule: no AI/LLM call here. Classifying a customer's claim into a
reason code (pricing, quantity, damaged goods, tax, duplicate billing)
and drafting a proposed resolution are both listed as AI-assisted in
CONTEXT.md 2.4 — they need a model this project hasn't wired up anywhere.
classify_reason() below is a deliberate stub, not a guess.
"""

from datetime import date

SLA_DAYS_DEFAULT = 30  # no tenant-configurable SLA exists yet — a fixed default, disclosed

# CONTEXT.md 2.4's own example tiers (analyst < $500, controller < $5,000, CFO above).
OWNER_TIERS = [
    (500, "analyst"),
    (5000, "controller"),
]
OWNER_ABOVE_TIERS = "cfo"


def classify_reason(claim_text: str | None) -> str | None:
    """
    TODO: needs an LLM call reading the customer's actual claim text.
    No claim text exists yet either — Cash Application short-pays carry
    no customer explanation, and the Customer Inbox sub-agent (the other
    source CONTEXT.md names) doesn't exist. None is honest here, not a
    guessed reason code.
    """
    return None


def suggest_owner(amount: float) -> str:
    """
    Amount-tiered routing suggestion — who should look at this case, not
    the approval gate itself. CONTEXT.md 2.4 is explicit that every
    credit memo and write-off requires approval regardless of amount
    ("no exceptions in V1"); that's enforced in ar_agent.py, not here.
    """
    for threshold, owner in OWNER_TIERS:
        if amount < threshold:
            return owner
    return OWNER_ABOVE_TIERS


def is_sla_breached(opened_at: date, today: date, sla_days: int = SLA_DAYS_DEFAULT) -> bool:
    return (today - opened_at).days > sla_days
