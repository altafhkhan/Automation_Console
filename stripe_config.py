"""
Server-side config for the Stripe subscription billing integration
(console_registration_api.py's /v1/portal/*/billing/* and /v1/billing/webhook).

Uses qumaxa's existing Stripe account (already live for a different
qumaxa product — see D:\\Projects\\ocrdocflow\\backend\\app\\api\\v1\\billing.py
for the reference pattern this follows: Stripe Checkout, so raw card
details never touch this server, plus webhook-signature verification
as the source of truth for what actually happened rather than the
browser's success redirect).

STRIPE_PRICE_ID is the single flat monthly subscription Price, created
once in the Stripe Dashboard (test mode while developing) — one plan
for now, unlike ocrdocflow's tiered starter/professional/business
lineup, since that's what was asked for here.

Unset until real keys are put in deploy/.env — every billing endpoint
that needs these fails closed with a clear error rather than silently
proceeding with empty strings.
"""

import os

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "")

# How many days a payment buys, in both directions: the webhook pushes
# demo_end out by this much on activation/renewal, and this is the same
# demo_end field _refresh_license_status()/run_license_watcher() already
# check — a Stripe-paid installation and a trial installation share one
# entitlement mechanism, not two.
RENEWAL_TERM_DAYS = 30


def stripe_configured() -> bool:
    return bool(STRIPE_SECRET_KEY and STRIPE_PRICE_ID)
