"""
Credit Risk sub-agent (CONTEXT.md Section 2.5) — deterministic exposure
math and risk scoring only. Pure functions: no database, no I/O.

CONTEXT.md is explicit that every output here is a recommendation, never
an executed action: "All credit limit changes, holds, and releases are
recommendations only — a human executes or approves them." ar_agent.py
reflects this with an unconditional approval gate (no threshold-based
auto-approval, unlike most other sub-agents), reusing the same
_lookup_approver() built for Dispute & Deduction's credit_memo/write_off
gate — recommendations always need a human, regardless of amount.

Golden rule: no AI/LLM call here. Narrative risk summaries ("payments
have stretched from 32 to 58 days...") and surfacing external
credit-bureau signals are both listed as AI-assisted in CONTEXT.md 2.5 —
narrate_risk() below is a deliberate stub, not a guess.
"""

# Configuration, not a hardcoded business rule (CONTEXT.md: "the scoring
# formula itself — weights are configuration, not LLM output"). Tune
# here; there's no per-tenant override mechanism yet, a disclosed
# simplification.
ELEVATED_UTILIZATION = 0.8
OVER_LIMIT_UTILIZATION = 1.0


def assess_risk(exposure: float, credit_limit: float) -> dict:
    """
    credit_limit must be > 0 — the caller (ar_agent.py) skips a customer
    with no known limit entirely rather than dividing by zero or guessing
    a default.
    """
    utilization = exposure / credit_limit
    if utilization >= OVER_LIMIT_UTILIZATION:
        risk_level = "over_limit"
    elif utilization >= ELEVATED_UTILIZATION:
        risk_level = "elevated"
    else:
        risk_level = "normal"
    return {"risk_level": risk_level, "utilization": utilization}


def narrate_risk(customer_id: str, exposure: float, credit_limit: float, utilization: float) -> str | None:
    """
    TODO: needs an LLM call reading payment-behavior trends over time
    (CONTEXT.md's own example: "payments have stretched from 32 to 58
    days over two quarters; two broken promises-to-pay"). No DSO or
    promise-to-pay history is tracked anywhere in this project yet
    either — Collections & Dunning doesn't record promises-to-pay in its
    current form. None is honest here, not a fabricated narrative.
    """
    return None
