r"""
Where the customer-side connector keeps its state. One module, imported
by connector_daemon.py, installer_skeleton.py and installer/*, so the
installer and the background service can never disagree about where the
credentials live.

Resolution order:
  1. AR_CONNECTOR_HOME environment variable (tests, unusual layouts).
  2. The legacy per-user folder ~/.ar_agent, if it already exists and the
     machine-wide folder does not (installs made with installer_skeleton.py
     before the packaged installer existed keep working untouched).
  3. The machine-wide folder:
       Windows  %ProgramData%\Qumaxa\ARConnector
       Linux    /var/lib/ar-connector
     A Windows Service runs as LocalSystem (or a service account), whose
     "home" is not the installing user's — a machine-wide folder is the
     only path both the installer and the service see the same way.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path


def _resolve_config_dir() -> Path:
    override = os.environ.get("AR_CONNECTOR_HOME")
    if override:
        return Path(override)

    if platform.system() == "Windows":
        machine_wide = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Qumaxa" / "ARConnector"
    else:
        machine_wide = Path("/var/lib/ar-connector")

    legacy = Path.home() / ".ar_agent"
    if legacy.exists() and not machine_wide.exists():
        return legacy
    return machine_wide


CONFIG_DIR: Path = _resolve_config_dir()

TOKEN_STORE_PATH = CONFIG_DIR / "credentials.json"
EMAIL_CONFIG_PATH = CONFIG_DIR / "email_config.json"
GP_CONNECTION_CONFIG_PATH = CONFIG_DIR / "gp_connection_config.json"
GP_SYNC_STATE_PATH = CONFIG_DIR / "gp_sync_state.json"

# Written by the installer (what was installed, when, which ERP) and
# updated by the daemon after every heartbeat (last contact, license
# status) — this is what `ARConnectorSetup status` reads, and what the
# installer polls for its first-run check.
INSTALL_INFO_PATH = CONFIG_DIR / "install.json"
STATUS_PATH = CONFIG_DIR / "status.json"

LOG_DIR = CONFIG_DIR / "logs"
