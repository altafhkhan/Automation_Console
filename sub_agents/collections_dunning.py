"""
Collections & Dunning sub-agent (CONTEXT.md Section 2.3) — deterministic
aging + step calculation only. A pure function: no database, no I/O.

Simplification disclosed: CONTEXT.md lists "aging buckets" and "dunning
schedule and step timing" as two separate deterministic pieces; this
project ties the dunning step directly to the aging bucket rather than a
separate day-count-since-last-step timer, to keep a first version
concrete. Revisit if a tenant ever needs, say, a reminder that repeats
every 7 days within the same bucket rather than firing once per bucket
transition.

Golden rule: no AI/LLM call here. Tone/wording of reminders and ranking
accounts by likelihood-to-pay — both listed as AI-assisted in CONTEXT.md
2.3 — need a model this project hasn't wired up anywhere yet.

Late-fee/interest calculation (also in CONTEXT.md's Deterministic column)
isn't implemented either — it needs a tenant fee-schedule configuration
that doesn't exist in this project's data model, and a guessed rate would
be a fabrication, not a deterministic calculation.
"""

from datetime import date

STEP_BY_BUCKET: dict[str, str | None] = {
    "current": None,
    "1-30": "reminder",
    "31-60": "follow_up",
    "61-90": "escalation",
    "90+": "final_demand",
}


def aging_bucket(due_date: date, today: date) -> str:
    days_overdue = (today - due_date).days
    if days_overdue <= 0:
        return "current"
    if days_overdue <= 30:
        return "1-30"
    if days_overdue <= 60:
        return "31-60"
    if days_overdue <= 90:
        return "61-90"
    return "90+"


def target_step(bucket: str) -> str | None:
    return STEP_BY_BUCKET[bucket]
