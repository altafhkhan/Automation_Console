"""
At-rest protection for the connector's local secrets (Console tokens,
mailbox password, SQL Server login). Closes the "plain JSON on disk is a
placeholder" TODO that installer_skeleton.py and connector_daemon.py
both carried.

Windows: the JSON is encrypted with DPAPI in LOCAL_MACHINE scope, so the
installer (run by an administrator) and the Windows Service (running as
LocalSystem or a service account) can both read it, but a copy of the
file taken to another machine is useless. The folder's ACL is tightened
separately by the installer (Administrators + SYSTEM only).

Linux/macOS: no DPAPI — the file is written with mode 0600 instead.

Files written before this module existed are plain JSON; read_json()
still reads them, and the next write_json() upgrades them in place.
"""

from __future__ import annotations

import base64
import json
import os
import platform
from pathlib import Path
from typing import Any

_PREFIX = "dpapi1:"
_ENTROPY = b"qumaxa-ar-connector-v1"
_CRYPTPROTECT_LOCAL_MACHINE = 0x4

try:  # pywin32 — present in the packaged installer, optional for dev runs
    import win32crypt  # type: ignore
except ImportError:  # pragma: no cover - non-Windows or pywin32 missing
    win32crypt = None


def _protect(raw: bytes) -> str:
    if platform.system() == "Windows" and win32crypt is not None:
        blob = win32crypt.CryptProtectData(raw, "AR Connector", _ENTROPY, None, None, _CRYPTPROTECT_LOCAL_MACHINE)
        return _PREFIX + base64.b64encode(blob).decode("ascii")
    return raw.decode("utf-8")


def _unprotect(text: str) -> bytes:
    if not text.startswith(_PREFIX):
        return text.encode("utf-8")
    if win32crypt is None:
        raise RuntimeError("This file was protected with Windows DPAPI and can only be read on the machine that wrote it.")
    blob = base64.b64decode(text[len(_PREFIX):])
    _description, raw = win32crypt.CryptUnprotectData(blob, _ENTROPY, None, None, 0)
    return raw


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = _protect(json.dumps(data).encode("utf-8"))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    if platform.system() != "Windows":
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def read_json(path: Path) -> Any:
    return json.loads(_unprotect(path.read_text(encoding="utf-8")).decode("utf-8"))


def is_protected(path: Path) -> bool:
    try:
        return path.read_text(encoding="utf-8").startswith(_PREFIX)
    except OSError:
        return False
