"""
AR Automation Connector — customer-facing installer / configurator.

Built into ARConnectorSetup.exe (installer/ar_connector.spec) and shipped
inside the Inno Setup package (installer/ar_connector.iss). The setup
wizard copies the files, then runs this program; it is also what the
Start Menu "AR Connector status" shortcut and the uninstaller call.

Commands
  setup        what the setup wizard runs: first install -> `install`,
               already configured -> `upgrade`.
  install      interactive first-run: prerequisite checks, security
               notice, signup code, accounting system, approval routing,
               optional email, registration with the Console, Windows
               Service install, first heartbeat check.
  configure    re-run the configuration questions (GP connection, email,
               approval routing) without re-registering.
  upgrade      keep configuration, re-register the service binary from
               the new files, restart it, verify a heartbeat.
  status       show installation, service and last-heartbeat state.
  check        run the prerequisite checks only.
  uninstall    stop + remove the Windows Service (keeps configuration).
  purge        delete the configuration folder (credentials, GP/email
               settings, logs). Used by the uninstaller when asked.

The flow follows installation-registration-design.md Section 1 and
reuses installer_skeleton.py's notice and questionnaire text so the two
cannot drift.
"""

from __future__ import annotations

import ctypes
import getpass
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# Both the frozen EXE (sys._MEIPASS/_internal) and `python installer/...`
# from a checkout must find the repo-root modules.
_HERE = Path(__file__).resolve().parent
for candidate in (_HERE, _HERE.parent):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from console_config import CONSOLE_BASE_URL  # noqa: E402
from connector_paths import (  # noqa: E402
    CONFIG_DIR,
    EMAIL_CONFIG_PATH,
    GP_CONNECTION_CONFIG_PATH,
    INSTALL_INFO_PATH,
    LOG_DIR,
    STATUS_PATH,
    TOKEN_STORE_PATH,
)
from connector_version import CONNECTOR_MODULE_PREFIX, CONNECTOR_VERSION  # noqa: E402
from secure_store import read_json, write_json  # noqa: E402
import installer_skeleton as skeleton  # noqa: E402

SERVICE_NAME = "QumaxaARConnector"
PORTAL_URL = f"{CONSOLE_BASE_URL}/portal"
SUPPORT_EMAIL = "altaf.khan@qumaxa.com"

ERP_CHOICES = [
    ("QuickBooks Online", "qbo"),
    ("Microsoft Dynamics GP", "gp"),
    ("Other / not listed", "other"),
]


# ---------------------------------------------------------------------------
# Small console helpers
# ---------------------------------------------------------------------------
def banner(title: str) -> None:
    print()
    print("=" * 70)
    print(f" {title}")
    print("=" * 70)


def step(n: int, title: str) -> None:
    print(f"\n--- Step {n}: {title} ---")


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or (default or "")


def ask_yes_no(prompt: str, default_yes: bool = False) -> bool:
    hint = "[Y/n]" if default_yes else "[y/N]"
    value = input(f"{prompt} {hint}: ").strip().lower()
    if not value:
        return default_yes
    return value in ("y", "yes")


def fail(message: str, code: int = 1) -> None:
    print(f"\nERROR: {message}")
    print(f"Need help? Email {SUPPORT_EMAIL} with the text above and the log at {LOG_DIR}.")
    pause_if_interactive()
    sys.exit(code)


def pause_if_interactive() -> None:
    # The setup wizard launches this in its own console window; keep it
    # open so the customer can read the result.
    if os.environ.get("AR_CONNECTOR_NO_PAUSE"):
        return
    try:
        input("\nPress Enter to close this window... ")
    except EOFError:
        pass


def frozen_dir() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else _HERE


def service_exe() -> Path:
    return frozen_dir() / "ARConnectorService.exe"


# ---------------------------------------------------------------------------
# Prerequisite checks
# ---------------------------------------------------------------------------
def is_admin() -> bool:
    if platform.system() != "Windows":
        return os.geteuid() == 0  # type: ignore[attr-defined]
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def windows_version_ok() -> tuple[bool, str]:
    if platform.system() != "Windows":
        return False, f"{platform.system()} (this package is for Windows; Linux uses the systemd unit in INSTALL-GUIDE.md)"
    release = platform.release()
    build = int(platform.version().split(".")[-1]) if platform.version().count(".") == 2 else 0
    ok = release in ("10", "11") or build >= 14393  # Windows 10 1607 / Server 2016 and later
    return ok, f"Windows {release} (build {build})"


def odbc_driver_installed() -> tuple[bool, str]:
    if platform.system() != "Windows":
        return True, "skipped (non-Windows)"
    try:
        import winreg

        for name in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"):
            try:
                winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\ODBC\ODBCINST.INI\{name}").Close()
                return True, name
            except OSError:
                continue
    except ImportError:
        pass
    return False, "not found (needed only for Microsoft Dynamics GP)"


def console_reachable() -> tuple[bool, str]:
    try:
        resp = requests.get(f"{CONSOLE_BASE_URL}/portal/", timeout=15)
        return resp.status_code < 500, f"{CONSOLE_BASE_URL} answered HTTP {resp.status_code}"
    except requests.RequestException as exc:
        return False, f"{CONSOLE_BASE_URL} not reachable: {exc.__class__.__name__}"


def config_dir_writable() -> tuple[bool, str]:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        probe = CONFIG_DIR / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        return True, str(CONFIG_DIR)
    except OSError as exc:
        return False, f"{CONFIG_DIR}: {exc}"


def run_checks(require_console: bool = True, need_gp: bool = False) -> bool:
    checks = [
        ("Administrator rights", (is_admin(), "yes" if is_admin() else "no — right-click the installer and choose 'Run as administrator'"), True),
        ("Windows version", windows_version_ok(), True),
        ("Configuration folder", config_dir_writable(), True),
        ("ODBC Driver for SQL Server", odbc_driver_installed(), need_gp),
        ("Console reachable (HTTPS)", console_reachable(), require_console),
    ]
    all_ok = True
    for label, (ok, detail), required in checks:
        mark = "OK " if ok else ("FAIL" if required else "warn")
        print(f"  [{mark}] {label}: {detail}")
        if required and not ok:
            all_ok = False
    return all_ok


# ---------------------------------------------------------------------------
# Accounting system
# ---------------------------------------------------------------------------
def choose_erp() -> tuple[str, str]:
    print("Which accounting system does this connector serve?")
    for i, (label, _) in enumerate(ERP_CHOICES, 1):
        print(f"  [{i}] {label}")
    while True:
        choice = ask("Choose", "1")
        if choice.isdigit() and 1 <= int(choice) <= len(ERP_CHOICES):
            return ERP_CHOICES[int(choice) - 1]
        print("Please enter a number from the list.")


def detect_gp_version(gp_config: dict) -> str | None:
    """
    Reads the GP release from the DYNAMICS system database
    (DU000020, PRODID 0 = Microsoft Dynamics GP). Same SQL Server login
    the customer just gave us, read-only. Returns None if anything is
    off — the caller then asks instead of blocking the install.
    """
    try:
        import pyodbc

        import gp_client

        system_config = dict(gp_config, database="DYNAMICS")
        with pyodbc.connect(gp_client.build_connection_string(system_config), timeout=10) as conn:
            row = conn.execute(
                "select top 1 VERSIONMAJOR, VERSIONMINOR, VERSIONBUILD from DU000020 where PRODID = 0"
            ).fetchone()
        if row:
            return f"{row[0]}.{row[1]}.{row[2]}"
    except Exception:  # noqa: BLE001 — detection is best-effort by design
        return None
    return None


def test_gp_connection(gp_config: dict) -> tuple[bool, str]:
    try:
        import pyodbc

        import gp_client

        with pyodbc.connect(gp_client.build_connection_string(gp_config), timeout=10) as conn:
            count = conn.execute("select count(*) from RM00101").fetchone()[0]
        return True, f"connected — {count} customer records visible in {gp_config['database']}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc).splitlines()[0][:200]


def collect_gp_with_test() -> dict | None:
    while True:
        gp_config = skeleton.collect_gp_connection_config()
        if gp_config is None:
            return None
        if gp_config.get("windows_auth"):
            print(
                "\nNote: the connector runs as a Windows Service. With Windows Integrated\n"
                "authentication, the service account (LocalSystem by default) must be allowed\n"
                "to read the GP database. A SQL Server login avoids that extra step."
            )
        print("Testing the connection...")
        ok, detail = test_gp_connection(gp_config)
        print(f"  {'OK' if ok else 'FAILED'}: {detail}")
        if ok:
            return gp_config
        if not ask_yes_no("Try again with different details?", default_yes=True):
            if ask_yes_no("Save these details anyway and fix them later with 'configure'?"):
                return gp_config
            return None


def collect_system(erp_key: str, erp_label: str) -> tuple[skeleton.DetectedSystem, dict | None]:
    if erp_key == "qbo":
        print(
            "\nQuickBooks Online is a cloud service, so nothing on this machine talks to it.\n"
            "After installation, your account administrator connects your QuickBooks company\n"
            f"from the Console portal ({PORTAL_URL}, Integrations tab, 'Connect...') — Intuit's\n"
            "own sign-in page asks for your consent; no password ever passes through us."
        )
        return skeleton.DetectedSystem("QuickBooks Online", "cloud", "auto_detected"), None

    if erp_key == "gp":
        gp_config = collect_gp_with_test()
        version = detect_gp_version(gp_config) if gp_config else None
        if version:
            system = skeleton.confirm(skeleton.DetectedSystem("Microsoft Dynamics GP", version, "auto_detected"))
        else:
            print("\nCould not read the GP version automatically.")
            version_text = ask("Dynamics GP version (e.g. 18.6.1745) — Enter if unsure", "")
            if version_text:
                system = skeleton.DetectedSystem("Microsoft Dynamics GP", version_text, "manual_entry")
            else:
                major = ask("General version (e.g. 18 or 2018)", "18")
                system = skeleton.DetectedSystem("Microsoft Dynamics GP", f"{major}.x", "manual_entry", precision="major_only")
        return system, gp_config

    return skeleton.ask_manually(), None


# ---------------------------------------------------------------------------
# Approval routing — quick path or the full questionnaire
# ---------------------------------------------------------------------------
def collect_policy() -> dict:
    print(
        "\nApproval routing decides who signs off on risky actions (credit memos,\n"
        "write-offs, credit holds, first collections outreach, ...). You can change\n"
        "it later from the Console."
    )
    if ask_yes_no("Set one approver for everything now, and refine later?", default_yes=True):
        approver = skeleton._ask_approver("all gated actions")  # noqa: SLF001 — shared validation
        actions = [
            "dunning_activation", "final_demand_letter", "cash_application_match", "unidentified_cash",
            "credit_memo", "write_off", "credit_limit_change", "credit_hold", "credit_release", "customer_inbox_reply",
        ]
        return {"version": 1, "approval_rules": [{"action": a, "approver": approver, "threshold": None} for a in actions]}
    return skeleton.collect_policy_config()


# ---------------------------------------------------------------------------
# Windows Service management
# ---------------------------------------------------------------------------
def _run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=check)


def service_state() -> str:
    if platform.system() != "Windows":
        return "n/a"
    result = _run(["sc", "query", SERVICE_NAME], check=False)
    if result.returncode != 0:
        return "not installed"
    for line in result.stdout.splitlines():
        if "STATE" in line:
            return line.split(":", 1)[1].strip().split(" ", 1)[-1].strip().lower()
    return "unknown"


def install_service() -> None:
    exe = service_exe()
    if not exe.exists():
        fail(f"Service program not found at {exe}. Re-run the setup package.")
    state = service_state()
    if state == "not installed":
        _run([str(exe), "--startup", "auto", "install"])
    else:
        if state != "stopped":
            _run([str(exe), "stop"], check=False)
            time.sleep(2)
        _run([str(exe), "update", "--startup", "auto"], check=False)
    _run(["sc", "failure", SERVICE_NAME, "reset=", "86400", "actions=", "restart/60000/restart/60000/restart/60000"], check=False)
    _run([str(exe), "start"])
    print(f"  Windows Service '{SERVICE_NAME}' installed and started.")


def remove_service() -> None:
    exe = service_exe()
    if service_state() == "not installed":
        print("  Service was not installed.")
        return
    if exe.exists():
        _run([str(exe), "stop"], check=False)
        time.sleep(2)
        _run([str(exe), "remove"], check=False)
    else:
        _run(["sc", "stop", SERVICE_NAME], check=False)
        time.sleep(2)
        _run(["sc", "delete", SERVICE_NAME], check=False)
    print(f"  Windows Service '{SERVICE_NAME}' removed.")


def tighten_folder_acl() -> None:
    """Only Administrators and SYSTEM can read the configuration folder
    (secrets are also DPAPI-encrypted; this is the second layer)."""
    if platform.system() != "Windows":
        return
    _run(["icacls", str(CONFIG_DIR), "/inheritance:r",
          "/grant:r", "*S-1-5-32-544:(OI)(CI)F", "/grant:r", "*S-1-5-18:(OI)(CI)F"], check=False)


# ---------------------------------------------------------------------------
# First-run verification
# ---------------------------------------------------------------------------
def wait_for_heartbeat(timeout_seconds: int = 45) -> dict | None:
    deadline = time.time() + timeout_seconds
    started = datetime.now(timezone.utc)
    while time.time() < deadline:
        if STATUS_PATH.exists():
            try:
                status = json.loads(STATUS_PATH.read_text())
                attempted = status.get("last_attempt_at")
                if attempted and datetime.fromisoformat(attempted) >= started:
                    return status
            except (OSError, ValueError):
                pass
        time.sleep(2)
    return None


def report_first_run() -> None:
    print("Waiting for the connector's first contact with the Console...")
    status = wait_for_heartbeat()
    if status is None:
        print("  The service is running but has not reported yet. Check again later with 'AR Connector status'.")
        return
    if status.get("last_heartbeat_at"):
        print(f"  Connected. License status: {status.get('license_status')}.")
    else:
        print(f"  First contact failed: {status.get('last_error')}. The service keeps retrying every few minutes.")


# ---------------------------------------------------------------------------
# install.json
# ---------------------------------------------------------------------------
def write_install_info(**fields) -> None:
    info = {}
    if INSTALL_INFO_PATH.exists():
        try:
            info = json.loads(INSTALL_INFO_PATH.read_text())
        except (OSError, ValueError):
            info = {}
    info.update(fields)
    info["connector_version"] = CONNECTOR_VERSION
    info["updated_at"] = datetime.now(timezone.utc).isoformat()
    INSTALL_INFO_PATH.parent.mkdir(parents=True, exist_ok=True)
    INSTALL_INFO_PATH.write_text(json.dumps(info, indent=2))


def read_install_info() -> dict:
    try:
        return json.loads(INSTALL_INFO_PATH.read_text())
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_check() -> None:
    banner(f"AR Automation Connector {CONNECTOR_VERSION} — prerequisite check")
    ok = run_checks(require_console=True, need_gp=False)
    print("\nAll required checks passed." if ok else "\nSome required checks failed — see above.")


def cmd_install() -> None:
    banner(f"AR Automation Connector {CONNECTOR_VERSION} — installation")
    print(f"Console: {CONSOLE_BASE_URL}")

    step(1, "Checking this machine")
    if not run_checks(require_console=True):
        fail("Fix the failed checks and run the installer again.")

    step(2, "Security and data-use notice")
    skeleton.show_security_notice()

    step(3, "Your signup code")
    print("You received a signup code by email after requesting access on qumaxa.com.")
    signup_code = ask("Signup code")
    if not signup_code:
        fail("A signup code is required. Email us if you do not have one.")

    step(4, "Accounting system")
    erp_label, erp_key = choose_erp()
    if erp_key == "gp":
        ok, detail = odbc_driver_installed()
        if not ok:
            fail("Microsoft ODBC Driver 17 or 18 for SQL Server is required for Dynamics GP. "
                 "Install it from Microsoft's site, then run the installer again.")
    system, gp_config = collect_system(erp_key, erp_label)

    step(5, "Approval routing")
    policy_config = collect_policy()

    step(6, "Email integration (optional)")
    email_config = skeleton.collect_email_config()

    step(7, "Registering with the Console")
    try:
        registration = skeleton.register(signup_code, system, policy_config)
    except requests.HTTPError as exc:
        detail = exc.response.text[:300] if exc.response is not None else str(exc)
        fail(f"The Console rejected the registration: {detail}\n"
             "Most common cause: a signup code that was already used or mistyped.")
    except requests.RequestException as exc:
        fail(f"Could not reach the Console: {exc}")
    skeleton.store_token(registration)
    skeleton.store_email_config(email_config)
    skeleton.store_gp_connection_config(gp_config)
    tighten_folder_acl()
    write_install_info(
        installed_at=datetime.now(timezone.utc).isoformat(),
        installation_id=registration.get("installation_id"),
        erp_type=system.erp_type,
        erp_version=system.erp_version,
        connector_module=f"{CONNECTOR_MODULE_PREFIX}-{erp_key}",
        install_dir=str(frozen_dir()),
    )
    print(f"  Registered. Installation id: {registration.get('installation_id')}")

    step(8, "Background service")
    install_service()
    report_first_run()

    banner("Installation complete")
    print(f"  Your trial has started. Manage approvals at {PORTAL_URL}")
    if erp_key == "qbo":
        print("  Next: connect your QuickBooks company from the portal's Integrations tab.")
    if email_config is None:
        print("  Email integration was skipped — run 'AR Connector configure' later to add it.")
    print(f"  Logs: {LOG_DIR}")
    pause_if_interactive()


def cmd_configure() -> None:
    banner("AR Automation Connector — configuration")
    if not TOKEN_STORE_PATH.exists():
        fail("This machine is not registered yet. Run the installer first.")
    info = read_install_info()
    changed = False
    if info.get("erp_type", "").lower().strip() in skeleton.GP_ERP_TYPE_ALIASES or ask_yes_no("Configure a Dynamics GP database connection?"):
        gp_config = collect_gp_with_test()
        if gp_config:
            skeleton.store_gp_connection_config(gp_config)
            changed = True
    if ask_yes_no("Configure email integration (IMAP/SMTP)?"):
        email_config = skeleton.collect_email_config()
        if email_config:
            skeleton.store_email_config(email_config)
            changed = True
    if ask_yes_no("Change approval routing? (also editable from the Console)"):
        policy = collect_policy()
        (CONFIG_DIR / "policy_config.pending.json").write_text(json.dumps(policy, indent=2))
        print("  Saved locally. Approval routing is applied by the Console — email us the file above,\n"
              "  or edit the rules in the Console admin. (Self-service policy updates are a roadmap item.)")
    tighten_folder_acl()
    if changed and service_state() == "running":
        print("Restarting the service to pick up the new settings...")
        _run([str(service_exe()), "restart"], check=False)
    pause_if_interactive()


def cmd_upgrade() -> None:
    banner(f"AR Automation Connector — upgrade to {CONNECTOR_VERSION}")
    if not TOKEN_STORE_PATH.exists():
        print("No existing configuration found — running a fresh installation instead.")
        cmd_install()
        return
    previous = read_install_info().get("connector_version", "unknown")
    print(f"  Previous version: {previous}")
    if not run_checks(require_console=False):
        fail("Fix the failed checks and run the upgrade again.")
    install_service()
    write_install_info(upgraded_from=previous, install_dir=str(frozen_dir()))
    report_first_run()
    print("\nUpgrade complete. Your configuration and credentials were kept.")
    pause_if_interactive()


def cmd_status() -> None:
    banner("AR Automation Connector — status")
    info = read_install_info()
    print(f"  Connector version : {CONNECTOR_VERSION} (installed: {info.get('connector_version', 'not installed')})")
    print(f"  Installed at      : {info.get('installed_at', '—')}")
    print(f"  Accounting system : {info.get('erp_type', '—')} {info.get('erp_version', '')}")
    print(f"  Installation id   : {info.get('installation_id', '—')}")
    print(f"  Console           : {CONSOLE_BASE_URL}")
    print(f"  Windows Service   : {service_state()}")
    print(f"  Registered        : {'yes' if TOKEN_STORE_PATH.exists() else 'no'}")
    print(f"  GP connection     : {'configured' if GP_CONNECTION_CONFIG_PATH.exists() else 'not configured'}")
    print(f"  Email integration : {'configured' if EMAIL_CONFIG_PATH.exists() else 'not configured'}")
    if STATUS_PATH.exists():
        try:
            status = json.loads(STATUS_PATH.read_text())
            print(f"  Last heartbeat    : {status.get('last_heartbeat_at') or 'never'}")
            print(f"  License status    : {status.get('license_status') or '—'}")
            if status.get("last_error"):
                print(f"  Last error        : {status['last_error']}")
        except (OSError, ValueError):
            pass
    print(f"  Configuration dir : {CONFIG_DIR}")
    print(f"  Logs              : {LOG_DIR}")
    pause_if_interactive()


def cmd_uninstall() -> None:
    banner("AR Automation Connector — uninstall")
    remove_service()
    print(f"  Configuration kept at {CONFIG_DIR} (run 'purge' to delete it).")


def cmd_purge() -> None:
    if CONFIG_DIR.exists():
        shutil.rmtree(CONFIG_DIR, ignore_errors=True)
        print(f"  Deleted {CONFIG_DIR} (credentials, GP/email settings, logs).")
    else:
        print("  Nothing to delete.")


def cmd_setup() -> None:
    if TOKEN_STORE_PATH.exists():
        cmd_upgrade()
    else:
        cmd_install()


NEEDS_ADMIN = {"setup", "install", "configure", "upgrade", "uninstall", "purge"}

COMMANDS = {
    "setup": cmd_setup,
    "install": cmd_install,
    "configure": cmd_configure,
    "upgrade": cmd_upgrade,
    "status": cmd_status,
    "check": cmd_check,
    "uninstall": cmd_uninstall,
    "purge": cmd_purge,
}


def main() -> None:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to a legacy code page
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    command = sys.argv[1].lower() if len(sys.argv) > 1 else "setup"
    if command in ("-h", "--help", "help") or command not in COMMANDS:
        print(__doc__)
        sys.exit(0 if command in ("-h", "--help", "help") else 2)
    if command in NEEDS_ADMIN and platform.system() == "Windows" and not is_admin():
        # Re-launch ourselves elevated (UAC prompt) rather than failing
        # half-way through — service control and the ProgramData ACL need it.
        print(f"'{command}' needs administrator rights — asking Windows for elevation...")
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, " ".join(sys.argv[1:]), str(frozen_dir()), 1)
        if rc <= 32:
            fail("Elevation was refused. Right-click the program and choose 'Run as administrator'.")
        sys.exit(0)
    try:
        COMMANDS[command]()
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)


if __name__ == "__main__":
    main()
