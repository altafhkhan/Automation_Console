"""
Windows Service host for the AR Automation Connector.

Built into ARConnectorService.exe by installer/ar_connector.spec. The
installer (ar_connector_installer.py) registers it with

    ARConnectorService.exe --startup auto install
    ARConnectorService.exe start

and the uninstaller removes it with `stop` + `remove`. Running the EXE
with no arguments is what the Windows Service Control Manager does — it
hands control to connector_daemon.run_forever() with a stop event the
SCM can set.

Nothing ERP-specific lives here: the service is a thin host around the
exact same daemon loop a Linux systemd unit runs.
"""

from __future__ import annotations

import logging
import sys
import threading

import servicemanager
import win32event
import win32service
import win32serviceutil

import connector_daemon

SERVICE_NAME = "QumaxaARConnector"
SERVICE_DISPLAY_NAME = "Qumaxa AR Automation Connector"
SERVICE_DESCRIPTION = (
    "Background connector for the Qumaxa AR Automation Console. Sends heartbeats, "
    "syncs accounting data (Dynamics GP) and sends queued emails through your own "
    "mail server. All AI reasoning stays in the central Console."
)


class ARConnectorService(win32serviceutil.ServiceFramework):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = SERVICE_DISPLAY_NAME
    _svc_description_ = SERVICE_DESCRIPTION

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = threading.Event()
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)

    def SvcStop(self):  # noqa: N802 — pywin32 naming
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        self.stop_event.set()
        win32event.SetEvent(self.hWaitStop)

    def SvcDoRun(self):  # noqa: N802 — pywin32 naming
        connector_daemon.configure_file_logging()
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        try:
            connector_daemon.run_forever(self.stop_event)
        except FileNotFoundError as exc:
            # No credentials.json: the installer has not completed registration.
            logging.getLogger("connector_daemon").critical(str(exc))
            servicemanager.LogErrorMsg(f"{SERVICE_DISPLAY_NAME}: {exc}")
        except Exception as exc:  # noqa: BLE001 — surface anything else in the Event Log
            logging.getLogger("connector_daemon").exception("Connector stopped with an unexpected error")
            servicemanager.LogErrorMsg(f"{SERVICE_DISPLAY_NAME} stopped: {exc}")
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STOPPED,
            (self._svc_name_, ""),
        )


def main() -> None:
    if len(sys.argv) == 1:
        # Started by the Service Control Manager.
        try:
            servicemanager.Initialize()
            servicemanager.PrepareToHostSingle(ARConnectorService)
            servicemanager.StartServiceCtrlDispatcher()
        except Exception as exc:  # noqa: BLE001 — run by hand, not by the SCM
            print(f"{SERVICE_DISPLAY_NAME}: this program is started by Windows as a service.")
            print("Use ARConnectorSetup.exe to install, check or remove it. "
                  f"(Service control manager said: {exc})")
            sys.exit(1)
    else:
        # install / start / stop / remove / debug — pywin32's standard CLI.
        win32serviceutil.HandleCommandLine(ARConnectorService)


if __name__ == "__main__":
    main()
