# gateway-cli-v2 — Windows install & build

Turns `gateway-cli-v2` into a **single self-contained Windows installer** for
users on isolated (air-gapped) networks with **no Python installed**.
PyInstaller embeds CPython 3.11+ and every dependency (`click`, `boto3`,
`requests`, `structlog`, `PyYAML`, …) into the bundle, so the target PC needs
nothing but **Windows x64**.

Two audiences:
- **[For users](#for-users)** — install the `.exe` and run `gateway-cli setup`.
- **[For maintainers](#for-maintainers)** — build the installer and bake in the
  site's endpoints.

The three CLIs (`gateway-cli.exe`, `api-key-helper.exe`, `statusline.exe`) share
one `_internal/` Python runtime — smaller installer, faster start, no per-run
temp extraction (which also plays nicer with endpoint-protection software).

---

## For users

You receive a single file: **`gateway-cli-setup-<version>.exe`** (e.g. on your
Desktop). No Python or other prerequisite is needed.

### 1. Install

```powershell
# Interactive: double-click the .exe
# Silent / mass deployment (SCCM, Intune, GPO):
gateway-cli-setup-0.1.0.exe /VERYSILENT /NORESTART
```

- Admins install to `C:\Program Files\GatewayCLI`; non-admins can pick a per-user
  install.
- "Add to PATH" (on by default) makes `gateway-cli`, `api-key-helper`, and
  `statusline` available in any **new** terminal.

### 2. Configure Claude Code

Open a **new terminal (Run as administrator** — `setup` writes the machine-wide
`managed-settings.json`):

```powershell
gateway-cli login                              # OIDC browser login
gateway-cli setup --model claude-sonnet-4-6    # apply gateway config; pick the model
claude                                         # ready to use
```

`--model` is normally the only value you choose — the site's endpoints
(gateway / admin API / OIDC) are baked into the build. If they are **not**
baked (a generic build), `setup` will ask you to pass them explicitly:
`--gateway-url … --admin-api-url … --oidc-issuer-url … --oidc-client-id …`.

Optional:

```powershell
gateway-cli setup --available-models claude-sonnet-4-6,claude-haiku-4-5,claude-opus-4-6
gateway-cli verify        # health / config check
gateway-cli env           # show the effective environment
```

### 3. Remove or upgrade

- **Uninstall:** *Apps & Features* → "LLM Gateway CLI". PATH entries are removed
  automatically. To also revert Claude Code settings first, run
  `gateway-cli clear` (unelevated user scope) and `gateway-cli disable` (elevated).
- **Upgrade:** run a newer `setup.exe` over the old install — same `AppId`, so
  Windows treats it as the same product.

---

## For maintainers

### Build machine requirements

PyInstaller does **not** cross-compile — build on **Windows x64** (VM or CI is
fine):

1. **Python 3.11+** (matches `entrypoints/gateway-cli-v2/pyproject.toml`).
2. **Inno Setup 6** — https://jrsoftware.org/isdl.php
   (optional: `-SkipInstaller` ships `dist\gateway-cli-suite` as a folder/zip).
3. This repository checked out with `entrypoints/gateway-cli-v2/src` present.

### 1. Bake in the site's endpoints (important)

A **bare build bakes nothing except the model default** (`claude-sonnet-4-6`).
To ship a build that "just works" for users, supply the site values one of
these ways (highest precedence first):

```
-Param  >  GATEWAY_CLI_DEFAULT_* env var  >  packaging\site-config.json  >  (blank)
```

**Easiest — edit `packaging\site-config.json`** (backslashes escaped as `\\`):

```json
{
  "oidcIssuerUrl": "https://<issuer>/oauth2/default",
  "oidcClientId":  "<client-id>",
  "gatewayUrl":    "https://gateway.example.com",
  "adminApiUrl":   "https://api.gateway.example.com",
  "caBundle":      "C:\\corp-proxy-ca.pem"
}
```

**Or pass build params** (override the JSON):

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 `
    -OidcIssuerUrl https://<issuer>/oauth2/default -OidcClientId <client-id> `
    -GatewayUrl https://gateway.example.com -AdminApiUrl https://api.gateway.example.com `
    -CaBundle C:\corp-proxy-ca.pem
```

Notes:
- `site-config.json` maps **5 keys only** (`oidcIssuerUrl`, `oidcClientId`,
  `gatewayUrl`, `adminApiUrl`, `caBundle`). The three **proxy** values
  (`-ExpectedProxyUrl`, `-NoProxyValue`, `-ForbiddenNoProxyToken`) can be set
  **only** via `-Param` or `GATEWAY_CLI_DEFAULT_*` env vars.
- Any value left blank stays blank — the corresponding Claude Code key is simply
  **not written** (blank ⇒ absent, not `KEY=""`).
- `site-config.json` is `.gitignore`d — never commit environment identifiers.
- Full catalog of every config item, its source, and bare-build behaviour:
  **`entrypoints/gateway-cli-v2/docs/CONFIG_ITEMS_AND_DEFAULTS.md`**.

### 2. Build

From the repository root, in PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

The script creates a throwaway `.build-venv`, pip-installs the project (pip reads
the poetry-core backend directly — Poetry itself is not needed), runs PyInstaller,
smoke-tests each exe with `--help`, and compiles the installer. Output:

```
dist\
├── gateway-cli-suite\                     # PyInstaller onedir (3 exes + shared _internal\)
└── installer\
    └── gateway-cli-setup-<version>.exe    # single offline installer
```

Version defaults to `pyproject.toml`; override with `-Version`.

### 3. Air-gapped build machine

Pre-fetch wheels on a connected machine with the **same Windows/Python version**,
copy the cache + repo across, then point `build.ps1` at it:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\download_wheels.ps1 -OutDir C:\wheels
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -WheelDir C:\wheels
```

### 4. Code-signing (required for production fleets)

Unsigned PyInstaller exes are a common AV/SmartScreen false-positive (this can
block download/PATH steps outright). `build.ps1` signs all three exes (before
ISCC) **and** the setup.exe (after ISCC) with Authenticode (SHA-256 + RFC-3161
timestamp) and verifies each with `signtool verify /pa` when a credential is
supplied:

```powershell
# Cert in the Windows store / HSM / token, by SHA-1 thumbprint (enterprise norm):
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -SignThumbprint <THUMBPRINT>

# Or a PFX file + password (dev / non-HSM certs):
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -SignPfxFile C:\certs\corp.pfx -SignPfxPassword <pw>
```

Env-var equivalents: `GATEWAY_CLI_SIGN_THUMBPRINT`, `GATEWAY_CLI_SIGN_PFX`,
`GATEWAY_CLI_SIGN_PFX_PASSWORD`. Override the timestamp server with
`-TimestampUrl` (default `http://timestamp.digicert.com`); point at a specific
`signtool.exe` with `-SignToolPath`. With **no** credential the build still
succeeds but ships **unsigned** binaries — fine for internal testing only.

### 5. Inject extra site config (optional)

`setup` deep-merges an optional operator JSON into Claude Code's settings. Copy
the example, edit, and rebuild — `build.ps1` bundles it:

```powershell
Copy-Item packaging\site-extra.json.example packaging\site-extra.json
```

```jsonc
{
  "managed": {                 // merged into managed-settings.json (highest tier)
    "env": { "HTTPS_PROXY": "http://proxy.example.com:8080" },
    "permissions": { "allow": ["Bash(git*)"] }
  },
  "user": {                    // merged into ~/.claude/settings.json
    "env": { "MY_TEAM_FLAG": "1" }
  }
}
```

gateway-cli's own keys (OTEL, `ANTHROPIC_BASE_URL`, `apiKeyHelper`,
`availableModels`, …) are applied **after** site-extra, so it can never break
routing. Every injected key is recorded under a `_gatewayCli` marker so
`gateway-cli disable` unmerges it cleanly. Absent file ⇒ no-op. Also `.gitignore`d.

### 6. User settings are backed up

Before modifying any file, `setup` writes a timestamped snapshot to
`%LOCALAPPDATA%\gateway-cli\backups\` (0700, owner-only), so existing user/org
settings are never lost. Restore by copying the desired `.bak` back to its
original path. `GATEWAY_CLI_BACKUP_DIR` overrides the location.

### 7. Config catalog (`manifest.py`)

Every Claude Code config key the tool knows about is declared in one place:
`entrypoints/gateway-cli-v2/src/cli/manifest.py`. It is a **declarative catalog +
resolver** — it does *not* write anything itself (that stays in `managed.py` /
`site_extra.py`); it is the single source for "what we touch, where it lives, and
who wins."

Main pieces:

| Class / data | What it is |
|---|---|
| `ConfigField` + `FIELDS` | One config key and how the tool treats it; `FIELDS` is the full catalog |
| `Output` | A concrete `(placement, tier)` target a field is written to (a field may have several) |
| `Category` / `Placement` / `Tier` / `Status` | Labels on a field: what area it controls, where it lives, which settings tier, and our relationship (`OWNED` / `PASSTHROUGH` / `BYPASS` / `DOCUMENTED`) |
| `ValueKind` / `Compose` | The value's typed shape (str/url/path/list/json…) and how sources combine (replace / merge) |
| `Location` + `LOCATIONS` | Per-OS filesystem paths (data dir, managed root, user settings, caches) |
| `Tierlevel` + `SETTINGS_HIERARCHY` | Claude Code's settings precedence, highest first |
| `PrecedenceRule` + `PRECEDENCE` | The non-obvious "who wins" cases (OTEL, `NO_PROXY`, proxies) |
| `ResolvedConfig` + `SETUP_REQUIRED_KEYS` | The effective gateway/OIDC values for one command, and the keys `setup` requires |
| `by_key` / `by_category` / `owned` / `bypass_keys` / `sensitive_keys` / `os_persisted_keys` | Lookup helpers other modules call instead of hardcoding literals |

**To manage it:**
- **Add / change a key** → edit the `FIELDS` tuple. Give it a `key`, `category`,
  `placement`, `status`, and (for `OWNED`) the `outputs`, plus `flag`,
  `baked_from`, and/or `env_override` as needed.
- **Names must match the wiring:** `baked_from` matches a `DEFAULT_*` constant /
  `GATEWAY_CLI_DEFAULT_*` build var; `env_override` matches the `GATEWAY_CLI_*`
  runtime var; `flag` matches the Click option.
- **Keep it in sync** with the runtime docs (`docs/FILE_AND_ENV_OPERATIONS.md`,
  `PROXY_PRECEDENCE.md`, `OTEL_PRECEDENCE.md`) and the summary in
  `docs/CONFIG_ITEMS_AND_DEFAULTS.md` — the manifest documents the surface, it
  does not re-derive runtime behaviour.
- **Run the tests** after any edit (`pytest` under `entrypoints/gateway-cli-v2`):
  drift guards check the catalog against the CLI flags and the writer.

### 8. Maintenance notes

| File | Purpose |
|---|---|
| `entrypoints/*_entry.py` | PyInstaller entry shims (mirror `[tool.poetry.scripts]`) |
| `gateway_cli.spec` | PyInstaller spec: 3 console exes, one shared `COLLECT` |
| `installer.iss` | Inno Setup 6 script → single offline `setup.exe` + PATH handling |
| `build.ps1` | One-command pipeline (venv → pip → PyInstaller → smoke test → ISCC) |
| `download_wheels.ps1` | Optional wheel pre-fetch for an offline build machine |
| `site-config.json` | Site's baked endpoints (§1) — `.gitignore`d |
| `site-extra.json.example` | Extra-config injection template (§5) |

- New dependency in `pyproject.toml`? Picked up automatically; if it loads data
  files/plugins dynamically, add `collect_data_files(...)` / `collect_submodules(...)`
  in `gateway_cli.spec`.
- New console script in `[tool.poetry.scripts]`? Add a shim in `entrypoints/`, an
  `Analysis`/`PYZ`/`EXE` trio in the spec, add it to `COLLECT`, and extend the
  smoke-test list in `build.ps1`.
- Bump the product version in `pyproject.toml` (or `-Version`).
