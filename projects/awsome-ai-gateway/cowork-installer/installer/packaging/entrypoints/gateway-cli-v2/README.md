# gateway-cli-cowork

Setup wizard for LLM Gateway **end users** on **Cowork (Claude Desktop)**. Reads
the admin-provided **onboarding card** and handles password change, OIDC login,
and Claude Desktop gateway configuration.

> **Cowork (Claude Desktop) only.** Claude Desktop reads its gateway config from
> an OS-native channel (Windows registry policy `HKCU\SOFTWARE\Policies\Claude`
> / macOS `configLibrary`), **not** Claude Code's `settings.json`. `setup` writes
> that channel and relaunches the app. Claude Code has its own installer
> (`installer/`) and is out of scope.

## Build the wheel

Requires Python 3.11+ and [Poetry](https://python-poetry.org/docs/#installation).

```bash
cd gateway-cli-v2
poetry install --no-root   # first time
poetry build               # → dist/gateway_cli_v2-<version>-py3-none-any.whl
```

Distribute the `.whl`. Bump `version` in `pyproject.toml` first. For the frozen
Windows/macOS installer see `installer/packaging/build.ps1` + `installer.iss`.

## Onboarding card

The admin runs `setup/make_onboarding_card.py` → `onboarding-card-<email>.json`:

| Section | Keys |
|---|---|
| `admin` | `email`, `tempPassword` |
| `authentication` | `hostedUiUrl`, `oidcIssuerUrl`, `oidcClientId` |
| `infra` | `gatewayUrl`, `adminApiUrl` |

The CLI auto-locates it (current dir, `~/Downloads`, `~/.gateway-cli/`).

## Prerequisite: Claude Desktop

> **Install Claude Desktop and complete its first-run sign-in _before_ `setup`.**
> A managed-config change takes effect only at launch, so `setup` force-quits and
> relaunches the app. (Credential rotation alone needs no relaunch.)

The installer treats Claude Desktop as a prerequisite — it never installs or
removes the app.

## Golden path

```bash
gateway-cli-cowork login     # locate card → temp-password change → OIDC login
gateway-cli-cowork setup     # install corporate CA + write managed config + relaunch
gateway-cli-cowork verify    # health-check the setup end to end
```

### Commands

| Command | What it does |
|---|---|
| `login` | Locate card, complete Cognito temp-password change if needed, run OIDC PKCE browser login. Tokens cached at the OS data dir (`oidc-tokens.json`, mode 0600). |
| `setup` | Install corporate CA into the OS store **first** (aborts before writing config on a fingerprint-pin mismatch), then write the 6 gateway keys to the OS-native managed config (preserving pre-existing org values) and relaunch. Flags: `--base-url`, `--model`, `--available-models`, `--credential-kind`, `--api-key-helper`, `--api-key`, `--force`, `--skip-ca`, `--no-relaunch`. |
| `verify` | Print the live managed config, then check: config channel, HKLM precedence trap, inference + egress-proxy reachability, CA trust/fingerprint pin, and (helper-script kind) api-key-helper token health. |
| `disable` | Revert the managed config to its exact pre-setup state (removes only keys this tool wrote, restores any it overwrote). Does **not** remove the CA. |
| `relaunch` | Force-quit + relaunch Claude Desktop (Windows: MSIX AUMID via `shell:AppsFolder`; macOS: quit/reopen). |
| `ca check` / `ca restore` | Report CA trust (read-only) / remove only the CA `setup` installed. |
| `logout` | Clear cached OIDC tokens + VK cache. |
| `locate-card` | Find the onboarding card on disk. |
| `password` | Complete the Cognito `NEW_PASSWORD_REQUIRED` challenge (also part of `login`). |
| `version` | Print the CLI version. |

> **CA install is part of `setup`** — there is no standalone `ca install`. Off
> the corporate egress proxy (no CA present), `setup` skips the CA step with a
> note; the inference URL is publicly trusted.

## Managed config keys

`setup` writes to `HKCU\SOFTWARE\Policies\Claude` (Windows, `REG_SZ`) or
`configLibrary/<uuid>.json` (macOS):

- `inferenceProvider` — `gateway` (selects 3P mode)
- `inferenceGatewayBaseUrl` — Cowork inference base URL (HTTPS)
- `inferenceModels` — model roster (first entry is default)
- credential wiring — `inferenceCredentialHelper` (absolute path to
  `api-key-helper.exe`, production) or a static VK

The write is ownership-aware: `disable` reverts only these keys and leaves any
pre-existing org policy intact.

## Uninstall / cleanup

- `disable` — revert managed config.
- `ca restore` — remove the CA `setup` installed.
- `logout` — clear cached tokens + VK.
- **Windows uninstaller** (`unins000.exe` / Add-Remove Programs) — remove
  binaries, PATH entry, ARP registration.

See `docs/cowork-uninstall-implementation-plan.md` for the planned consolidated
`clear` / `uninstall` commands.
