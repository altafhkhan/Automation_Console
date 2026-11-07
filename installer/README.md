# installer/ — building the customer-facing Connector package

What a buyer receives: `AR-Connector-Setup-<version>.exe` (Inno Setup
wizard) or, as a fallback, `AR-Connector-<version>.zip`. Both contain the
same two programs built by PyInstaller from this repo:

| Program | Source | Role |
|---|---|---|
| `ARConnectorSetup.exe` | `ar_connector_installer.py` | install / configure / upgrade / status / check / uninstall / purge |
| `ARConnectorService.exe` | `ar_connector_service.py` | Windows Service host around `connector_daemon.run_forever()` |

Shared code at the repo root: `connector_daemon.py` (the loop),
`gp_client.py` (Dynamics GP via SQL Server), `installer_skeleton.py`
(notice + questionnaires, reused by the installer), `connector_paths.py`
(where state lives), `secure_store.py` (DPAPI at rest),
`connector_version.py` (the one version string).

## Build

```
powershell -ExecutionPolicy Bypass -File installer\build.ps1
```

Needs Python 3.12+ on PATH and, for the setup EXE, Inno Setup 6
(`winget install JRSoftware.InnoSetup`). The script creates
`installer\.venv` with the pinned packages in `requirements-connector.txt`,
runs PyInstaller with `ar_connector.spec`, compiles `ar_connector.iss`,
and writes `installer\dist\SHA256SUMS.txt`. Outputs are git-ignored.

Flags: `-SkipVenv` (reuse the venv), `-SkipInno` (zip only),
`-DistDir name` (alternate output folder, e.g. when a previous build's EXE
is still running and locks `dist\ARConnector`).

## Release checklist

1. Bump `CONNECTOR_VERSION` in `connector_version.py`.
2. Run the build; test `ARConnectorSetup.exe check` and `status` on a
   clean Windows VM, then the full wizard against a signup code from the
   live Console.
3. Hand the buyer the setup EXE, `INSTALL-GUIDE.md` and the SHA-256 line.
4. Code signing: the EXE is unsigned, so SmartScreen shows "unknown
   publisher" on first run. An OV/EV code-signing certificate for Qumaxa
   IT Services removes that; add `SignTool=` to `ar_connector.iss` once one
   exists.

## What the installer does on the customer's machine

- Files: `C:\Program Files\Qumaxa\AR Connector`
- State: `C:\ProgramData\Qumaxa\ARConnector` (ACL: Administrators +
  SYSTEM; secrets DPAPI-encrypted; `install.json`, `status.json`, `logs\`)
- Service: `QumaxaARConnector`, automatic start, restart on failure
- Registration: `POST /v1/registrations` exactly as `installer_skeleton.py`
  documents, with `connector_version` from `connector_version.py`
- Upgrade: re-run the newer setup EXE; configuration is kept
- Uninstall: Apps & features; asks whether to purge the state folder
