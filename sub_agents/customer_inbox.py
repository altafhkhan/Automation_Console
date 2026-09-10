"""
Customer Inbox sub-agent (CONTEXT.md Section 2.6) — deterministic
escalation and routing only. Pure functions: no database, no I/O.

Golden rule: no AI/LLM call here. Classifying a message (remittance
advice, invoice copy request, dispute, payment promise, W-9/tax doc
request, general question), extracting invoice numbers/amounts/dates
from it, and drafting a reply are all listed as AI-assisted in
CONTEXT.md 2.6 — none of that exists anywhere in this project.
classify_message() and draft_reply() below are deliberate stubs, not
guesses.

Escalation triggers ARE deterministic per CONTEXT.md's own column split
— a keyword match on "angry, legal-sounding" content is a legitimate,
honest deterministic implementation, not a placeholder for something
smarter later.
"""

from typing import Any

# Escalation-trigger vocabulary, by language. This is a fixed, curated
# word list, not language detection or translation — there's no
# language/locale field anywhere in this project's data model to key a
# smarter per-message lookup off of (see skills.md's 2026-09-08 NLS
# note), so every message is checked against every language's terms at
# once, regardless of which one it's actually written in. Covers the
# same four categories the original English-only list did (legal
# threat, fraud accusation, regulatory-body escalation, anger/
# complaint) across languages a B2B AR product is plausibly likely to
# see. Not exhaustive — a language not listed here still silently won't
# match, same failure mode as before, just narrower. Terms were chosen
# to avoid the more common false-positive readings of each word in
# ordinary business correspondence (e.g. "processo judicial" rather
# than bare "processo," which is everyday Portuguese for "process").
ESCALATION_KEYWORDS_BY_LANGUAGE = {
    "en": [
        "lawyer", "attorney", "sue", "lawsuit", "legal action",
        "better business bureau", "bbb", "fraud", "scam",
        "complaint", "furious", "outraged", "unacceptable",
    ],
    "es": [
        "abogado", "demanda judicial", "demandar", "acción legal",
        "fraude", "estafa", "queja", "reclamo",
        "furioso", "indignado", "inaceptable",
    ],
    "fr": [
        "avocat", "poursuites judiciaires", "poursuivre en justice", "action en justice",
        "fraude", "escroquerie", "réclamation", "plainte",
        "furieux", "indigné", "inacceptable",
    ],
    "de": [
        "anwalt", "rechtsanwalt", "klage", "verklagen", "rechtliche schritte",
        "betrug", "beschwerde", "wütend", "empört", "inakzeptabel",
    ],
    "pt": [
        "advogado", "processo judicial", "processar", "ação legal", "ação judicial",
        "fraude", "golpe", "reclamação", "queixa",
        "furioso", "indignado", "inaceitável",
    ],
    "it": [
        "avvocato", "causa legale", "citare in giudizio", "azione legale",
        "frode", "truffa", "reclamo", "furioso", "indignato", "inaccettabile",
    ],
}

ESCALATION_KEYWORDS = [
    keyword
    for keywords in ESCALATION_KEYWORDS_BY_LANGUAGE.values()
    for keyword in keywords
]

# "w9_tax_doc_request" and "general_question" have no downstream
# sub-agent to hand off to — CONTEXT.md doesn't name one for either —
# so they stay with Customer Inbox itself (a drafted reply, if
# classified, rather than a routing decision).
ROUTING_MAP = {
    "remittance_advice": "cash_application",
    "invoice_copy_request": "invoice_delivery",
    "dispute": "dispute_deduction",
    "payment_promise": "collections_dunning",
}


def needs_escalation(subject: str, body: str) -> bool:
    text = f"{subject} {body}".lower()
    return any(keyword in text for keyword in ESCALATION_KEYWORDS)


def classify_message(subject: str, body: str) -> str | None:
    """
    TODO: needs an LLM call. No classifier exists anywhere in this
    project — every inbound message is honestly "unclassifiable" until
    one is wired up, which correctly escalates it per CONTEXT.md's own
    wording ("escalates anything ... unclassifiable to a human").
    """
    return None


def route_classification(classification: str) -> str | None:
    """The sub-agent this classification would hand off to, or None if
    it has no downstream sub-agent."""
    return ROUTING_MAP.get(classification)


def draft_reply(classification: str, message: dict[str, Any]) -> str | None:
    """
    TODO: needs an LLM call ("drafts replies for routine requests" per
    CONTEXT.md). No drafting exists — None is honest. Even a real draft
    still couldn't auto-send: no tenant has a configured auto-send
    whitelist in this project's policy model, so every tenant is
    correctly still in CONTEXT.md's own "draft-only mode" default.
    """
    return None
