<#
.SYNOPSIS
    Pre-downloads all build dependencies as wheels for an air-gapped build.

.DESCRIPTION
    Run this on an INTERNET-CONNECTED Windows x64 machine with the same
    Python minor version as the build machine. It fills a wheel directory
    with the project's runtime dependencies plus pip/PyInstaller.

    Copy the resulting directory (plus this repository) to the offline build
    machine, then run:

        powershell -File packaging\build.ps1 -WheelDir C:\path\to\wheels

.PARAMETER OutDir
    Where to place the wheels. Default: .\wheels
#>
[CmdletBinding()]
param(
    [string]$OutDir = "wheels"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$ProjectDir = Join-Path $PSScriptRoot "entrypoints\gateway-cli-v2"
Set-Location $RepoRoot

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

# Wheels must match the build machine: Windows x64 + same Python minor
# version. Running this script with that interpreter guarantees it.
# The project itself lives in the nested gateway-cli-v2 directory.
python -m pip download --dest $OutDir `
    $ProjectDir `
    "pyinstaller>=6.11" `
    "pip" "setuptools" "wheel"
if ($LASTEXITCODE -ne 0) { throw "pip download failed" }

Write-Host "`nWheel cache ready: $OutDir"
Write-Host "Copy it to the build machine and pass -WheelDir to build.ps1."
