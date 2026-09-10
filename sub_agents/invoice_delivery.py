"""
Invoice Delivery & Validation sub-agent (CONTEXT.md Section 2.1) —
deterministic validation only. A pure function: no database, no I/O.
Invoice creation stays in the customer's ERP (their system of record);
this sub-agent only checks an invoice is correct and ready before it
goes out.

Approval gates: none for standard delivery (CONTEXT.md) — an invalid
invoice isn't something the AR Agent asks a human to approve or reject,
it's something a human has to go fix in the ERP. ar_agent.py reflects
this by routing failures to task status 'exception', not 'needs_approval'.

Golden rule (INSTRUCTIONS.md / CONTEXT.md): deterministic code decides.
The "AI-assisted" half of this sub-agent — detecting errors a rule can't
catch, drafting the cover email — needs an actual LLM call, which this
project hasn't wired up anywhere yet. detect_likely_errors() below is a
deliberate stub, not a fabricated result.
"""

from typing import Any


def validate(invoice: dict[str, Any], contract_terms: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    invoice: {"invoice_id": str, "po_number": str | None,
              "bill_to_contact": str | None, "terms": str | None, ...}
    contract_terms: this customer's contracted terms, if a data source
        for it exists yet (it doesn't — see ar_agent.py's
        _fetch_contract_terms()). None means "can't verify," which does
        NOT count as a failure — CONTEXT.md's own worklist pattern
        (see needs_version_follow_up elsewhere in this project) is to
        surface what's unverifiable rather than block on it.

    Returns {"valid": bool, "checks": [{"check": str, "passed": bool | None, ...}]}.
    valid is False only when a check that actually ran came back False —
    an unverifiable check never blocks delivery on its own.
    """
    checks = [
        {"check": "po_number_present", "passed": bool(invoice.get("po_number"))},
        {"check": "bill_to_contact_present", "passed": bool(invoice.get("bill_to_contact"))},
    ]

    if contract_terms is None:
        checks.append({"check": "terms_match_contract", "passed": None, "reason": "no_contract_data_source"})
    else:
        checks.append({
            "check": "terms_match_contract",
            "passed": invoice.get("terms") == contract_terms.get("terms"),
        })

    valid = not any(c["passed"] is False for c in checks)
    return {"valid": valid, "checks": checks}


def detect_likely_errors(invoice: dict[str, Any]) -> list[str]:
    """
    TODO: needs an actual LLM call (e.g. flagging a wrong entity name or
    a mismatched contract reference that no fixed rule above would
    catch). No model integration exists anywhere in this project yet —
    returning an empty list is honest about that, not a real check.
    """
    return []
