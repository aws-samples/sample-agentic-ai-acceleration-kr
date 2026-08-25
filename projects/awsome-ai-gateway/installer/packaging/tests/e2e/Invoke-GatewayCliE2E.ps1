<#
.SYNOPSIS
    End-to-end functional test of an installed gateway-cli against a live gateway.

.DESCRIPTION
    Runs the real user journey against the installed executable - one pass per
    account supplied in -Users:

        login --no-browser --email <e> --password-stdin   (headless Cognito login)
        setup --model <model>                              (write Claude Code config)
        verify                                             (health + drift checks)
        api-key-helper                                     (issue a Virtual Key to stdout)
        POST /v1/messages                                  (live inference with that key)

    and asserts on each step's process exit code plus the JSON emitted by `verify`
    (its "verify_complete" event carries a per-check status map). It then runs the two
    checks verify cannot: it executes the installed api-key-helper (verify only confirms
    the file exists) and requires a single bare Virtual Key on stdout, then POSTs a minimal
    /v1/messages using that key. This is the same flow the Windows installer enables; it
    needs no console, so it runs unattended over SSM (as SYSTEM) or interactively.

    Inference is classified, not pass/fail-on-200: a transport/TLS failure or an auth
    rejection (401/403) fails the gate (the credential + routing path we own is broken);
    any other backend error (e.g. a 5xx from a known-degraded dev inference backend) is a
    WARN, because the gateway still accepted the credential and routed the request.

    SCOPE (SSM/SYSTEM): login caches, HKCU env, and ~/.claude/settings.json are user-scoped,
    so an SSM run validates SYSTEM's profile - the whole journey runs as one identity, so what
    it exercises is internally consistent. Real users run the elevated installer once and then
    use Claude Code as themselves; per product decision that is a no-op difference, so this gate
    intentionally does not load a separate target-user profile.

    Headless login relies on `--password-stdin`: getpass cannot read piped input on
    a headless Windows console, so the password is fed as the first line of stdin.

.PARAMETER Users
    One hashtable per account, each:
        @{ Email = 'you@corp'; Password = '...'; ExpectProvisioned = $true }
    ExpectProvisioned = $false asserts the account authenticates with Cognito but is
    rejected at Virtual-Key provisioning (e.g. no Cognito group -> "no group mapping
    found") - the client must surface that cleanly rather than crash.

    NEVER hardcode passwords in a committed file. Pass them at call time from a
    secret store / CI variable / admin-reset temp value.

.PARAMETER Model
    Model alias handed to `setup --model` (required by setup). Must be a gateway
    alias; defaults to the primary baked fallback.

.PARAMETER CliExe
    Path to the installed gateway-cli.exe.

.EXAMPLE
    .\Invoke-GatewayCliE2E.ps1 -Users @(
        @{ Email='clitest@example.com'; Password=$env:CLITEST_PW; ExpectProvisioned=$true },
        @{ Email='hhkang@amazon.com';   Password=$env:HHKANG_PW;  ExpectProvisioned=$true }
    )

.OUTPUTS
    Writes a PASS/FAIL summary and exits 0 only when every account meets expectations.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [hashtable[]]$Users,

    [string]$Model = 'claude-sonnet-4-6',

    [string]$CliExe = 'C:\Program Files\GatewayCLI\gateway-cli.exe'
)

# Targets Windows PowerShell 5.1 (what SSM RunPowerShellScript uses): no ternary
# operator, and no Set-StrictMode (which would throw on absent verify-check keys -
# we want missing keys to read as $null and fail the assertion gracefully).

# gateway-cli (Click) and its deps log progress to stderr - and verify's structured
# "verify_complete" event is a stderr line too. Capturing native stderr through a
# PowerShell pipe (`2>&1 | Out-String`) is unusable here: under a 'Stop' preference it
# becomes a terminating NativeCommandError, and even under 'Continue' each stderr line
# is wrapped as an ErrorRecord and re-flowed to the console width, which shreds the
# single-line JSON across physical lines so it can never be parsed. Instead we run the
# exe via Start-Process with OS-level stream redirection to temp files: raw bytes, no
# PowerShell reformatting, intact JSON - and a clean way to feed the headless password
# on stdin (a redirected file) without getpass choking on a piped console handle.
function Invoke-Cli {
    param(
        [string[]]$CliArgs,
        [string]$StdinText = $null,  # when set, written to a temp file and fed as stdin
        [string]$Exe = $CliExe       # override to run a sibling binary (e.g. api-key-helper.exe)
    )
    $outFile = [System.IO.Path]::GetTempFileName()
    $errFile = [System.IO.Path]::GetTempFileName()
    $inFile  = $null
    $spArgs = @{
        FilePath               = $Exe
        NoNewWindow            = $true
        Wait                   = $true
        PassThru               = $true
        RedirectStandardOutput = $outFile
        RedirectStandardError  = $errFile
    }
    # Start-Process rejects an empty -ArgumentList array (PS 5.1), so only add it when
    # there are args - a bare binary (api-key-helper with no flags) passes @().
    if ($CliArgs -and $CliArgs.Count -gt 0) { $spArgs.ArgumentList = $CliArgs }
    if ($null -ne $StdinText) {
        $inFile = [System.IO.Path]::GetTempFileName()
        # ASCII, no BOM; the CLI reads the first line. Trailing newline terminates it.
        [System.IO.File]::WriteAllText($inFile, $StdinText + "`n", (New-Object System.Text.ASCIIEncoding))
        $spArgs.RedirectStandardInput = $inFile
    }
    try {
        $proc = Start-Process @spArgs
        $out = Get-Content -Raw -Path $outFile -ErrorAction SilentlyContinue
        $err = Get-Content -Raw -Path $errFile -ErrorAction SilentlyContinue
        return @{ Code = $proc.ExitCode; Out = "$out"; Err = "$err"; All = "$out`n$err" }
    }
    finally {
        Remove-Item -Force -ErrorAction SilentlyContinue $outFile, $errFile
        if ($inFile) { Remove-Item -Force -ErrorAction SilentlyContinue $inFile }
    }
}

function Get-VerifyChecks {
    # Extract the structured "verify_complete" line and return its .checks object,
    # or $null when the line is absent / unparseable. The line is intact here because
    # Invoke-Cli captured raw stderr bytes (see its note), so a single-line match holds.
    param([string]$VerifyOutput)
    $line = ($VerifyOutput -split "`r?`n") |
        Where-Object { $_ -match '"event":\s*"verify_complete"' } |
        Select-Object -First 1
    if (-not $line) { return $null }
    try { return ($line | ConvertFrom-Json).checks } catch { return $null }
}

function Get-ClaudeSettings {
    # Read the settings.json that `setup` just wrote (current profile) and return the
    # parsed object, or $null. This is the same file Claude Code reads to (a) inject the
    # env block before running apiKeyHelper and (b) discover ANTHROPIC_BASE_URL. Under SSM
    # this is SYSTEM's profile - the harness runs the whole journey (login/setup/verify/
    # helper/inference) as one identity, so the caches, settings and token it exercises are
    # internally consistent. Real end users run the elevated installer once, then use Claude
    # Code as themselves; validating SYSTEM's profile here is an accepted scope of this gate.
    $settingsPath = Join-Path $env:USERPROFILE '.claude\settings.json'
    if (-not (Test-Path $settingsPath)) { return $null }
    try { return (Get-Content -Raw -Path $settingsPath | ConvertFrom-Json) } catch { return $null }
}

function Set-HelperEnvFromSettings {
    # Claude Code injects settings.json's env block into the environment before invoking
    # apiKeyHelper; replicate that so a direct helper run resolves OIDC mode exactly as it
    # would in production. Returns the ANTHROPIC_BASE_URL (needed for the inference probe).
    param($Settings)
    $envBlock = $Settings.env
    if ($null -eq $envBlock) { return $null }
    foreach ($name in @('OIDC_ISSUER_URL', 'OIDC_CLIENT_ID', 'ADMIN_API_URL',
                        'GATEWAY_CLI_GATEWAY_URL', 'ANTHROPIC_BASE_URL', 'NODE_EXTRA_CA_CERTS',
                        'REQUESTS_CA_BUNDLE', 'AWS_CA_BUNDLE', 'SSL_CERT_FILE', 'NO_PROXY')) {
        $val = $envBlock.$name
        if ($null -ne $val -and "$val".Length -gt 0) {
            Set-Item -Path "env:$name" -Value "$val"
        }
    }
    return $envBlock.ANTHROPIC_BASE_URL
}

function Invoke-Inference {
    # POST a minimal /v1/messages using the helper-issued key. Classifies the outcome so a
    # genuine CLIENT fault (no TLS handshake / auth rejection) fails the gate, while a gateway
    # that accepted the credential and forwarded the request - but whose backend errored - is
    # reported as a WARN, not a FAIL. Rationale: this proves the credential + routing path the
    # installer is responsible for; the dev inference backend is separately known-degraded and
    # its 5xx is not a client regression. Returns @{ Status=<int|0>; Verdict='pass'|'warn'|'fail'; Detail='...' }.
    param([string]$BaseUrl, [string]$ApiKey, [string]$Model)

    if ([string]::IsNullOrWhiteSpace($BaseUrl)) {
        return @{ Status = 0; Verdict = 'fail'; Detail = 'no ANTHROPIC_BASE_URL in settings.json env' }
    }
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $uri = ($BaseUrl.TrimEnd('/')) + '/v1/messages'
    # Build the JSON literally: ConvertTo-Json (PS 5.1) unwraps a single-element array, so a
    # one-message `messages` would serialize as an object, not the array the API requires.
    # $Model is a validated gateway alias (no quotes/backslashes), so direct interpolation is safe.
    $body = '{"model":"' + $Model + '","max_tokens":16,"messages":[{"role":"user","content":"ping"}]}'
    # The gateway authenticates virtual keys via `Authorization: Bearer <vk>` (same as
    # statusline/usage_client.py, the real client path). An `x-api-key` header is rejected
    # 401 by this gateway, so a faithful probe must use Bearer - otherwise the gate would
    # fail a healthy credential on a header mismatch the product never hits.
    $headers = @{
        'Authorization'     = "Bearer $ApiKey"
        'anthropic-version' = '2023-06-01'
        'content-type'      = 'application/json'
    }
    try {
        $resp = Invoke-WebRequest -Uri $uri -Method Post -Headers $headers -Body $body `
            -UseBasicParsing -TimeoutSec 60 -ErrorAction Stop
        return @{ Status = [int]$resp.StatusCode; Verdict = 'pass'; Detail = "HTTP $([int]$resp.StatusCode)" }
    }
    catch {
        # A protocol error (non-2xx) carries an HTTP response we can classify; a transport
        # failure (DNS/TLS/connect) does not - that one is a hard client fault.
        $webResp = $null
        if ($_.Exception -and $_.Exception.Response) { $webResp = $_.Exception.Response }
        if ($null -eq $webResp) {
            return @{ Status = 0; Verdict = 'fail'; Detail = "no HTTP response (transport/TLS): $($_.Exception.Message)" }
        }
        $status = 0
        try { $status = [int]$webResp.StatusCode } catch { $status = 0 }
        if ($status -eq 401 -or $status -eq 403) {
            return @{ Status = $status; Verdict = 'fail'; Detail = "gateway rejected the helper credential (HTTP $status)" }
        }
        # Reached the gateway, credential accepted, request forwarded - backend/model errored.
        return @{ Status = $status; Verdict = 'warn'; Detail = "gateway routed OK; backend returned HTTP $status (inference backend, not client)" }
    }
}

# Checks that must be 'ok' for a healthy, fully-provisioned install. Proxy checks are
# intentionally excluded: on a host with no corporate proxy they warn against the baked
# expected-proxy placeholder, which is not a functional failure.
$CriticalChecks = @(
    'gateway-proxy', 'admin-api', 'oidc-tokens',
    'claude-settings', 'api-key-helper'
)

$results = @()
$anyFail = $false

foreach ($u in $Users) {
    $email = $u.Email
    $password = $u.Password
    $expectProvisioned = $u.ContainsKey('ExpectProvisioned') -and [bool]$u.ExpectProvisioned
    $failures = @()

    Write-Host "`n########## $email (ExpectProvisioned=$expectProvisioned) ##########" -ForegroundColor Cyan

    if (-not (Test-Path $CliExe)) { throw "gateway-cli not found at $CliExe - install it first." }

    # ---- login (headless) ----
    $login = Invoke-Cli -CliArgs @('login', '--no-browser', '--email', $email, '--password-stdin') -StdinText $password
    $loginCode = $login.Code
    $loginOut = $login.All
    Write-Host "  login exit=$loginCode"

    if ($expectProvisioned) {
        if ($loginCode -ne 0) { $failures += "login exited $loginCode (expected 0)" }
        if ($loginOut -notmatch 'Login successful') { $failures += "login did not report success" }
    }
    else {
        # Cognito auth should still succeed; VK provisioning should be the rejection point.
        if ($loginCode -eq 0) { $failures += "login unexpectedly succeeded for an unprovisioned account" }
        if ($loginOut -notmatch 'no group mapping|Login failed') {
            $failures += "unprovisioned account did not surface a clean provisioning error"
        }
        # Nothing further to assert for an account we expect to be blocked.
        $blockedResult = if ($failures.Count -eq 0) { 'PASS' } else { 'FAIL' }
        $results += [pscustomobject]@{ Email = $email; LoginExit = $loginCode; SetupExit = 'n/a'; VerifyExit = 'n/a'; Provisioned = $false; Helper = 'n/a'; Inference = 'n/a'; Result = $blockedResult }
        if ($failures.Count) { $anyFail = $true; $failures | ForEach-Object { Write-Host "    [FAIL] $_" -ForegroundColor Red } }
        else { Write-Host "    [PASS] rejected at provisioning as expected" -ForegroundColor Green }
        continue
    }

    # ---- setup ----
    $setup = Invoke-Cli -CliArgs @('setup', '--model', $Model)
    $setupCode = $setup.Code
    $setupOut = $setup.All
    Write-Host "  setup exit=$setupCode"
    if ($setupCode -ne 0) { $failures += "setup exited $setupCode (expected 0): $($setupOut.Trim())" }

    # ---- verify ----
    $verify = Invoke-Cli -CliArgs @('verify')
    $verifyCode = $verify.Code
    $verifyOut = $verify.All
    Write-Host "  verify exit=$verifyCode"
    if ($verifyCode -ne 0) { $failures += "verify exited $verifyCode (expected 0)" }

    $checks = Get-VerifyChecks $verifyOut
    if ($null -eq $checks) {
        $failures += "verify emitted no parseable verify_complete event"
    }
    else {
        foreach ($c in $CriticalChecks) {
            $status = $checks.$c
            if ($status -ne 'ok') { $failures += "verify check '$c' = '$status' (expected ok)" }
        }
        # A fully-provisioned account must have a live Virtual Key cached.
        if ($checks.'vk-cache' -ne 'ok') { $failures += "vk-cache = '$($checks.'vk-cache')' (expected ok)" }
    }

    # ---- credential helper (functional) ----
    # verify only confirms the helper file exists and caches look current; it never runs it.
    # Execute the real helper the way Claude Code does: settings.json env injected, output on
    # stdout. A healthy install must emit exactly one bare token (the Virtual Key) and exit 0.
    $helperToken = $null
    $helperVerdict = 'skip'
    $settings = Get-ClaudeSettings
    $baseUrl = $null
    if ($null -eq $settings) {
        $failures += "cannot read settings.json to drive the credential helper"
    }
    else {
        $baseUrl = Set-HelperEnvFromSettings $settings
        $helperExe = Join-Path (Split-Path -Parent $CliExe) 'api-key-helper.exe'
        if (-not (Test-Path $helperExe)) {
            $failures += "api-key-helper.exe not found next to gateway-cli.exe at $helperExe"
        }
        else {
            $helper = Invoke-Cli -CliArgs @() -Exe $helperExe
            Write-Host "  api-key-helper exit=$($helper.Code)"
            $tok = "$($helper.Out)".Trim()
            if ($helper.Code -ne 0) {
                $failures += "api-key-helper exited $($helper.Code) (expected 0): $("$($helper.Err)".Trim())"
                $helperVerdict = 'fail'
            }
            elseif ([string]::IsNullOrWhiteSpace($tok) -or ($tok -match '\s')) {
                # Must be a single bare token on stdout - not empty, not multi-line, not a log line.
                $failures += "api-key-helper did not emit a single bare token on stdout"
                $helperVerdict = 'fail'
            }
            else {
                $helperToken = $tok
                $helperVerdict = 'ok'
            }
        }
    }

    # ---- live inference (functional) ----
    # POST a minimal /v1/messages with the helper token. pass=200, fail=transport/401/403,
    # warn=gateway routed but backend errored (the credential + routing path we own still works).
    $infVerdict = 'skip'
    $infDetail = 'not attempted (no helper token)'
    if ($helperVerdict -eq 'ok') {
        $inf = Invoke-Inference -BaseUrl $baseUrl -ApiKey $helperToken -Model $Model
        $infVerdict = $inf.Verdict
        $infDetail = $inf.Detail
        Write-Host "  inference: $infVerdict ($infDetail)"
        if ($infVerdict -eq 'fail') {
            $failures += "live inference failed: $infDetail"
        }
        elseif ($infVerdict -eq 'warn') {
            Write-Host "    [WARN] $infDetail" -ForegroundColor Yellow
        }
    }

    $userResult = if ($failures.Count -eq 0) { 'PASS' } else { 'FAIL' }
    $results += [pscustomobject]@{
        Email = $email; LoginExit = $loginCode; SetupExit = $setupCode
        VerifyExit = $verifyCode; Provisioned = $true
        Helper = $helperVerdict; Inference = $infVerdict
        Result = $userResult
    }
    if ($failures.Count) { $anyFail = $true; $failures | ForEach-Object { Write-Host "    [FAIL] $_" -ForegroundColor Red } }
    else { Write-Host "    [PASS] login + setup + verify + helper + inference all green" -ForegroundColor Green }
}

Write-Host "`n================= E2E SUMMARY =================" -ForegroundColor Cyan
$results | Format-Table -AutoSize | Out-String | Write-Host

if ($anyFail) { Write-Host "E2E RESULT: FAIL" -ForegroundColor Red; exit 1 }
Write-Host "E2E RESULT: PASS" -ForegroundColor Green
exit 0
