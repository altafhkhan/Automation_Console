"""
Shared client-side config for installer_skeleton.py and connector_daemon.py.

Single source of truth for CONSOLE_BASE_URL so the installer and the
background daemon can never drift apart (e.g. one still pointing at
staging after the other was updated for prod). Override via the
CONSOLE_BASE_URL environment variable per environment — no code edit
needed to point a build at staging vs. production.
"""

import os

CONSOLE_BASE_URL = os.environ.get("CONSOLE_BASE_URL", "https://console.qumaxa.com")
