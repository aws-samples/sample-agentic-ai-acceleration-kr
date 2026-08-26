# gateway-cli end-to-end test

`Invoke-GatewayCliE2E.ps1` exercises the real user journey against an **installed**
`gateway-cli` and a **live** gateway, one pass per account:

```
login --no-browser --email <e> --password-stdin   headless Cognito login
setup --model <model>                              write Claude Code config
verify                                             health + drift checks
api-key-helper                                     issue a Virtual Key to stdout
POST /v1/messages                                  live inference with that key
```

It asserts on each step's process exit code **and** on the per-check status map
carried by `verify`'s structured `verify_complete` event. It then runs the two
things `verify` cannot: it **executes** the installed `api-key-helper` (verify only
checks the file exists) and requires a single bare Virtual Key on stdout, then
**POSTs a minimal `/v1/messages`** with that key. It needs no console, so it runs
unattended over SSM (as `SYSTEM`) or in an interactive session.

This is a *functional* test of a deployed build — distinct from the pytest suite
under `entrypoints/gateway-cli-v2/tests/`, which unit-tests the CLI's logic in
isolation. Run this after building and installing, to prove the packaged exe
talks to the gateway end to end.

## Prerequisites

- The suite installed on the target host (default path
  `C:\Program Files\GatewayCLI\gateway-cli.exe`; override with `-CliExe`).
- Network reachability to the baked gateway / admin-API / OIDC endpoints.
- One or more Cognito accounts whose passwords you can supply at call time.
  A **fully provisioned** account must belong to a Cognito group that maps to a
  gateway plan — otherwise Virtual-Key provisioning returns *"no group mapping
  found"* and login fails. (An account with no group mapping is a valid negative
  case; see `ExpectProvisioned=$false` below.)

## Running

```powershell
.\Invoke-GatewayCliE2E.ps1 -Users @(
    @{ Email='clitest@example.com'; Password=$env:CLITEST_PW; ExpectProvisioned=$true },
    @{ Email='hhkang@amazon.com';   Password=$env:HHKANG_PW;  ExpectProvisioned=$true }
) -Model 'claude-sonnet-4-6'
```

Exits `0` only when every account meets its expectation.

### `-Users` hashtable

| Key                 | Meaning |
|---------------------|---------|
| `Email`             | Cognito login email. |
| `Password`          | Supplied at call time — **never** hardcode in a committed file. Pull from a secret store, CI variable, or admin-reset temp value. |
| `ExpectProvisioned` | `$true` (default when set): login+setup+verify must all pass and the critical checks + `vk-cache` must read `ok`. `$false`: asserts the account authenticates with Cognito but is cleanly rejected at VK provisioning (surfaces *"no group mapping"* / *"Login failed"* rather than crashing). |

### What "PASS" checks

For a provisioned account, these `verify` checks must be `ok`:
`gateway-proxy`, `admin-api`, `oidc-tokens`, `claude-settings`, `api-key-helper`,
plus `vk-cache`. The proxy checks (`http-proxy`/`https-proxy`) are intentionally
**not** asserted: on a host with no corporate forward proxy they warn against the
baked expected-proxy placeholder, which is a config note, not a functional failure
(`verify` exits `0` on `warn`).

Beyond `verify`, the account must also:

- **Credential helper** (`Helper` column) — running `api-key-helper.exe` (with the
  `settings.json` env block injected, exactly as Claude Code invokes it) exits `0`
  and prints a **single bare token** (the Virtual Key) on stdout. Empty, multi-line,
  or a non-zero exit fails the gate. This is the functional check `verify` skips —
  it only confirms the helper *file* exists.
- **Live inference** (`Inference` column) — a minimal `/v1/messages` POST with that
  token is **classified**, not pass-only-on-`200`:
  - `pass` — HTTP `200`.
  - `fail` — a transport/TLS failure (no HTTP response) **or** an auth rejection
    (`401`/`403`). Both mean the credential + routing path the installer owns is
    broken.
  - `warn` — any other backend error (e.g. a `5xx` from a **known-degraded dev
    inference backend**). The gateway accepted the credential and routed the
    request, so the client path is proven; the backend fault is out of the
    installer's scope and does **not** fail the gate.

### Scope: SSM / SYSTEM profile

`login` caches, `HKCU` env vars, and `~/.claude/settings.json` are **user-scoped**.
Under SSM the harness runs as `SYSTEM`, so it validates `SYSTEM`'s profile. Because
the *entire* journey (login → setup → verify → helper → inference) runs as one
identity, what it exercises is internally consistent. Real users run the elevated
installer once, then use Claude Code as themselves — per product decision that is a
no-op difference, so this gate intentionally does **not** load a separate
target-user profile. Treat an SSM/SYSTEM run as an installed-artifact functional
smoke test, not a per-user profile audit.

## Running remotely over SSM

The harness runs as `SYSTEM` under `AWS-RunPowerShellScript`. A wrapper that
downloads it and invokes it:

```powershell
$ErrorActionPreference = 'Continue'   # stderr progress must not become a terminating error
$exe = 'C:\Program Files\GatewayCLI\gateway-cli.exe'
$dst = "$env:TEMP\Invoke-GatewayCliE2E.ps1"
Invoke-WebRequest -UseBasicParsing -Uri "<presigned-s3-url>" -OutFile $dst
& $dst -Users @(
    @{ Email='clitest@example.com'; Password='<temp>'; ExpectProvisioned=$true },
    @{ Email='hhkang@amazon.com';   Password='<temp>'; ExpectProvisioned=$true }
) -Model 'claude-sonnet-4-6'
Write-Host "HARNESS_EXITCODE=$LASTEXITCODE"
```

Note: SSM command parameters are recorded in command history / CloudTrail, so
passwords passed this way are visible there. Use short-lived admin-reset temp
passwords and rotate/disable them afterward.

## Design notes (Windows PowerShell 5.1)

The harness targets Windows PowerShell 5.1 — what SSM `RunPowerShellScript` uses:

- **No ternary operator, no `Set-StrictMode`.** A missing verify-check key must
  read as `$null` and fail its assertion gracefully, not throw.
- **ASCII-only.** PS 5.1 reads a BOM-less UTF-8 file as ANSI, so non-ASCII bytes
  (em-dashes, etc.) corrupt the parse. Keep this file ASCII.
- **Native stderr is captured via `Start-Process` + file redirection, not a
  pipe.** `gateway-cli` logs progress to stderr, and `verify`'s `verify_complete`
  JSON is itself a stderr line. Capturing that through `2>&1 | Out-String` is
  unusable: under a `Stop` preference it becomes a terminating
  `NativeCommandError`, and even under `Continue` each stderr line is wrapped as
  an `ErrorRecord` and re-flowed to the console width — shredding the single-line
  JSON so it can never be parsed. `Invoke-Cli` runs the exe with
  `-RedirectStandardError`/`-RedirectStandardOutput` to temp files (raw bytes, no
  reformatting) and feeds the headless password via `-RedirectStandardInput`
  (getpass cannot read a piped console handle on a headless Windows console, which
  is why `login` needs `--password-stdin`).
- **The `/v1/messages` body is built as a literal string, not `ConvertTo-Json`.**
  PS 5.1's `ConvertTo-Json` unwraps a single-element array, so a one-message
  `messages` array would serialize as an object and the API would reject it. The
  model alias is validated (no quotes/backslashes), so direct interpolation is safe.
- **`Start-Process -ArgumentList @()` errors on an empty array**, so `Invoke-Cli`
  omits `ArgumentList` entirely when there are no args — that is how it runs
  `api-key-helper` with no flags.
- **Inference status on a non-2xx comes from `$_.Exception.Response`.** `Invoke-WebRequest`
  throws on non-2xx in PS 5.1; the HTTP status is read off the `WebException`'s
  `.Response.StatusCode`. A `$null` `.Response` means a transport/TLS failure (a hard
  client fault), which is why the classifier distinguishes the two.
