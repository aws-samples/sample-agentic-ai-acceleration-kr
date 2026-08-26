<#
.SYNOPSIS
    Builds the LLM Gateway CLI v2 Windows executables and offline installer.

.DESCRIPTION
    Runs the full packaging pipeline on a Windows x64 machine:
      1. Creates a clean build venv (Python 3.11+).
      2. Installs the project + PyInstaller into it (online, or from a local
         wheel cache created by download_wheels.ps1 for air-gapped builders).
      3. Runs PyInstaller with packaging\gateway_cli.spec.
      4. Smoke-tests each produced exe (--help).
      5. Compiles the Inno Setup installer (if ISCC.exe is available).

    Output:
      dist\gateway-cli-suite\                exes + shared runtime (zip-able)
      dist\installer\gateway-cli-setup-<v>.exe   single-file offline installer

.PARAMETER WheelDir
    Optional path to a directory of pre-downloaded wheels. When set, pip runs
    with --no-index --find-links so the build itself needs no internet.

.PARAMETER Version
    Version stamped into the installer file name. Defaults to the version in
    pyproject.toml.

.PARAMETER SkipInstaller
    Build only the PyInstaller output; skip the Inno Setup compile.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\build.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -WheelDir C:\wheels
#>
[CmdletBinding()]
param(
    [string]$WheelDir = "",
    [string]$Version = "",
    # Environment-specific corporate values baked into the build. The two OIDC
    # values are the ones that must be supplied per environment; the domains and
    # CA path have generic placeholder defaults in cli/site_defaults.py and only
    # need overriding for a different environment. Any value left blank here (or
    # via the matching GATEWAY_CLI_DEFAULT_* env var) falls back to the literal
    # default in site_defaults.py.
    [string]$OidcIssuerUrl = $env:GATEWAY_CLI_DEFAULT_OIDC_ISSUER_URL,
    [string]$OidcClientId  = $env:GATEWAY_CLI_DEFAULT_OIDC_CLIENT_ID,
    [string]$GatewayUrl    = $env:GATEWAY_CLI_DEFAULT_GATEWAY_URL,
    [string]$AdminApiUrl   = $env:GATEWAY_CLI_DEFAULT_ADMIN_API_URL,
    [string]$CaBundle      = $env:GATEWAY_CLI_DEFAULT_CA_BUNDLE,
    # Corporate forward-proxy validation values baked into cli/managed (read by
    # cli/verify's proxy check). All are environment-specific real infrastructure,
    # so they carry only generic placeholder fallbacks in source:
    #   ExpectedProxyUrl      - forward-proxy address HTTP_PROXY/HTTPS_PROXY must equal.
    #   NoProxyValue          - NO_PROXY bypass list (corporate domain suffixes +
    #                           internal CIDR ranges reached directly).
    #   ForbiddenNoProxyToken - corporate suffix that must NOT appear in NO_PROXY
    #                           (its endpoints are reached via the CIDR ranges).
    [string]$ExpectedProxyUrl      = $env:GATEWAY_CLI_DEFAULT_EXPECTED_PROXY_URL,
    [string]$NoProxyValue          = $env:GATEWAY_CLI_DEFAULT_NO_PROXY_VALUE,
    [string]$ForbiddenNoProxyToken = $env:GATEWAY_CLI_DEFAULT_FORBIDDEN_NO_PROXY_TOKEN,
    # Code-signing (Authenticode). Supply EITHER a cert-store thumbprint (for an
    # HSM/token/CNG-backed cert, the enterprise norm) OR a PFX file + password.
    # When neither is given, signing is skipped with a warning and the build
    # still succeeds - unsigned binaries are fine for internal testing but WILL
    # trip AV/SmartScreen on locked-down fleets, so a production build must sign.
    [string]$SignThumbprint = $env:GATEWAY_CLI_SIGN_THUMBPRINT,
    [string]$SignPfxFile    = $env:GATEWAY_CLI_SIGN_PFX,
    [string]$SignPfxPassword = $env:GATEWAY_CLI_SIGN_PFX_PASSWORD,
    # RFC 3161 timestamp server so signatures stay valid after the cert expires.
    [string]$TimestampUrl   = "http://timestamp.digicert.com",
    [string]$SignToolPath   = "",
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"

# Layout:
#   $PackagingDir = …\packaging                       (this script's dir)
#   $RepoRoot     = …\win_installer                   (build/dist output root)
#   $ProjectDir   = …\packaging\entrypoints\gateway-cli-v2  (real pyproject + src)
$PackagingDir = $PSScriptRoot
$RepoRoot = Split-Path -Parent $PSScriptRoot
$ProjectDir = Join-Path $PackagingDir "entrypoints\gateway-cli-v2"
if (-not (Test-Path (Join-Path $ProjectDir "pyproject.toml"))) {
    throw "Project not found at $ProjectDir (expected pyproject.toml there)."
}
# Run from $RepoRoot so PyInstaller writes build\ and dist\ there; the spec
# resolves the project source relative to its own location, not the cwd.
Set-Location $RepoRoot

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }

# ---------------------------------------------------------------------------
# Code-signing (Authenticode) helpers
# ---------------------------------------------------------------------------
# Resolve signtool.exe once. It ships with the Windows SDK; the newest version
# under the versioned SDK bin dirs is preferred, falling back to PATH.
function Resolve-SignTool {
    if ($SignToolPath) {
        if (-not (Test-Path $SignToolPath)) { throw "SignToolPath not found: $SignToolPath" }
        return $SignToolPath
    }
    $roots = @(
        "${env:ProgramFiles(x86)}\Windows Kits\10\bin",
        "${env:ProgramFiles}\Windows Kits\10\bin"
    ) | Where-Object { Test-Path $_ }
    foreach ($root in $roots) {
        $found = Get-ChildItem -Path $root -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match "\\x64\\" } |
            Sort-Object FullName -Descending | Select-Object -First 1
        if ($found) { return $found.FullName }
    }
    $fromPath = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($fromPath) { return $fromPath.Source }
    return $null
}

# True when the caller supplied any signing credential.
function Should-Sign {
    return -not ([string]::IsNullOrWhiteSpace($SignThumbprint) -and [string]::IsNullOrWhiteSpace($SignPfxFile))
}

$script:SignTool = $null

# Sign one or more files with SHA-256 + RFC 3161 timestamp, then verify.
function Invoke-Sign([string[]]$Files) {
    if (-not (Should-Sign)) { return }
    if (-not $script:SignTool) {
        $script:SignTool = Resolve-SignTool
        if (-not $script:SignTool) {
            throw "Signing was requested but signtool.exe was not found. Install the Windows SDK or pass -SignToolPath."
        }
        Write-Host "Using signtool: $script:SignTool"
    }

    # Build the credential-selection args once (thumbprint takes precedence).
    $credArgs = @()
    if (-not [string]::IsNullOrWhiteSpace($SignThumbprint)) {
        $credArgs = @("/sha1", $SignThumbprint)
    } else {
        if (-not (Test-Path $SignPfxFile)) { throw "SignPfxFile not found: $SignPfxFile" }
        $credArgs = @("/f", $SignPfxFile)
        if (-not [string]::IsNullOrWhiteSpace($SignPfxPassword)) {
            $credArgs += @("/p", $SignPfxPassword)
        }
    }

    foreach ($file in $Files) {
        if (-not (Test-Path $file)) { throw "Cannot sign missing file: $file" }
        & $script:SignTool sign /fd SHA256 /td SHA256 /tr $TimestampUrl @credArgs $file
        if ($LASTEXITCODE -ne 0) { throw "signtool sign failed ($LASTEXITCODE) for $file" }
        & $script:SignTool verify /pa $file
        if ($LASTEXITCODE -ne 0) { throw "signtool verify failed ($LASTEXITCODE) for $file" }
        Write-Host "  signed $(Split-Path -Leaf $file)"
    }
}

# ---------------------------------------------------------------------------
Step "Checking prerequisites"
# ---------------------------------------------------------------------------
if (-not [Environment]::Is64BitOperatingSystem) {
    throw "This build must run on 64-bit Windows (matches ArchitecturesAllowed in installer.iss)."
}

# Prefer the py launcher so we can pin a 3.11+ interpreter.
$PythonExe = $null
$PythonArgs = @()
foreach ($candidate in @("py -3.13", "py -3.12", "py -3.11", "python")) {
    $parts = @($candidate -split " ")
    $exe = $parts[0]
    $extra = @($parts | Select-Object -Skip 1)
    try {
        $v = & $exe @extra -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and [version]$v -ge [version]"3.11") {
            $PythonExe = $exe
            $PythonArgs = $extra
            Write-Host "Using Python $v via '$candidate'"
            break
        }
    } catch { }
}
if (-not $PythonExe) {
    throw "No Python >= 3.11 found. Install it from python.org (the build machine needs Python; end users do not)."
}

if (-not $Version) {
    $pyproj = Join-Path $ProjectDir "pyproject.toml"
    $Version = (Select-String -Path $pyproj -Pattern '^version\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
}
Write-Host "Building version: $Version"

# ---------------------------------------------------------------------------
Step "Creating build venv"
# ---------------------------------------------------------------------------
$VenvDir = Join-Path $RepoRoot ".build-venv"
if (Test-Path $VenvDir) { Remove-Item -Recurse -Force $VenvDir }
& $PythonExe @PythonArgs -m venv $VenvDir
$Py = Join-Path $VenvDir "Scripts\python.exe"

$PipArgs = @()
if ($WheelDir) {
    if (-not (Test-Path $WheelDir)) { throw "WheelDir not found: $WheelDir" }
    Write-Host "Offline mode: installing from $WheelDir"
    $PipArgs = @("--no-index", "--find-links", $WheelDir)
}

# ---------------------------------------------------------------------------
Step "Installing project and build tools into the venv"
# ---------------------------------------------------------------------------
& $Py -m pip install --upgrade pip @PipArgs
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }
# pip builds the poetry-core project directly; no Poetry install needed.
# Install the project from its nested directory (pyproject.toml lives there).
& $Py -m pip install @PipArgs $ProjectDir "pyinstaller>=6.11"
if ($LASTEXITCODE -ne 0) { throw "dependency install failed" }

# ---------------------------------------------------------------------------
# Load packaging\site-config.json (guideline 1-1), if present. This is the single
# file a site admin edits to set the baked corporate defaults (OIDC, domains, CA
# path) without touching build.ps1. It only fills values NOT already supplied via
# a -Param or GATEWAY_CLI_DEFAULT_* env var, so the documented precedence holds:
#   -Param  >  env var  >  site-config.json  >  site_defaults.py literal
# Edit packaging\site-config.json (camelCase keys) to set the baked values.
$SiteConfigInput = Join-Path $PackagingDir "site-config.json"
if (Test-Path $SiteConfigInput) {
    Step "Loading site-config.json"
    try {
        $sc = Get-Content -Raw -Path $SiteConfigInput | ConvertFrom-Json
    } catch {
        throw "site-config.json is not valid JSON: $($_.Exception.Message)"
    }
    # ConvertFrom-Json returns a PSCustomObject; map its camelCase keys to the
    # script params. A leading __comment (string or array) is simply ignored.
    foreach ($pair in @(
        @{ Name = 'OidcIssuerUrl'; Key = 'oidcIssuerUrl' },
        @{ Name = 'OidcClientId';  Key = 'oidcClientId'  },
        @{ Name = 'GatewayUrl';    Key = 'gatewayUrl'    },
        @{ Name = 'AdminApiUrl';   Key = 'adminApiUrl'   },
        @{ Name = 'CaBundle';      Key = 'caBundle'      }
    )) {
        # Only apply the file value when the caller left the param/env empty.
        $cur = (Get-Variable -Name $pair.Name -Scope Script -ErrorAction SilentlyContinue).Value
        $val = $sc.PSObject.Properties[$pair.Key].Value
        if ([string]::IsNullOrWhiteSpace($cur) -and -not [string]::IsNullOrWhiteSpace($val)) {
            Set-Variable -Name $pair.Name -Value ([string]$val) -Scope Script
            Write-Host "  from site-config.json: $($pair.Name)"
        }
    }
} else {
    Write-Host "`n(no packaging\site-config.json - using -Param/env/site_defaults.py values)"
}

# ---------------------------------------------------------------------------
Step "Baking corporate site defaults into the build"
# ---------------------------------------------------------------------------
# Generate cli/_site_config.py with the environment-specific values. Only the
# keys given here are emitted; anything omitted falls back to the literal
# default in cli/site_defaults.py. The file is build output, not source - it is
# overwritten every build and should not be committed.
$SiteConfigPath = Join-Path $ProjectDir "src\cli\_site_config.py"
$siteLines = @(
    "# Auto-generated by build.ps1 - DO NOT EDIT, DO NOT COMMIT.",
    "# Environment-specific corporate defaults baked into this build.",
    ""
)
function Add-SiteValue($name, $value) {
    if (-not [string]::IsNullOrWhiteSpace($value)) {
        # Python string literal via JSON encoding (handles quotes/backslashes).
        $lit = $value | ConvertTo-Json
        $script:siteLines += "$name = $lit"
        Write-Host "  baked $name"
    }
}
Add-SiteValue "OIDC_ISSUER_URL" $OidcIssuerUrl
Add-SiteValue "OIDC_CLIENT_ID"  $OidcClientId
Add-SiteValue "GATEWAY_URL"     $GatewayUrl
Add-SiteValue "ADMIN_API_URL"   $AdminApiUrl
Add-SiteValue "CA_BUNDLE"       $CaBundle
Add-SiteValue "EXPECTED_PROXY_URL"       $ExpectedProxyUrl
Add-SiteValue "NO_PROXY_VALUE"           $NoProxyValue
Add-SiteValue "FORBIDDEN_NO_PROXY_TOKEN" $ForbiddenNoProxyToken
[System.IO.File]::WriteAllText(
    $SiteConfigPath,
    ($siteLines -join "`n") + "`n",
    (New-Object System.Text.UTF8Encoding($false))
)
if ([string]::IsNullOrWhiteSpace($OidcIssuerUrl) -or [string]::IsNullOrWhiteSpace($OidcClientId)) {
    Write-Warning "OIDC issuer/client not supplied - the build will have blank OIDC defaults."
    Write-Warning "Users will need --oidc-issuer-url/--oidc-client-id or a card. Pass -OidcIssuerUrl/-OidcClientId to bake them."
}

# Bundle the optional site-extra.json (guideline 1-4): the operator edits ONE JSON
# with custom managed/user keys and it is deep-merged into the settings files at
# setup time. Copied next to cli/site_extra.py so it ships inside the .exe; the
# spec collects cli/*.json. Absent -> injection is simply a no-op.
$SiteExtraSrc = Join-Path $PackagingDir "site-extra.json"
$SiteExtraDst = Join-Path $ProjectDir "src\cli\site_extra.json"
if (Test-Path $SiteExtraSrc) {
    Copy-Item -Force $SiteExtraSrc $SiteExtraDst
    Write-Host "  bundled site-extra.json"
} else {
    if (Test-Path $SiteExtraDst) { Remove-Item -Force $SiteExtraDst }
    Write-Host "  no site-extra.json (custom-key injection disabled)"
}

# Re-install so the generated module is importable by PyInstaller's analysis.
& $Py -m pip install @PipArgs --no-deps --force-reinstall $ProjectDir | Out-Null

# ---------------------------------------------------------------------------
Step "Running PyInstaller"
# ---------------------------------------------------------------------------
$env:GATEWAY_CLI_VERSION = $Version
# PyInstaller logs its progress to stderr. Under $ErrorActionPreference='Stop'
# PowerShell promotes that native stderr into a terminating NativeCommandError the
# moment the output is captured (an unattended SSM/CI run), aborting the build before
# the exit-code check below ever runs. Interactive console runs don't capture, so the
# bug is invisible there. Run PyInstaller under 'Continue' and gate on the real exit code.
$eapPyi = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $Py -m PyInstaller --noconfirm --clean "packaging\gateway_cli.spec" 2>&1 | ForEach-Object { Write-Host $_ }
$pyiRc = $LASTEXITCODE
$ErrorActionPreference = $eapPyi
if ($pyiRc -ne 0) { throw "PyInstaller build failed ($pyiRc)" }

# ---------------------------------------------------------------------------
Step "Smoke-testing executables"
# ---------------------------------------------------------------------------
$DistDir = Join-Path $RepoRoot "dist\gateway-cli-suite"
foreach ($exe in @("gateway-cli.exe", "api-key-helper.exe", "statusline.exe")) {
    $path = Join-Path $DistDir $exe
    if (-not (Test-Path $path)) { throw "Expected executable missing: $path" }
    # Same native-stderr caveat as PyInstaller above: gate on the exit code, not stderr.
    $eapSmoke = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & $path --help 2>&1 | Out-Null
    $smokeRc = $LASTEXITCODE
    $ErrorActionPreference = $eapSmoke
    if ($smokeRc -ne 0) {
        throw "$exe --help exited with $smokeRc - the bundle is likely missing a module. Re-run it manually to see the traceback."
    }
    Write-Host "OK: $exe"
}

# ---------------------------------------------------------------------------
Step "Code-signing executables"
# ---------------------------------------------------------------------------
# Sign the three exes BEFORE the installer is compiled so the installer embeds
# already-signed binaries (the installer itself is signed separately below).
if (Should-Sign) {
    Invoke-Sign @(
        (Join-Path $DistDir "gateway-cli.exe"),
        (Join-Path $DistDir "api-key-helper.exe"),
        (Join-Path $DistDir "statusline.exe")
    )
} else {
    Write-Warning "No signing credential supplied - executables are UNSIGNED."
    Write-Warning "Pass -SignThumbprint (cert store / HSM) or -SignPfxFile/-SignPfxPassword to sign."
    Write-Warning "Unsigned PyInstaller binaries commonly trip AV/SmartScreen on locked-down fleets."
}

# ---------------------------------------------------------------------------
if ($SkipInstaller) {
    Step "Done (installer skipped)"
    Write-Host "PyInstaller output: $DistDir"
    exit 0
}

Step "Compiling Inno Setup installer"
# ---------------------------------------------------------------------------
$Iscc = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
    "${env:LOCALAPPDATA}\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Iscc) {
    $fromPath = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($fromPath) { $Iscc = $fromPath.Source }
}
if (-not $Iscc) {
    Write-Warning "Inno Setup 6 (ISCC.exe) not found - skipping installer compile."
    Write-Warning "Install it from https://jrsoftware.org/isdl.php, or ship dist\gateway-cli-suite as a zip."
    exit 0
}

# Same native-stderr caveat as PyInstaller: gate on the exit code under 'Continue'.
$eapIscc = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $Iscc "/DAppVersion=$Version" "packaging\installer.iss" 2>&1 | ForEach-Object { Write-Host $_ }
$isccRc = $LASTEXITCODE
$ErrorActionPreference = $eapIscc
if ($isccRc -ne 0) { throw "Inno Setup compile failed ($isccRc)" }

# ---------------------------------------------------------------------------
Step "Code-signing installer"
# ---------------------------------------------------------------------------
$InstallerPath = Join-Path $RepoRoot "dist\installer\gateway-cli-setup-$Version.exe"
if (Should-Sign) {
    Invoke-Sign @($InstallerPath)
} else {
    Write-Warning "Installer is UNSIGNED (no signing credential supplied)."
}

Step "Build complete"
Write-Host "Executables: $DistDir"
Write-Host "Installer:   $(Join-Path $RepoRoot "dist\installer\gateway-cli-setup-$Version.exe")"
Write-Host "Deliver the installer to the isolated network and run it there (silent: /VERYSILENT /NORESTART)."
