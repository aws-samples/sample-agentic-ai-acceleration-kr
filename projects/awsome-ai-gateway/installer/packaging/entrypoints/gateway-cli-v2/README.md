# gateway-cli

A setup wizard for LLM Gateway **end users**. The corporate endpoints (gateway URL,
admin API, OIDC issuer/client, CA bundle) are **baked into the build**, so the user
runs one command — `gateway-cli onboard` — to log in, configure Claude Code, and
verify the result. No admin-provided file is needed.

---

## Building the wheel

The `.whl` file is what you distribute to end users. Build it from this directory using Poetry.

**Prerequisites:** Python 3.11+, [Poetry](https://python-poetry.org/docs/#installation)

```bash
cd gateway-cli-v2

# Install build dependencies (first time only)
poetry install --no-root

# Build the wheel and sdist into dist/
poetry build
```

The output appears in `dist/`:

```
dist/
  gateway_cli_v2-0.1.0-py3-none-any.whl   ← distribute this to end users
  gateway_cli_v2-0.1.0.tar.gz
```

To bump the version before building, update `version` in `pyproject.toml` first.

> The wheel file name encodes the version (e.g. `gateway_cli_v2-0.1.0-py3-none-any.whl`). Update the filename in [install.md](install.md) whenever the version changes.

---

## Installation

See [install.md](install.md) for full platform-specific instructions (Windows, WSL, macOS, Linux).

> **Important:** Install Claude Code and complete its first-run wizard **before** running `gateway-cli onboard`. If Claude Code's first-run wizard runs after onboarding, it overwrites `~/.claude/settings.json` with defaults and clears the gateway configuration.

---

## Baked configuration

The corporate endpoints are compiled into the build (`cli/site_defaults.py`, overlaid
at build time by `cli/_site_config.py` from `packaging/site-config.json`), so the user
does not supply any of them:

| Value | Source |
|---|---|
| `gatewayUrl` | baked (`ANTHROPIC_BASE_URL`) |
| `adminApiUrl` | baked (`ADMIN_API_URL` / `GATEWAY_CLI_GATEWAY_URL`) |
| `oidcIssuerUrl`, `oidcClientId` | baked (Cognito OIDC) |
| CA bundle | baked (`NODE_EXTRA_CA_CERTS`) |

Any baked value can be overridden at runtime by the matching `GATEWAY_CLI_*` environment
variable or command flag (`--gateway-url`, `--admin-api-url`, `--oidc-issuer-url`,
`--oidc-client-id`), which is how staging/dev builds are pointed at a different
environment. Run `gateway-cli config --explain` to print the resolved values and where
each one came from.

---

## Wizard steps

Run everything at once with the `onboard` command (recommended), or run each step
individually for troubleshooting.

```bash
gateway-cli onboard
```

| # | Step | Individual command |
|---|------|--------------------|
| 1 | OIDC login (browser PKCE, or headless email+password) | `gateway-cli login` |
| 2 | Write Claude Code user settings | `gateway-cli setup --model <alias>` |
| 3 | End-to-end health check | `gateway-cli verify` |

### Step 1 — login

Opens the browser to the Cognito Hosted UI (PKCE Authorization Code flow) and, on
success, exchanges the id_token for a Virtual Key. Tokens are cached at
`~/.gateway-cli/oidc-tokens.json` (mode 0600); the VK is cached alongside.

```bash
gateway-cli login
```

On a headless host with no browser, log in with email + password instead. The password
is read from the first line of stdin so it never appears in the process table or shell
history:

```bash
gateway-cli login --no-browser --email me@company.com --password-stdin < pw.txt
```

### Step 2 — setup

Merges gateway settings into `~/.claude/settings.json`
(`%USERPROFILE%\.claude\settings.json` on Windows):

- `apiKeyHelper` — path to the `api-key-helper` binary
- `model` — the model alias from `--model` (default `claude-sonnet-4-6`); must be a
  gateway alias, never a Bedrock inference-profile id (`us.anthropic.*`)
- `env.ANTHROPIC_BASE_URL` — gateway proxy URL (routes all Claude Code API calls)
- `env.ADMIN_API_URL` — admin API URL (primary key read by `api-key-helper`)
- `env.GATEWAY_CLI_GATEWAY_URL` — same as `ADMIN_API_URL` (legacy alias, kept for compatibility)
- `env.OIDC_ISSUER_URL` / `env.OIDC_CLIENT_ID` — Cognito OIDC config

`--model` is optional (defaults to `claude-sonnet-4-6`; override with the flag or
`GATEWAY_CLI_MODEL`). The original settings file is backed up first, and conflicting
keys that would bypass the gateway (e.g. `CLAUDE_CODE_USE_BEDROCK`,
`ANTHROPIC_API_KEY`) are removed.

```bash
gateway-cli setup --model claude-sonnet-4-6
```

### Step 3 — verify

Runs six checks and reports pass/warn/fail for each:

1. Gateway proxy reachable (`GET <gatewayUrl>/health`)
2. Admin API reachable (`GET <adminApiUrl>/health`)
3. OIDC tokens cached and not expired (`oidc-tokens.json`)
4. Virtual Key cache exists and not expired (`vk-cache.json`)
5. `~/.claude/settings.json` has `apiKeyHelper` + `ANTHROPIC_BASE_URL`
6. `CLAUDE_CODE_USE_BEDROCK` is not set (would bypass the gateway)

Also prints where each gateway-related env var (`OIDC_ISSUER_URL`, `OIDC_CLIENT_ID`, `ADMIN_API_URL`, `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_USE_BEDROCK`) is defined — process env, `settings.json`, shell profiles, or Windows registry — with the exact file path and line number for easy debugging.

```bash
gateway-cli verify
```

## Teardown — clear / uninstall

Two commands split along "can the running process safely do it?" (see
`docs/TEARDOWN_CLEAR_UNINSTALL_PLAN.md`):

- **`clear`** — reverts everything *software-level* setup/login wrote, in the safe
  order: managed settings → gateway-cli's keys in `~/.claude/settings.json`
  (pre-setup values restored from the earliest backup snapshot) → persisted OS env
  vars (`HKCU\Environment` restore-or-delete / shell-rc line removal) → OIDC
  tokens + VK cache → this tool's own backup snapshots. Runs unelevated; only the
  managed-settings step may need an admin shell — on a permission error the
  user-scope steps still complete and the elevated one-liner is printed.
  Flags: `--keep-tokens`, `--keep-os-env`, `--dry-run`, `--yes`.
- **`uninstall`** — removes the binaries by delegating to the Inno `unins000.exe`
  (elevated + detached; a running exe can never delete its own image). Resolves the
  uninstaller from Add/Remove Programs by GUID-substring (tolerating the Inno
  `}}_is1` key quirk) and validates it before elevating. Use `--clear-first` to run
  `clear` in-process before the exe it runs from is removed.
- **`verify --post-teardown`** — the inverse gate: asserts every surface `clear`
  owns is gone/reverted (exits 1 on residue).

`disable` and `logout` remain the narrow single-surface verbs.

```bash
gateway-cli clear --dry-run     # show the plan
gateway-cli clear -y            # revert software state
gateway-cli uninstall --clear-first   # full removal in one step (Windows)
gateway-cli verify --post-teardown    # prove nothing is left
```
