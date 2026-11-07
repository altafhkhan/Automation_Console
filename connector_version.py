"""
Single source of truth for the customer-side connector version.

Read by connector_daemon.py (sent on every heartbeat), the installer
(sent at registration, written to install.json) and the build script
(stamped into the setup EXE's file name and version resource). Bump it
here and nowhere else — the Console's version matrix keys off this
string (installation-registration-design.md, Section 4).
"""

CONNECTOR_VERSION = "1.0.0"
CONNECTOR_MODULE_PREFIX = "ar-connector"
