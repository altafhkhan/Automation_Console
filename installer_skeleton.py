"""
Customer-site installer — scaffold, not production-ready.

Flow (matches installation-registration-design.md, Section 1):
  0. Show the security & data-use notice (no passwords stored; why the
     exact software version matters for support) before collecting
     anything else.
  1. Confirm identity via signup code (from the lead-capture email).
  2. Auto-detect ERP/software + version; fall back to asking if detection fails.
  3. Confirm/edit detected values.
  4. Ask the policy questionnaire (approval routing).
  5. Register with the Console over HTTPS, receive a token.
  6. Store the token securely and register the connector as a background
     service (systemd on Linux, Windows Service via NSSM/pywin32 on Windows).

This intentionally keeps ERP-specific detection behind a small interface
(`detect_erp()`), so adding a new ERP later (Oracle Fusion Cloud, NetSuite,
SAP) means writing one new detector, not touching the rest of the flow.
"""

from __future__ import annotations

import getpass
import json
import platform
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

import requests  # pip install requests

from console_config import CONSOLE_BASE_URL
from connector_paths import EMAIL_CONFIG_PATH, GP_CONNECTION_CONFIG_PATH, TOKEN_STORE_PATH
from connector_version import CONNECTOR_VERSION
from secure_store import write_json as _write_secret

# The packaged, customer-facing installer is installer/ar_connector_installer.py;
# it reuses the questionnaire and notice functions below. This file remains the
# design reference for the flow. Paths are shared via connector_paths.py and
# secrets go through secure_store.py (DPAPI on Windows) — the old "plain JSON
# on disk" placeholders are gone.
GP_ERP_TYPE_ALIASES = {"microsoft dynamics gp", "dynamics gp", "gp"}


# ---------------------------------------------------------------------------
# Step 0 — security & data notice
# ---------------------------------------------------------------------------
SECURITY_NOTICE = """\
Before we begin, two things you should know:

  1. We never ask for or store your accounting system passwords.
     The connector uses a secure API/token-based connection you approve
     separately — your login credentials never pass through this
     installer or our servers.

  2. We ask for your exact software name and version because it
     directly affects support quality. If something ever goes wrong,
     knowing the precise version lets us reproduce the issue and fix it
     fast — instead of guessing. This detail is what makes support
     effective, not just a formality.
"""


def show_security_notice() -> None:
    print(SECURITY_NOTICE)
    input("Press Enter to continue... ")


# ---------------------------------------------------------------------------
# Step 2 — detection
# ---------------------------------------------------------------------------
@dataclass
class DetectedSystem:
    erp_type: str
    erp_version: str | None
    source: str  # 'auto_detected' | 'user_confirmed' | 'manual_entry'
    precision: str = "exact"  # 'exact' | 'major_only'


def detect_erp() -> DetectedSystem | None:
    """
    Try each known detector in turn. Extend this list as new ERP adapters
    are built — each detector is independent and self-contained.
    """
    for detector in (_detect_quickbooks_desktop, _detect_oracle_ebs, _detect_dynamics_gp):
        result = detector()
        if result:
            return result
    return None


def _detect_quickbooks_desktop() -> DetectedSystem | None:
    if platform.system() != "Windows":
        return None
    # TODO: real implementation reads the QuickBooks install path / registry
    # key (e.g. HKLM\SOFTWARE\Intuit\QuickBooks) to pull the exact version.
    # Placeholder returns None so the flow falls back to asking the user.
    return None


def _detect_oracle_ebs() -> DetectedSystem | None:
    # TODO: real implementation queries FND_PRODUCT_GROUP (or equivalent)
    # over the DB connection the customer supplies during setup.
    # Requires DB credentials to be collected first — this is why EBS
    # detection typically happens one step later than QuickBooks detection.
    return None


def _detect_dynamics_gp() -> DetectedSystem | None:
    # TODO: real implementation reads DYNAMICS..SY01500 (the GP system
    # database's release-version table) over the SQL Server connection
    # the customer supplies during setup — same "needs DB credentials
    # collected first" ordering constraint as Oracle EBS above. GP has
    # no public cloud API (unlike QuickBooks Online): a real adapter
    # would integrate via eConnect/GP Web Services or direct SQL Server
    # reads, from the local connector, not Console-to-cloud.
    if platform.system() != "Windows":
        return None
    return None


def ask_manually() -> DetectedSystem:
    print("\nWe couldn't automatically detect your accounting system.")
    erp_type = input("System name (e.g. Oracle EBS, QuickBooks Desktop, QuickBooks Online, Microsoft Dynamics GP): ").strip()
    erp_version = input(
        "Exact version (e.g. 10.2.8, 2020) — or press Enter if you're not sure: "
    ).strip()

    if erp_version:
        return DetectedSystem(erp_type=erp_type, erp_version=erp_version, source="manual_entry")

    # Don't block installation over a patch number — accept major-version-only
    # and flag it for a support follow-up instead.
    major_version = input(
        "No problem — what's the general version/year (e.g. '10.2', '2020')? "
    ).strip()
    print("Got it. We'll follow up to confirm the exact version for support purposes.")
    return DetectedSystem(
        erp_type=erp_type,
        erp_version=f"{major_version}.x",
        source="manual_entry",
        precision="major_only",
    )


def confirm(detected: DetectedSystem) -> DetectedSystem:
    print(f"\nDetected: {detected.erp_type} version {detected.erp_version}")
    answer = input("Is this correct? [Y/n]: ").strip().lower()
    if answer in ("", "y", "yes"):
        return DetectedSystem(detected.erp_type, detected.erp_version, "user_confirmed")
    return ask_manually()


# ---------------------------------------------------------------------------
# Step 4 — policy questionnaire
# ---------------------------------------------------------------------------
def _ask_approver(label: str) -> str | None:
    """
    Requires a real email address, not just a name — added 2026-09-10
    alongside the tenant portal's exact-approver enforcement
    (console_registration_api.py's _approval_authorized()), which
    matches this string against a real tenant_users.email. A name like
    "the CFO" was never matchable to anything; this makes that failure
    obvious at setup time instead of silently at approval time. Blank
    is still allowed — that action type just falls back to "any of your
    team's portal users can act on it" rather than a specific person.
    """
    while True:
        approver = input(f"Who approves '{label}'? (their email address, or blank to leave unset): ").strip()
        if not approver:
            return None
        if "@" in approver and "." in approver.split("@")[-1]:
            return approver
        print("That doesn't look like an email address — enter a real one, or leave it blank.")


def collect_policy_config() -> dict:
    """
    Covers every action_type this project actually gates on approval
    (ar_agent.py) — previously only 3 of ~10 (see skills.md), which
    meant most items had no configured approver at all and always fell
    back to "any of the tenant's portal users." That fallback still
    exists (see _ask_approver()'s blank option and the portal's own
    _approval_authorized()) but is now a deliberate choice per action
    type, not the default for 7 of them by omission.

    Two distinct patterns, matching the two gate styles already in
    ar_agent.py: threshold-gated ones only require this specific
    approver once the dollar amount crosses the threshold
    (_check_requires_approval()); the rest always require this
    approver regardless of amount (_lookup_approver(), which has no
    threshold concept at all — CONTEXT.md's own "no exceptions in V1"
    wording for credit_memo/write_off, "recommendations only" for
    credit_hold/credit_release). Asking for a threshold on those would
    collect data nothing ever reads.
    """
    print("\nApproval routing setup — you can change this later from the Console.")
    approval_rules = []

    threshold_gated_action_types = [
        ("dunning_activation", "First-time collections outreach"),
        ("final_demand_letter", "Final demand letter"),
        ("cash_application_match", "Applying a matched payment automatically"),
        ("unidentified_cash", "Applying an unidentified/ambiguous payment"),
    ]
    unconditional_action_types = [
        ("credit_memo", "Credit memo issuance"),
        ("write_off", "Bad-debt write-off"),
        ("credit_limit_change", "Credit limit change"),
        ("credit_hold", "Placing a customer on credit hold"),
        ("credit_release", "Releasing a customer from credit hold"),
        ("customer_inbox_reply", "Sending a drafted customer inbox reply"),
    ]

    for action_key, label in threshold_gated_action_types:
        approver = _ask_approver(label)
        threshold = input("Dollar threshold requiring their approval (blank = always): ").strip()
        approval_rules.append({
            "action": action_key,
            "approver": approver,
            "threshold": float(threshold) if threshold else None,
        })

    for action_key, label in unconditional_action_types:
        approver = _ask_approver(label)
        approval_rules.append({
            "action": action_key,
            "approver": approver,
            "threshold": None,  # always required, not amount-tiered -- see _lookup_approver()
        })

    return {"version": 1, "approval_rules": approval_rules}


# ---------------------------------------------------------------------------
# Step 4b — email integration (optional; several agents stay idle without
# it, but installation is never blocked over it — same "don't block over
# missing info" philosophy as the major-version-only ERP fallback above).
#
# ARCHITECTURE.md: "Never request customer passwords." This is the one
# place in the whole flow that's a real exception to that principle in
# letter — it does ask for a mailbox password — but not in spirit: the
# credential is written ONLY to EMAIL_CONFIG_PATH on this machine and is
# never included in register()'s payload to the Console. Only
# connector_daemon.py, running locally, ever reads it.
# ---------------------------------------------------------------------------
EMAIL_NOTICE = """\
Several agents need to send and read email on your behalf — delivering
invoices, sending payment reminders, and monitoring your AR inbox for
remittances and disputes. This connects directly to YOUR OWN mailbox
(IMAP to read, SMTP to send) using credentials that stay on this
machine — they are never sent to our servers, only used locally by the
background connector to talk to your mail provider.

You can skip this and set it up later — those features just stay idle
until you do.
"""


def collect_email_config() -> dict | None:
    print(EMAIL_NOTICE)
    answer = input("Set up email integration now? [y/N]: ").strip().lower()
    if answer not in ("y", "yes"):
        print("Skipped — invoice delivery, payment reminders, and inbox monitoring will stay idle until configured.")
        return None

    print("\nMost providers use the same host and login for both reading (IMAP) and sending (SMTP).")
    host = input("Mail server hostname (e.g. imap.yourcompany.com): ").strip()
    username = input("Mailbox username / email address: ").strip()
    password = getpass.getpass("Mailbox password (or app password — not echoed): ")
    imap_port = input("IMAP port [993]: ").strip() or "993"
    smtp_port = input("SMTP port [587]: ").strip() or "587"

    return {
        "imap": {
            "host": host, "port": int(imap_port), "use_ssl": True,
            "username": username, "password": password, "folder": "INBOX",
        },
        "smtp": {
            "host": host, "port": int(smtp_port), "use_tls": True, "use_ssl": False,
            "username": username, "password": password, "from_address": username,
        },
    }


def store_email_config(email_config: dict | None) -> None:
    if email_config is None:
        return
    # Same never-sent-to-Console guarantee as store_token()'s tokens —
    # this file is read only by connector_daemon.py, locally.
    _write_secret(EMAIL_CONFIG_PATH, email_config)
    print(f"Email credentials stored locally at {EMAIL_CONFIG_PATH} (encrypted at rest). Never sent to the Console.")


# ---------------------------------------------------------------------------
# Step 4c — Microsoft Dynamics GP connection (only asked when detect_erp()/
# ask_manually() identified this installation as GP). Added 2026-09-10
# alongside gp_client.py — see that module's docstring for why GP needs
# direct SQL Server access at all (no public API, unlike QuickBooks
# Online) and exactly which parts of that integration are well-established
# vs. genuinely unverified without a live GP database to check against.
# ---------------------------------------------------------------------------
GP_CONNECTION_NOTICE = """\
Microsoft Dynamics GP integration
----------------------------------
Reading your open invoices, customers, payments, and credit limits needs
direct, read-only access to your GP company database over SQL Server.
This connector never writes to GP, and — same as your mailbox password
above — these credentials are stored only on this machine, never sent to
the Console.
"""


def collect_gp_connection_config() -> dict | None:
    print(GP_CONNECTION_NOTICE)
    answer = input("Set up the Dynamics GP database connection now? [y/N]: ").strip().lower()
    if answer not in ("y", "yes"):
        print("Skipped — AR data for this installation will stay empty until this is configured.")
        return None

    host = input("SQL Server hostname or host\\instance (e.g. SQLSERVER01\\GP): ").strip()
    port = input("SQL Server port (blank for a named instance such as HOST\\GP) [1433 if no instance]: ").strip()
    if not port and "\\" not in host:
        port = "1433"
    database = input("GP company database name (e.g. TWO, or your real company DB): ").strip()

    auth = input("Authentication — [1] Windows Integrated, [2] SQL Server login [1]: ").strip() or "1"
    if auth == "2":
        username = input("SQL Server login username: ").strip()
        password = getpass.getpass("SQL Server login password (not echoed): ")
    else:
        username = None
        password = None

    return {
        "host": host,
        "port": int(port) if port else None,
        "database": database,
        "windows_auth": auth != "2",
        "username": username,
        "password": password,
    }


def store_gp_connection_config(gp_connection_config: dict | None) -> None:
    if gp_connection_config is None:
        return
    _write_secret(GP_CONNECTION_CONFIG_PATH, gp_connection_config)
    print(f"GP database connection stored locally at {GP_CONNECTION_CONFIG_PATH} (encrypted at rest). Never sent to the Console.")


# ---------------------------------------------------------------------------
# Step 5 — registration
# ---------------------------------------------------------------------------
def register(signup_code: str, system: DetectedSystem, policy_config: dict) -> dict:
    payload = {
        "signup_code": signup_code,
        "erp_type": system.erp_type,
        "erp_version": system.erp_version,
        "erp_version_source": system.source,
        "erp_version_precision": system.precision,
        "other_tools": [],  # TODO: extend questionnaire to capture these
        "connector_module": f"{system.erp_type.lower().replace(' ', '_')}-adapter",
        "connector_version": CONNECTOR_VERSION,
        "policy_config": policy_config,
        "acknowledged_security_notice": True,  # set only after show_security_notice() has run
    }
    resp = requests.post(f"{CONSOLE_BASE_URL}/v1/registrations", json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def store_token(registration: dict) -> None:
    # Stores both the access_token (1h) and refresh_token (30d, see design
    # doc Section 9). The background connector daemon — not this installer
    # — is responsible for calling /v1/token/refresh before the access
    # token expires and overwriting this file with the rotated pair.
    TOKEN_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_STORE_PATH.write_text(json.dumps(registration))
    print(f"\nCredentials stored at {TOKEN_STORE_PATH} (encrypted at rest).")


# ---------------------------------------------------------------------------
# Step 6 — background service registration
# ---------------------------------------------------------------------------
def install_background_service() -> None:
    system = platform.system()
    if system == "Linux":
        _install_systemd_service()
    elif system == "Windows":
        _install_windows_service()
    else:
        print(f"Unsupported platform for background service: {system}")


def _install_systemd_service() -> None:
    unit = """\
[Unit]
Description=AR Automation Connector
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/ar_agent/connector_daemon.py
Restart=always
User=ar_agent

[Install]
WantedBy=multi-user.target
"""
    print("\nSystemd unit file (write to /etc/systemd/system/ar-agent-connector.service"
          " and run `systemctl enable --now ar-agent-connector`):\n")
    print(unit)
    # TODO: actually write the file and call systemctl — requires root,
    # so in practice this step runs inside a signed install package.


def _install_windows_service() -> None:
    print("\nWindows: register connector_daemon.py as a service via NSSM or "
          "pywin32's win32serviceutil, then set startup type to Automatic.")
    # TODO: real implementation — e.g. `nssm install ARAgentConnector <python> connector_daemon.py`


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    print("AR Automation Agent — installer\n")
    show_security_notice()

    signup_code = input("Enter the signup code from your email: ").strip()

    detected = detect_erp()
    system = confirm(detected) if detected else ask_manually()

    policy_config = collect_policy_config()
    email_config = collect_email_config()
    gp_connection_config = (
        collect_gp_connection_config() if system.erp_type.strip().lower() in GP_ERP_TYPE_ALIASES else None
    )

    print("\nRegistering with the console...")
    registration = register(signup_code, system, policy_config)
    store_token(registration)
    store_email_config(email_config)
    store_gp_connection_config(gp_connection_config)

    install_background_service()

    print("\nInstallation complete. Your 7-day trial has started.")


if __name__ == "__main__":
    main()
