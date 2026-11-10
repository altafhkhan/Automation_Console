# Qumaxa AR Automation Connector — Install Guide

The AR Automation Console has two parts. The **Console** runs in the cloud
(console.qumaxa.com) and holds the AR agent, your policies and your
approval queue. The **Connector** is a small Windows Service installed where
your accounting system lives. This guide covers the Connector.

## Before you start

| Need | Details |
|---|---|
| A Windows PC or server | Windows 10 / Windows Server 2016 or later, 64-bit, always on. |
| Administrator rights | The installer creates a Windows Service. |
| Outbound HTTPS | Port 443 to `console.qumaxa.com`. No inbound ports are opened. |
| Your signup code | Emailed to you after you request access on qumaxa.com. |
| **Dynamics GP only:** ODBC driver | Microsoft ODBC Driver 17 or 18 for SQL Server, and a read-only SQL Server login for your GP company database. |
| **QuickBooks Online only:** nothing on this PC | Your administrator connects the QuickBooks company later from the Console portal. |
| Optional: mailbox details | IMAP/SMTP host, mailbox address and an app password, if you want invoice delivery, reminders and inbox monitoring. |

## Install (about 10 minutes)

1. Run `AR-Connector-Setup-<version>.exe` as administrator and read the
   notice. Files go to `C:\Program Files\Qumaxa\AR Connector`.
2. A console window opens and walks you through eight steps:
   - **Checks** — admin rights, Windows version, folder access, ODBC driver,
     and that the Console is reachable.
   - **Security notice** — we never store your accounting passwords.
   - **Signup code** — from your email.
   - **Accounting system** — choose QuickBooks Online or Dynamics GP.
     For GP, enter the SQL Server host, port, company database and login;
     the installer tests the connection and reads the GP version itself.
   - **Approval routing** — one approver email for everything (quick), or
     a per-action list. Changeable later in the Console.
   - **Email integration** — optional; skip and add later.
   - **Registration** — the Console issues this installation's tokens.
   - **Service** — `Qumaxa AR Automation Connector` is installed, set to
     start automatically, started, and the first heartbeat is confirmed.
3. Log in to the portal at `https://console.qumaxa.com/portal` with the
   invitation your administrator receives. QuickBooks Online users: open
   **Integrations → Connect...** and approve access on Intuit's page.

## Where things are

| Item | Location |
|---|---|
| Program | `C:\Program Files\Qumaxa\AR Connector` |
| Configuration and credentials | `C:\ProgramData\Qumaxa\ARConnector` (readable by Administrators and SYSTEM only; secrets encrypted with Windows DPAPI) |
| Logs | `C:\ProgramData\Qumaxa\ARConnector\logs\connector.log` |
| Windows Service | `QumaxaARConnector` — "Qumaxa AR Automation Connector" in services.msc |
| Start Menu | Qumaxa AR Connector → status / configure / install guide / uninstall |

## Day-to-day

- **Status** — Start Menu → *AR Connector status* (shows service state,
  last heartbeat, license status, last error). It asks for administrator
  approval, because the configuration folder is locked to administrators.
- **Change GP or mailbox settings** — Start Menu → *AR Connector configure*.
  The service restarts itself afterwards.
- **Change approvers or thresholds** — in the Console, not on this PC.
- **Dynamics GP and Windows authentication** — the service runs as
  LocalSystem. If you chose Windows Integrated authentication, either grant
  the computer account read access in SQL Server or change the service's
  "Log On" account in services.msc to a domain account that can read GP.
  A SQL Server login avoids this.

## Upgrade

Run the newer `AR-Connector-Setup-<version>.exe`. Files are replaced in
place, your registration and settings are kept, the service is restarted
and a heartbeat is confirmed. No signup code is needed again.

## Uninstall

Settings → Apps → *Qumaxa AR Automation Connector* → Uninstall (or the
Start Menu shortcut). The service is stopped and removed. You are asked
whether to delete the stored configuration and credentials too:

- **No** — keep them, so a reinstall picks up the same registration.
- **Yes** — everything under `C:\ProgramData\Qumaxa\ARConnector` is deleted.

Removing the Connector does not delete your data in the Console. To have
your tenant data deleted, email the address below.

## Linux (manual)

The same daemon runs under systemd. From a checkout of the connector files:

```
sudo useradd --system --home /var/lib/ar-connector ar-connector
sudo mkdir -p /opt/ar-connector /var/lib/ar-connector
sudo cp *.py /opt/ar-connector/
sudo pip3 install requests pyodbc
sudo -u ar-connector AR_CONNECTOR_HOME=/var/lib/ar-connector python3 /opt/ar-connector/installer/ar_connector_installer.py install
```

then install the unit shown by `installer_skeleton.py` with
`ExecStart=/usr/bin/python3 /opt/ar-connector/connector_daemon.py`,
`Environment=AR_CONNECTOR_HOME=/var/lib/ar-connector`, and
`systemctl enable --now ar-agent-connector`.

## Trouble?

Email **altaf.khan@qumaxa.com** with the output of *AR Connector status*
and the last lines of `connector.log`.
