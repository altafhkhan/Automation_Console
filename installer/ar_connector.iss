; Inno Setup script for the Qumaxa AR Automation Connector.
; Compiled by installer/build.ps1 after PyInstaller has produced
; installer/dist/ARConnector/. Produces installer/dist/AR-Connector-Setup-<version>.exe
;
; What the wizard does:
;   1. Copies ARConnectorSetup.exe + ARConnectorService.exe + runtime to
;      C:\Program Files\Qumaxa\AR Connector  (upgrades replace in place).
;   2. Runs ARConnectorSetup.exe setup in a console window — first install
;      asks the questions and registers; an upgrade keeps the configuration,
;      re-registers the Windows Service and restarts it.
;   3. Adds Start Menu shortcuts for status / configure / the install guide.
;   4. On uninstall stops and removes the Windows Service, then asks whether
;      to delete the stored configuration and credentials as well.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef DistDir
  #define DistDir "dist"
#endif
#define AppName "Qumaxa AR Automation Connector"
#define AppPublisher "Qumaxa IT Services"
#define AppURL "https://qumaxa.com/products/ar-automation-console"
#define ServiceName "QumaxaARConnector"

[Setup]
AppId={{8F3C1D2E-6A4B-4F0D-9C1E-2B7A5D4E6F10}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\Qumaxa\AR Connector
DefaultGroupName=Qumaxa AR Connector
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#DistDir}
OutputBaseFilename=AR-Connector-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
LicenseFile=LICENSE-NOTICE.txt
UninstallDisplayName={#AppName}
CloseApplications=no
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#DistDir}\ARConnector\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "INSTALL-GUIDE.md"; DestDir: "{app}"; DestName: "INSTALL-GUIDE.txt"; Flags: ignoreversion

[Icons]
Name: "{group}\AR Connector status"; Filename: "{app}\ARConnectorSetup.exe"; Parameters: "status"; WorkingDir: "{app}"
Name: "{group}\AR Connector configure"; Filename: "{app}\ARConnectorSetup.exe"; Parameters: "configure"; WorkingDir: "{app}"
Name: "{group}\AR Connector install guide"; Filename: "{app}\INSTALL-GUIDE.txt"
Name: "{group}\Uninstall AR Connector"; Filename: "{uninstallexe}"

[Run]
; Stop a running service before files are replaced on upgrade is handled by
; the setup program itself (it stops/updates/starts the service).
Filename: "{app}\ARConnectorSetup.exe"; Parameters: "setup"; WorkingDir: "{app}"; StatusMsg: "Configuring the connector..."; Flags: waituntilterminated

[UninstallRun]
Filename: "{app}\ARConnectorSetup.exe"; Parameters: "uninstall"; WorkingDir: "{app}"; Flags: waituntilterminated runhidden; RunOnceId: "RemoveService"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    if MsgBox('Also delete the stored configuration and credentials (Console tokens, Dynamics GP and mailbox settings, logs)?' + #13#10 + #13#10 +
              'Choose No if you plan to reinstall and keep your registration.',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    begin
      Exec(ExpandConstant('{app}\ARConnectorSetup.exe'), 'purge', ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode);
    end;
  end;
end;
