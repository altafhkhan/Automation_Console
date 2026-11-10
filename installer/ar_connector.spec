# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the AR Automation Connector — builds two EXEs that
share one `_internal` runtime folder:

  ARConnectorSetup.exe    the installer / configurator / uninstaller CLI
  ARConnectorService.exe  the Windows Service host

Run via installer/build.ps1 (it creates the virtualenv and pins versions);
`pyinstaller ar_connector.spec` from this folder also works.
"""

import os
import sys

from PyInstaller.utils.hooks import collect_submodules

SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))
REPO_ROOT = os.path.dirname(SPEC_DIR)
sys.path.insert(0, REPO_ROOT)

from connector_version import CONNECTOR_VERSION  # noqa: E402

PATHEX = [REPO_ROOT, SPEC_DIR]
HIDDEN = [
    "win32timezone",  # pywin32 service framework needs this at runtime
    "pyodbc",
    "gp_client",
    "connector_daemon",
    "connector_paths",
    "connector_version",
    "secure_store",
    "console_config",
    "installer_skeleton",
] + collect_submodules("requests")

setup_a = Analysis(
    [os.path.join(SPEC_DIR, "ar_connector_installer.py")],
    pathex=PATHEX,
    hiddenimports=HIDDEN,
    datas=[(os.path.join(SPEC_DIR, "INSTALL-GUIDE.md"), ".")],
    noarchive=False,
)
service_a = Analysis(
    [os.path.join(SPEC_DIR, "ar_connector_service.py")],
    pathex=PATHEX,
    hiddenimports=HIDDEN,
    noarchive=False,
)

MERGE((setup_a, "ARConnectorSetup", "ARConnectorSetup"), (service_a, "ARConnectorService", "ARConnectorService"))

setup_pyz = PYZ(setup_a.pure)
setup_exe = EXE(
    setup_pyz,
    setup_a.scripts,
    [],
    exclude_binaries=True,
    name="ARConnectorSetup",
    console=True,
    uac_admin=False,  # elevates itself per command (see ar_connector_installer.main)
    icon=None,
)

service_pyz = PYZ(service_a.pure)
service_exe = EXE(
    service_pyz,
    service_a.scripts,
    [],
    exclude_binaries=True,
    name="ARConnectorService",
    console=True,
    icon=None,
)

COLLECT(
    setup_exe,
    setup_a.binaries,
    setup_a.datas,
    service_exe,
    service_a.binaries,
    service_a.datas,
    name="ARConnector",
)
