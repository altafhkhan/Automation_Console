"""
Customer-site connector daemon — scaffold, not production-ready.

This is the always-on background service that `installer_skeleton.py`
registers as a systemd unit / Windows Service (see its
`install_background_service()` and the ExecStart path in
`_install_systemd_service()`). It is the piece CONTEXT.md and
installation-registration-design.md describe as the thin, ERP-specific
process that runs at the customer's site while all AI reasoning (Master
Agent, AR Agent, sub-agents, policy engine) stays in the Console.

Responsibilities (design doc Section 1 + 6 + 9):
  - Heartbeat loop: POST /v1/heartbeat every HEARTBEAT_INTERVAL_MINUTES.
    This is also the mechanism that detects a license renewal or a plan
    change — no reinstall needed, the next heartbeat response picks it up.
  - Silent token refresh: proactively refresh the access token (1h TTL)
    via /v1/token/refresh before it expires, using the refresh token
    (30d TTL, rotated on every use). store_token() in installer_skeleton.py
    explicitly hands this responsibility to this daemon, not the installer.
  - Hard stop on expiry: when entitlements.connector_should_run is False,
    every sub-agent sync/ERP-push cycle is skipped entirely — no polling,
    no syncing, no partial/degraded mode. The daemon keeps heartbeating
    (that's how it notices a renewal) but does nothing else until
    connector_should_run flips back to True.
  - Pluggable sync per sub-agent: actual ERP-specific push/pull logic
    (Oracle EBS, QuickBooks Desktop, ...) is not implemented here. Each
    sub-agent's sync worker is registered in SUB_AGENT_SYNCERS, mirroring
    the detect_erp() pluggable-detector pattern in installer_skeleton.py —
    add one function per sub-agent/adapter without touching the loop.
  - Error reporting: any exception raised by a sync worker is caught and
    reported to POST /v1/errors rather than crashing the daemon; the
    server fills in the version fingerprint from the token, per design
    doc Section 4.

Run (matches the systemd ExecStart in installer_skeleton.py):
  python3 connector_daemon.py
"""

from __future__ import annotations

import email
import imaplib
import json
import logging
import signal
import smtplib
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable

import requests  # pip install requests

from console_config import CONSOLE_BASE_URL

try:
    import gp_client  # pip install pyodbc -- only actually imported/used if GP_CONNECTION_CONFIG_PATH exists
except ImportError:
    gp_client = None  # honest: no pyodbc installed means no GP connection was ever configured either

TOKEN_STORE_PATH = Path.home() / ".ar_agent" / "credentials.json"  # TODO: OS secret store, see installer_skeleton.py

# Deliberately a SEPARATE file from TOKEN_STORE_PATH, not a second key in
# the same JSON: Credentials.save() below only knows about the four auth
# fields and rewrites the whole file on every token refresh — merging
# email config into that file risks it being silently dropped on the
# next refresh. TODO: OS secret store for this too, same gap as the auth
# tokens — plain JSON on disk is a placeholder, not acceptable at rest.
EMAIL_CONFIG_PATH = Path.home() / ".ar_agent" / "email_config.json"

# Same reasoning as EMAIL_CONFIG_PATH, same file installer_skeleton.py's
# store_gp_connection_config() writes to. Its mere presence/absence is
# what gates GP-specific behavior below — no separate erp_type flag
# needed, matching how EMAIL_CONFIG_PATH's presence alone already gates
# email sync. Added 2026-09-10 alongside gp_client.py — see that
# module's docstring for what's real GP schema knowledge here vs.
# genuinely unverified without a live database.
GP_CONNECTION_CONFIG_PATH = Path.home() / ".ar_agent" / "gp_connection_config.json"


def load_gp_connection_config() -> dict[str, Any] | None:
    """None means "not a GP installation, or GP install that skipped
    this step" — the honest default connector_daemon.py already uses
    for load_email_config() below, applied the same way here."""
    if not GP_CONNECTION_CONFIG_PATH.exists():
        return None
    return json.loads(GP_CONNECTION_CONFIG_PATH.read_text())


def load_email_config() -> dict[str, Any] | None:
    """
    None means "no email integration configured for this installation" —
    a real, honest state for any installation that ran the installer
    before collect_email_config() existed, or whose customer skipped it.
    Callers must treat this as "nothing to do," not an error.
    """
    if not EMAIL_CONFIG_PATH.exists():
        return None
    return json.loads(EMAIL_CONFIG_PATH.read_text())


def _wipe_local_credentials() -> None:
    """
    Called when the Console tells us, definitively, that this
    installation no longer exists (ReregistrationRequired) — most
    notably after a tenant data-deletion request (see
    console_registration_api.py's admin_delete_tenant()). Removes the
    local token and email-credential files rather than leaving them on
    disk indefinitely: the daemon can't do anything useful with them
    either way once the installation is gone, and one of those files
    holds a mailbox password in plaintext.
    """
    for path in (TOKEN_STORE_PATH, EMAIL_CONFIG_PATH):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.exception("Failed to remove local credential file %s", path)


HEARTBEAT_INTERVAL_SECONDS = 5 * 60  # design doc: "every N minutes" — 5 is a placeholder, make configurable
REFRESH_SAFETY_MARGIN_SECONDS = 5 * 60  # refresh this long before the access token actually expires
REQUEST_TIMEOUT_SECONDS = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("connector_daemon")


# ---------------------------------------------------------------------------
# Credential store
# ---------------------------------------------------------------------------
@dataclass
class Credentials:
    installation_id: str
    access_token: str
    refresh_token: str
    access_token_expires_at: datetime

    @classmethod
    def load(cls) -> "Credentials":
        if not TOKEN_STORE_PATH.exists():
            raise FileNotFoundError(
                f"No credentials at {TOKEN_STORE_PATH} — run the installer first."
            )
        data = json.loads(TOKEN_STORE_PATH.read_text())
        return cls(
            installation_id=data["installation_id"],
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            access_token_expires_at=datetime.fromisoformat(data["access_token_expires_at"]),
        )

    def save(self) -> None:
        # TODO: same placeholder as installer_skeleton.py — replace with the
        # OS secret store (Windows DPAPI/Credential Manager, Linux `keyring`)
        # before shipping. Plain JSON on disk is not acceptable for a real
        # credential at rest.
        TOKEN_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_STORE_PATH.write_text(json.dumps({
            "installation_id": self.installation_id,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "access_token_expires_at": self.access_token_expires_at.isoformat(),
        }))

    def needs_refresh(self) -> bool:
        now = datetime.now(timezone.utc)
        margin = timedelta(seconds=REFRESH_SAFETY_MARGIN_SECONDS)
        return now >= self.access_token_expires_at.replace(tzinfo=timezone.utc) - margin


class ReregistrationRequired(Exception):
    """Raised when the refresh token itself is invalid/expired — a human must re-run the installer."""


def refresh_credentials(creds: Credentials) -> Credentials:
    resp = requests.post(
        f"{CONSOLE_BASE_URL}/v1/token/refresh",
        json={"refresh_token": creds.refresh_token},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if resp.status_code == 401:
        raise ReregistrationRequired("Refresh token rejected — installation must re-register")
    resp.raise_for_status()
    body = resp.json()

    refreshed = Credentials(
        installation_id=creds.installation_id,
        access_token=body["access_token"],
        refresh_token=body["refresh_token"],
        access_token_expires_at=datetime.fromisoformat(body["access_token_expires_at"]),
    )
    refreshed.save()
    log.info("Access token refreshed, expires at %s", refreshed.access_token_expires_at)
    return refreshed


def _auth_headers(creds: Credentials) -> dict[str, str]:
    return {"Authorization": f"Bearer {creds.access_token}"}


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------
def send_heartbeat(creds: Credentials, connector_version: str, metrics: dict[str, Any]) -> dict[str, Any]:
    resp = requests.post(
        f"{CONSOLE_BASE_URL}/v1/heartbeat",
        json={"connector_version": connector_version, "status": "running", "metrics": metrics},
        headers=_auth_headers(creds),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if resp.status_code == 401:
        # Access token expired between our proactive check and this call —
        # fall back to a reactive refresh-and-retry rather than waiting for
        # the next loop iteration.
        raise TokenExpiredMidRequest()
    resp.raise_for_status()
    return resp.json()


class TokenExpiredMidRequest(Exception):
    pass


def report_error(creds: Credentials, sub_agent: str, error_code: str, error_detail: str = "") -> None:
    try:
        requests.post(
            f"{CONSOLE_BASE_URL}/v1/errors",
            json={"sub_agent": sub_agent, "error_code": error_code, "error_detail": error_detail},
            headers=_auth_headers(creds),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        # Best-effort — losing an error report must never crash the daemon
        # or block the next heartbeat.
        log.exception("Failed to report error for sub_agent=%s error_code=%s", sub_agent, error_code)


# ---------------------------------------------------------------------------
# Sub-agent sync — pluggable per adapter, mirrors detect_erp() in
# installer_skeleton.py. Each real ERP adapter registers its sync function
# in SUB_AGENT_SYNCERS below without touching run_sync_cycle() or the main
# loop.
# ---------------------------------------------------------------------------
SyncWorker = Callable[[Credentials, dict[str, Any]], None]


GP_SYNC_STATE_PATH = Path.home() / ".ar_agent" / "gp_sync_state.json"


def _load_gp_sync_state() -> dict[str, Any]:
    if not GP_SYNC_STATE_PATH.exists():
        return {}
    return json.loads(GP_SYNC_STATE_PATH.read_text())


def _save_gp_sync_state(state: dict[str, Any]) -> None:
    GP_SYNC_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    GP_SYNC_STATE_PATH.write_text(json.dumps(state))


def _pull_bank_feed_entries() -> list[dict[str, Any]]:
    """
    TODO: real implementation queries this installation's ERP/bank feed —
    e.g. Oracle EBS's AP/AR interface tables, or QuickBooks Desktop's
    QBXML — whichever detect_erp() identified in installer_skeleton.py.
    No adapter exists yet for either, so this still deliberately returns
    nothing for them rather than fabricating data.

    Dynamics GP (added 2026-09-10, gp_client.py — built to spec, NOT
    verified against a live database, see that module's docstring)
    dispatches here instead: gated purely by whether
    load_gp_connection_config() found a config file at all, same
    pattern EMAIL_CONFIG_PATH's presence already uses to gate email
    sync. `since` tracks the last successfully-synced payment's
    GLPOSTDT locally (_load_gp_sync_state()) so the same real payment
    application doesn't get pushed — and turned into a duplicate
    ar_tasks/audit_log entry — every single cycle forever; advanced
    only after a successful push below, in sync_cash_application().
    """
    gp_config = load_gp_connection_config()
    if not gp_config or gp_client is None:
        return []

    since = _load_gp_sync_state().get("last_synced_payment_date")
    connection_string = gp_client.build_connection_string(gp_config)
    return gp_client.fetch_payments(connection_string, since=since)


def sync_cash_application(creds: Credentials, agent_versions: dict[str, Any]) -> None:
    entries = _pull_bank_feed_entries()
    if not entries:
        return  # nothing to push this cycle

    resp = requests.post(
        f"{CONSOLE_BASE_URL}/v1/sync/cash-application",
        json={"bank_feed_entries": entries},
        headers=_auth_headers(creds),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()

    # Advance the GP high-water mark only after the push actually
    # succeeded (see _pull_bank_feed_entries()'s docstring) — a failed
    # push must be retried next cycle with the same `since`, not
    # silently skipped.
    dates = [e["received_date"] for e in entries if e.get("received_date")]
    if dates:
        _save_gp_sync_state({"last_synced_payment_date": max(dates)})


def _pull_new_invoices() -> list[dict[str, Any]]:
    """
    TODO: real implementation queries this installation's ERP for newly
    posted invoices — same gap as _pull_bank_feed_entries() above. No ERP
    adapter exists yet, so this returns nothing rather than fabricating
    invoice data.
    """
    return []


def sync_invoice_delivery(creds: Credentials, agent_versions: dict[str, Any]) -> None:
    invoices = _pull_new_invoices()
    if not invoices:
        return  # nothing to push this cycle

    resp = requests.post(
        f"{CONSOLE_BASE_URL}/v1/sync/invoice-delivery",
        json={"invoices": invoices},
        headers=_auth_headers(creds),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()


def _decode_mime_words(value: str) -> str:
    return "".join(
        part.decode(enc or "utf-8", errors="replace") if isinstance(part, bytes) else part
        for part, enc in decode_header(value)
    )


def _extract_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                payload = part.get_payload(decode=True) or b""
                return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        return ""
    payload = msg.get_payload(decode=True) or b""
    return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")


def _pull_inbox_messages() -> list[dict[str, Any]]:
    """
    Real IMAP implementation — reads unseen messages from this
    installation's own AR mailbox, per the customer's own credentials in
    EMAIL_CONFIG_PATH (never sent to the Console — ARCHITECTURE.md:
    "Never request customer passwords"). None from load_email_config()
    means no installation has configured this yet, which is the honest
    state for every real installation so far in this project — returns
    [] rather than erroring, same as every other "no integration
    configured yet" gap.
    """
    config = load_email_config()
    if not config:
        return []

    imap_conf = config["imap"]
    messages: list[dict[str, Any]] = []

    if imap_conf.get("use_ssl", True):
        conn = imaplib.IMAP4_SSL(imap_conf["host"], imap_conf.get("port", 993))
    else:
        conn = imaplib.IMAP4(imap_conf["host"], imap_conf.get("port", 143))

    try:
        conn.login(imap_conf["username"], imap_conf["password"])
        conn.select(imap_conf.get("folder", "INBOX"))
        status, data = conn.search(None, "UNSEEN")
        if status != "OK":
            return []

        for num in data[0].split():
            status, msg_data = conn.fetch(num, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            messages.append({
                "subject": _decode_mime_words(msg.get("Subject", "")),
                "body": _extract_body(msg),
                "sender": msg.get("From", ""),
            })
            # Mark seen so the next cycle doesn't re-pull it — the
            # connector's own dedupe, separate from
            # customer_inbox_events.processed_at on the Console side.
            conn.store(num, "+FLAGS", "\\Seen")
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 — best-effort cleanup, login may not have gotten this far
            pass
        conn.logout()

    return messages


def sync_customer_inbox(creds: Credentials, agent_versions: dict[str, Any]) -> None:
    messages = _pull_inbox_messages()
    if not messages:
        return  # nothing to push this cycle

    resp = requests.post(
        f"{CONSOLE_BASE_URL}/v1/sync/customer-inbox",
        json={"messages": messages},
        headers=_auth_headers(creds),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()


def _pull_credit_limits() -> list[dict[str, Any]]:
    """
    TODO: real implementation queries this installation's ERP for
    customer credit limits — same gap as _pull_bank_feed_entries() above,
    for Oracle EBS/QuickBooks Desktop specifically (no adapter for
    either exists yet).

    Dynamics GP dispatches to gp_client.fetch_credit_limits() instead —
    same load_gp_connection_config() gate as _pull_bank_feed_entries()
    above. No high-water-mark needed here (unlike cash application):
    /v1/sync/credit-limits upserts, so re-sending the same current
    limit every cycle is harmless, not duplicative.
    """
    gp_config = load_gp_connection_config()
    if not gp_config or gp_client is None:
        return []
    return gp_client.fetch_credit_limits(gp_client.build_connection_string(gp_config))


def sync_credit_limits(creds: Credentials, agent_versions: dict[str, Any]) -> None:
    limits = _pull_credit_limits()
    if not limits:
        return  # nothing to push this cycle

    resp = requests.post(
        f"{CONSOLE_BASE_URL}/v1/sync/credit-limits",
        json={"credit_limits": limits},
        headers=_auth_headers(creds),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()


def _pull_gp_open_invoices() -> list[dict[str, Any]]:
    """
    No non-GP equivalent exists here at all (added 2026-09-10) — QBO's
    server pulls invoices directly from Intuit's API, so no connector-
    push path for open invoices ever existed before GP needed one. Full
    snapshot every cycle, matching /v1/sync/open-invoices' full-replace
    semantics — no high-water-mark needed since it's not an append-only
    log like cash_application_events.
    """
    gp_config = load_gp_connection_config()
    if not gp_config or gp_client is None:
        return []
    return gp_client.fetch_open_invoices(gp_client.build_connection_string(gp_config))


def sync_open_invoices(creds: Credentials, agent_versions: dict[str, Any]) -> None:
    if load_gp_connection_config() is None:
        return  # not a GP installation (or one that skipped this step) -- nothing to push

    # Always push from here on, even when empty — unlike the other
    # syncers in this file, an empty list is meaningful (every invoice
    # got paid off) and must still replace whatever's on the server,
    # not be silently skipped the way "nothing new" is elsewhere.
    invoices = _pull_gp_open_invoices()
    resp = requests.post(
        f"{CONSOLE_BASE_URL}/v1/sync/open-invoices",
        json={"open_invoices": invoices},
        headers=_auth_headers(creds),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()


def _pull_gp_customers() -> list[dict[str, Any]]:
    """Same "no non-GP equivalent" note as _pull_gp_open_invoices()."""
    gp_config = load_gp_connection_config()
    if not gp_config or gp_client is None:
        return []
    return gp_client.fetch_customers(gp_client.build_connection_string(gp_config))


def sync_customers_local(creds: Credentials, agent_versions: dict[str, Any]) -> None:
    customers = _pull_gp_customers()
    if not customers:
        return  # nothing to push this cycle (upsert, not full-replace -- safe to skip when empty)

    resp = requests.post(
        f"{CONSOLE_BASE_URL}/v1/sync/customers",
        json={"customers": customers},
        headers=_auth_headers(creds),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()


SUB_AGENT_SYNCERS: dict[str, SyncWorker] = {
    "cash_application": sync_cash_application,
    "invoice_delivery": sync_invoice_delivery,
    "customer_inbox": sync_customer_inbox,
    "credit_risk": sync_credit_limits,
    "open_invoices": sync_open_invoices,
    "customers": sync_customers_local,
    # ... one entry per sub-agent adapter, added as they're built.
}


def _send_via_smtp(smtp_conf: dict[str, Any], to_address: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = smtp_conf.get("from_address", smtp_conf["username"])
    msg["To"] = to_address
    msg["Subject"] = subject
    msg.set_content(body)

    port = smtp_conf.get("port", 587)
    server = (
        smtplib.SMTP_SSL(smtp_conf["host"], port, timeout=REQUEST_TIMEOUT_SECONDS)
        if smtp_conf.get("use_ssl")
        else smtplib.SMTP(smtp_conf["host"], port, timeout=REQUEST_TIMEOUT_SECONDS)
    )
    try:
        if smtp_conf.get("use_tls", True) and not smtp_conf.get("use_ssl"):
            server.starttls()
        server.login(smtp_conf["username"], smtp_conf["password"])
        server.send_message(msg)
    finally:
        server.quit()


def sync_outbound_email(creds: Credentials, agent_versions: dict[str, Any]) -> None:
    """
    The other half of _queue_outbound_email() in ar_agent.py — pulls
    whatever the Console has queued for this installation and sends it
    via the customer's own SMTP server, never the Console's. Not in
    SUB_AGENT_SYNCERS: its shape (pull from Console, act locally, report
    back per-item) is fundamentally a different direction from the other
    syncers (pull locally, push to Console once), so run_sync_cycle()
    calls it as its own guarded step below.
    """
    config = load_email_config()
    if not config:
        return  # no email integration configured for this installation

    resp = requests.get(
        f"{CONSOLE_BASE_URL}/v1/outbox/pending",
        headers=_auth_headers(creds),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()

    for item in resp.json():
        try:
            _send_via_smtp(config["smtp"], item["to_address"], item["subject"], item["body"])
            status, detail = "sent", ""
        except Exception as exc:  # noqa: BLE001 — one failed send must not block the rest of the outbox
            log.exception("Failed to send outbound email %s", item["email_id"])
            status, detail = "failed", str(exc)

        ack = requests.post(
            f"{CONSOLE_BASE_URL}/v1/outbox/{item['email_id']}/ack",
            json={"status": status, "detail": detail},
            headers=_auth_headers(creds),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        ack.raise_for_status()


def run_sync_cycle(creds: Credentials, heartbeat_response: dict[str, Any]) -> None:
    agent_versions = heartbeat_response.get("agent_versions", {})
    for sub_agent, syncer in SUB_AGENT_SYNCERS.items():
        try:
            syncer(creds, agent_versions)
        except Exception as exc:  # noqa: BLE001 — a single adapter failure must not take down the daemon
            log.exception("Sync worker failed for sub_agent=%s", sub_agent)
            report_error(creds, sub_agent=sub_agent, error_code="sync_failed", error_detail=str(exc))

    try:
        sync_outbound_email(creds, agent_versions)
    except Exception as exc:  # noqa: BLE001 — same isolation as the loop above
        log.exception("Outbound email sync failed")
        report_error(creds, sub_agent="outbound_email", error_code="sync_failed", error_detail=str(exc))


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
CONNECTOR_VERSION = "v0.1.0"  # TODO: pull from package metadata, must match installer_skeleton.py's registration


def run_forever(stop_event: threading.Event) -> None:
    creds = Credentials.load()
    log.info("Connector daemon started for installation_id=%s", creds.installation_id)

    while not stop_event.is_set():
        try:
            if creds.needs_refresh():
                creds = refresh_credentials(creds)

            try:
                heartbeat_response = send_heartbeat(creds, CONNECTOR_VERSION, metrics={})
            except TokenExpiredMidRequest:
                creds = refresh_credentials(creds)
                heartbeat_response = send_heartbeat(creds, CONNECTOR_VERSION, metrics={})

            entitlements = heartbeat_response.get("entitlements", {})
            if entitlements.get("connector_should_run", True):
                run_sync_cycle(creds, heartbeat_response)
            else:
                # Hard stop per design doc Section 6 — no polling, no syncing,
                # no sub-agent activity of any kind. Heartbeat keeps running
                # so the daemon notices the moment license_status changes back.
                log.warning(
                    "License status is '%s' — connector halted, skipping sync cycle",
                    entitlements.get("license_status", "unknown"),
                )

        except ReregistrationRequired:
            log.critical("Refresh token invalid or expired — installation must be re-registered. Stopping.")
            _wipe_local_credentials()
            return
        except requests.RequestException:
            # Transient network/Console issue — log and retry next interval
            # rather than crashing the service.
            log.exception("Heartbeat cycle failed, will retry next interval")

        stop_event.wait(HEARTBEAT_INTERVAL_SECONDS)


def main() -> None:
    stop_event = threading.Event()

    def _handle_stop_signal(signum, frame):  # noqa: ANN001 — signal handler signature is fixed
        log.info("Received signal %s, shutting down gracefully", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_stop_signal)
    signal.signal(signal.SIGINT, _handle_stop_signal)

    try:
        run_forever(stop_event)
    except FileNotFoundError as exc:
        log.critical(str(exc))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
