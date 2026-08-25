# Windows EXE / Installer Packaging

Builds `gateway-cli-v2` into a **self-contained Windows installer** for
air-gapped machines with **no Python**. PyInstaller embeds the CPython 3.11+
runtime and all dependencies, so the target needs only Windows x64.

## What gets built

```
dist/
├── gateway-cli-cowork-suite/               # PyInstaller onedir
│   ├── gateway-cli-cowork.exe              # cli.main:main
│   ├── api-key-helper.exe                  # api_key_helper.main:main
│   └── _internal/                          # ONE shared runtime + deps
└── installer/
    └── gateway-cli-cowork-setup-<version>.exe   # single offline installer
```

Both exes share one `_internal/` runtime (onedir) — smaller installer, faster
startup (no per-run temp self-extraction), fewer endpoint-protection conflicts.

## Files

| File | Purpose |
|---|---|
| `entrypoints/*.py` | Script shims for the `[tool.poetry.scripts]` entries |
| `gateway_cli.spec` | PyInstaller spec: 2 console EXEs, one shared `COLLECT` |
| `installer.iss` | Inno Setup 6 → single `setup.exe` with PATH handling |
| `build.ps1` | Build pipeline: venv → pip → PyInstaller → smoke test → ISCC |
| `download_wheels.ps1` | Optional wheel pre-fetch for an offline build machine |

## Build

PyInstaller can't cross-compile — build on **Windows x64** (VM/CI fine).
Requires **Python 3.11+** and **Inno Setup 6** (skip with `-SkipInstaller` to
ship the suite as a zip). Copy `packaging/` next to `pyproject.toml` and `src/`.

```powershell
# From the repo root
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

Builds a throwaway `.build-venv`, pip-installs the project (poetry-core backend
— Poetry not needed), runs PyInstaller, smoke-tests each exe with `--help`, and
compiles the installer.

**Offline build machine:** pre-fetch wheels on a connected box with the *same*
Windows/Python version, then point the build at them:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\download_wheels.ps1 -OutDir C:\wheels
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -WheelDir C:\wheels
```

## Install

Transfer the single `gateway-cli-cowork-setup-<version>.exe`.

- **Interactive:** double-click. All-users, admin-only install to the fixed
  path `C:\Gateway-CLI-Cowork` (dir-selection page hidden). The Welcome page
  shows the overview + required `setup` follow-up (see below).
- **Silent (SCCM/Intune/GPO):** `gateway-cli-cowork-setup-<version>.exe /VERYSILENT /NORESTART`
- Install dir is added to **PATH** automatically (system + user); removed on
  uninstall (*Apps & Features*). Re-run a newer setup.exe to upgrade (same `AppId`).

## Wizard Welcome page

The Welcome page doubles as the "read this first" overview via the built-in
`WelcomeLabel2` message in `installer.iss` `[Messages]` — no extra page/click.

> **Gotcha:** Inno 6 hides Welcome by default. `installer.iss` sets
> `DisableWelcomePage=no` to bring it back — remove that line and the overview
> silently disappears.

- `%n` = line break; `[name/ver]` = `AppName`+`AppVersion`.
- **ASCII only** (inline message, not a UTF-8 file — non-ASCII → mojibake).
  For rich/localized text switch to `InfoBeforeFile=overview.rtf`.
- **Keep it truthful:** it must match actual behaviour (fixed path, auto PATH,
  admin-only, HKLM/HKCU scope on the Tasks page). Update it in the same edit
  that changes `[Setup]`/`[Tasks]`.

## Code-signing

`build.ps1` signs every `.exe` with Authenticode (SHA-256 + RFC 3161 timestamp)
and verifies with `signtool verify /pa`. Provide a credential one of two ways:

```powershell
# Cert in the Windows store / HSM / token, by SHA-1 thumbprint
powershell ... build.ps1 -SignThumbprint <THUMBPRINT>
# Or a PFX + password (dev certs)
powershell ... build.ps1 -SignPfxFile <path.pfx> -SignPfxPassword <pw>
```

Env equivalents: `GATEWAY_CLI_SIGN_THUMBPRINT`, `GATEWAY_CLI_SIGN_PFX`,
`GATEWAY_CLI_SIGN_PFX_PASSWORD`. Timestamp server via `-TimestampUrl`,
`signtool.exe` via `-SignToolPath`. With no credential the build still succeeds
but ships **unsigned** — fine for testing, but a locked-down fleet must sign to
avoid AV/SmartScreen quarantine. UPX is disabled in the spec for the same reason.

## Isolated-network notes

- **Internal CA:** the bundle ships certifi's public CAs. For an internal
  corporate CA, set `REQUESTS_CA_BUNDLE` / `AWS_CA_BUNDLE` to the PEM, or bake
  it into the bundle in `gateway_cli.spec`.
- **boto3:** with no route to AWS, point boto3 at internal `endpoint_url`s.
- **OS floor:** CPython 3.11+ supports Windows 8.1 / Server 2012 R2+; build with
  the oldest patch line you must support.

## Maintenance

- New dependency: auto-picked-up, but if it loads data/plugins dynamically add a
  `collect_data_files(...)` / `collect_submodules(...)` line in the spec.
- New console script: add a shim in `entrypoints/`, an `Analysis`/`PYZ`/`EXE`
  trio + `COLLECT` entry in the spec, and the exe to `build.ps1`'s smoke test.
- Version: edit `pyproject.toml` (or override with `-Version`).
