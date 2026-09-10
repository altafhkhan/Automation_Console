"""
Server-side config for the QuickBooks Online OAuth2 integration
(qbo_client.py, console_registration_api.py's /v1/qbo/* endpoints).

These are qumaxa's own app-level OAuth client credentials, registered
once with Intuit at developer.intuit.com — one app, shared across every
tenant who connects a QBO company, the same way a "Sign in with Google"
button uses one app Client ID regardless of which Google account signs
in. This is NOT a customer credential, so holding it centrally doesn't
violate this project's "never touch customer passwords" principle —
what IS customer-specific (each tenant's own access/refresh tokens) is
what actually lives in qbo_connections, one row per installation.

Unset until a real Intuit developer app exists — every QBO endpoint
that needs these fails closed with a clear error rather than silently
proceeding with empty strings.
"""

import os

QBO_CLIENT_ID = os.environ.get("QBO_CLIENT_ID", "")
QBO_CLIENT_SECRET = os.environ.get("QBO_CLIENT_SECRET", "")
QBO_REDIRECT_URI = os.environ.get("QBO_REDIRECT_URI", "https://console.qumaxa.com/v1/qbo/oauth/callback")
QBO_ENVIRONMENT = os.environ.get("QBO_ENVIRONMENT", "sandbox")  # 'sandbox' | 'production'

QBO_AUTHORIZATION_URL = "https://appcenter.intuit.com/connect/oauth2"
QBO_TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
QBO_SCOPE = "com.intuit.quickbooks.accounting"


def qbo_configured() -> bool:
    return bool(QBO_CLIENT_ID and QBO_CLIENT_SECRET)


def qbo_api_base(realm_id: str) -> str:
    host = "sandbox-quickbooks.api.intuit.com" if QBO_ENVIRONMENT == "sandbox" else "quickbooks.api.intuit.com"
    return f"https://{host}/v3/company/{realm_id}"
