<#
.SYNOPSIS
  Builds the customer-facing AR Automation Connector package, reproducibly.

.DESCRIPTION
  1. Creates installer\.venv with the pinned packages in requirements-connector.txt.
  2. Runs PyInstaller on ar_connector.spec -> installer\dist\ARConnector\
     (ARConnectorSetup.exe + ARConnectorService.exe + shared runtime).
  3. If the Inno Setup compiler (ISCC.exe) is installed, compiles
     ar_connector.iss -> installer\dist\AR-Connector-Setup-<version>.exe.
     Otherwise zips the folder to installer\dist\AR-Connector-<version>.zip.
  4. Writes installer\dist\SHA256SUMS.txt.

  The version comes from ..\connector_version.py — bump it there.

.PARAMETER SkipVenv
  Reuse the existing installer\.venv without reinstalling packages.
.PARAMETER SkipInno
  Build only the PyInstaller folder and the zip, even if ISCC is present.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File installer\build.ps1
#>
param(
    [switch]$SkipVenv,
    [switch]$SkipInno,
    [string]$DistDir = "dist"   # output folder name inside the installer folder
)

$ErrorActionPreference = "Stop"
$installerDir = $PSScriptRoot
$repoRoot = Split-Path $installerDir -Parent
$venv = Join-Path $installerDir ".venv"
$dist = Join-Path $installerDir $DistDir

Push-Location $installerDir
try {
    $version = (python -c "import sys; sys.path.insert(0, r'$repoRoot'); from connector_version import CONNECTOR_VERSION; print(CONNECTOR_VERSION)").Trim()
    if (-not $version) { throw "Could not read CONNECTOR_VERSION from connector_version.py" }
    Write-Host "== Building AR Connector $version" -ForegroundColor Cyan

    # 1. virtualenv with pinned inputs
    if (-not $SkipVenv -or -not (Test-Path $venv)) {
        Write-Host "== Creating virtualenv at $venv"
        python -m venv $venv
        & "$venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
        & "$venv\Scripts\python.exe" -m pip install --quiet -r (Join-Path $installerDir "requirements-connector.txt")
    }
    $py = "$venv\Scripts\python.exe"

    # 2. PyInstaller
    $stale = Join-Path $dist "ARConnector"
    if (Test-Path $stale) {
        Remove-Item -Recurse -Force $stale -ErrorAction SilentlyContinue
        if (Test-Path $stale) { throw "Cannot delete $stale - an ARConnectorSetup.exe/ARConnectorService.exe from a previous build is still running. Close it (or pass -DistDir other) and retry." }
    }
    Write-Host "== Running PyInstaller"
    & $py -m PyInstaller --noconfirm --clean --distpath $dist --workpath (Join-Path $installerDir "build") (Join-Path $installerDir "ar_connector.spec")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
    if (-not (Test-Path (Join-Path $dist "ARConnector\ARConnectorSetup.exe"))) { throw "ARConnectorSetup.exe missing after build" }
    if (-not (Test-Path (Join-Path $dist "ARConnector\ARConnectorService.exe"))) { throw "ARConnectorService.exe missing after build" }

    # 3. Inno Setup (preferred) or zip
    $iscc = $null
    foreach ($candidate in @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe")) {
        if (Test-Path $candidate) { $iscc = $candidate; break }
    }
    $zipPath = Join-Path $dist "AR-Connector-$version.zip"
    if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
    Compress-Archive -Path (Join-Path $dist "ARConnector\*"), (Join-Path $installerDir "INSTALL-GUIDE.md") -DestinationPath $zipPath
    Write-Host "== Zip package: $zipPath"

    if ($iscc -and -not $SkipInno) {
        Write-Host "== Compiling Inno Setup package with $iscc"
        & $iscc "/DAppVersion=$version" "/DDistDir=$DistDir" (Join-Path $installerDir "ar_connector.iss")
        if ($LASTEXITCODE -ne 0) { throw "ISCC failed" }
        Write-Host "== Setup package: $(Join-Path $dist "AR-Connector-Setup-$version.exe")"
    } elseif (-not $SkipInno) {
        Write-Warning "Inno Setup not found (winget install JRSoftware.InnoSetup) - only the zip package was produced."
    }

    # 4. checksums
    $sums = Get-ChildItem $dist -File | Where-Object { $_.Extension -in ".exe", ".zip" } | ForEach-Object {
        "{0}  {1}" -f (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower(), $_.Name
    }
    Set-Content -Path (Join-Path $dist "SHA256SUMS.txt") -Value $sums -Encoding ascii
    Write-Host "== Done. Artifacts in $dist" -ForegroundColor Green
    $sums | ForEach-Object { Write-Host "   $_" }
}
finally {
    Pop-Location
}
