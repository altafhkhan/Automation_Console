"""
Cash Application sub-agent (CONTEXT.md Section 2.2) — deterministic
matching only. A pure function: no database, no I/O. The AR Agent
supplies the open-invoice data and persists whatever this returns.

Golden rule (INSTRUCTIONS.md / CONTEXT.md): AI reads, drafts, classifies,
and prioritizes. Deterministic code calculates and decides. There is no
AI/LLM call here — every rule below is exact arithmetic, which is also
why an "exact" match can resolve without a human in the loop while
anything fuzzier can't (see ar_agent.py's approval-gate logic).
"""

from typing import Any

AMOUNT_TOLERANCE = 1.00  # dollars — "amount-within-tolerance" per CONTEXT.md 2.2


def match(event: dict[str, Any], open_invoices: list[dict[str, Any]]) -> dict[str, Any]:
    """
    event: {"amount": float, "reference": str, "received_date": str, ...}
    open_invoices: [{"invoice_id": str, "reference": str, "amount_due": float}, ...]

    Returns one of:
      {"match_type": "exact",       "confidence": 1.0, "invoice_id": ...}
      {"match_type": "tolerance",   "confidence": 0.9, "invoice_id": ...}
      {"match_type": "short_pay",   "confidence": 0.8, "invoice_id": ..., "shortfall": ...}
      {"match_type": "amount_only", "confidence": 0.6, "invoice_id": ...}
      {"match_type": "unmatched",   "confidence": 0.0, "reason": ...}

    short_pay is CONTEXT.md 2.2's own term ("Detects short-pays and
    disputes") — the reference matches a real invoice, but materially
    less was received than invoiced. ar_agent.py routes this to the
    Dispute & Deduction sub-agent instead of the normal
    cash_application_match/unidentified_cash approval path.
    """
    if not open_invoices:
        return {"match_type": "unmatched", "confidence": 0.0, "reason": "no_open_invoice_data"}

    reference = event.get("reference")
    amount = event.get("amount")

    by_reference = [inv for inv in open_invoices if inv.get("reference") == reference]
    if by_reference:
        invoice = by_reference[0]
        shortfall = invoice["amount_due"] - amount
        if invoice["amount_due"] == amount:
            return {"match_type": "exact", "confidence": 1.0, "invoice_id": invoice["invoice_id"]}
        if abs(shortfall) <= AMOUNT_TOLERANCE:
            return {"match_type": "tolerance", "confidence": 0.9, "invoice_id": invoice["invoice_id"]}
        if shortfall > AMOUNT_TOLERANCE:
            return {
                "match_type": "short_pay", "confidence": 0.8,
                "invoice_id": invoice["invoice_id"], "shortfall": shortfall,
            }
        # shortfall < -AMOUNT_TOLERANCE: paid MORE than invoiced — an
        # overpayment, not a dispute. Falls through to amount-based
        # matching below (which won't find this exact amount on any
        # invoice) and surfaces as unmatched — an honest outcome for now;
        # overpayment handling isn't built.

    by_amount = [inv for inv in open_invoices if inv.get("amount_due") == amount]
    if len(by_amount) == 1:
        return {"match_type": "amount_only", "confidence": 0.6, "invoice_id": by_amount[0]["invoice_id"]}

    return {"match_type": "unmatched", "confidence": 0.0, "reason": "no_matching_invoice"}
