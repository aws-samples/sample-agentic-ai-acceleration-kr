# File & environment-variable operations

> 🇰🇷 한국어: [`FILE_AND_ENV_OPERATIONS.ko.md`](./FILE_AND_ENV_OPERATIONS.ko.md)

Every file and OS-environment mutation `gateway-cli` performs across its full
lifecycle, per command. Read this before changing any write path — it is the
single reference for what the tool touches on a user's machine and what is
recoverable.

**Design rule:** `gateway-cli` manages its **own** config files (Claude Code
settings, its token caches) and **adds** its four operator env vars to the
*User* scope. It does **not** remove the user's system/OS
environment variables. A gateway-bypassing OS env var
(`CLAUDE_CODE_USE_BEDROCK`, `ANTHROPIC_API_KEY`, `ANTHROPIC_BEDROCK_BASE_URL`) is
**detected and reported** by `verify`, which prints step-by-step removal guidance
for both User and System (admin) scope — the user removes it themselves.

> **Known gaps between this rule and the current code** (each detailed in its
> section below, surfaced by an adversarial review of this doc):
> 1. ~~`verify`'s POSIX profile scan does not cover Linux/WSL system files~~ —
>    **fixed**: the scan now covers `/etc/*`, `/etc/environment`,
>    `/etc/profile.d/*.sh` on Linux/WSL and fish `set -x` syntax (Step 3).
> 2. `disable` takes **no backup** of `managed-settings.json` and cannot restore a
>    prior org value that occupied one of our keys (Other commands).
> 3. Windows env persistence **overwrites** existing `HKCU` values (no snapshot);
>    only the POSIX shell-rc write is truly additive (Step 2C).

Legend:

| Symbol | Meaning |
|---|---|
| ✏️ | write / create |
| 🔁 | overwrite or merge |
| 🗑️ | remove |
| 💾 | back up first (timestamped, non-overwriting) |
| 🌐 | in-process env only (this process; **not** persisted) |

---

## Key locations

| What | Path |
|---|---|
| **Data dir** (`<data_dir>`) | macOS `~/Library/Application Support/gateway-cli/` · Linux `~/.local/share/gateway-cli/` · Windows `%LOCALAPPDATA%\gateway-cli\`. Override: `GATEWAY_CLI_DATA_DIR`. |
| **Backups dir** | `<data_dir>/backups/` (mode `0700`). Override: `GATEWAY_CLI_BACKUP_DIR`. |
| **Managed settings root** | Windows `C:\Program Files\ClaudeCode\` · macOS `/Library/Application Support/ClaudeCode/` · Linux/WSL-native `/etc/claude-code/` · WSL→Windows Claude `/mnt/c/Program Files/ClaudeCode/`. |
| **User settings** | `~/.claude/settings.json`. Override: `GATEWAY_CLI_SETTINGS_PATH`. |
| **Token / VK caches** | `<data_dir>/oidc-tokens.json`, `<data_dir>/vk-cache.json`. Overrides: `GATEWAY_CLI_OIDC_CACHE`, `GATEWAY_CLI_VK_CACHE`. |

---

## Cross-cutting — every command, at startup

`cli/main.py` + `gateway_cli_oidc/tls.py`, before any subcommand runs.

| Target | Op | Detail |
|---|---|---|
| `REQUESTS_CA_BUNDLE`, `SSL_CERT_FILE`, `AWS_CA_BUNDLE`, `CURL_CA_BUNDLE` | 🌐 ✏️ (`setdefault`) | `apply_ca_bundle()` — set **only if the corporate PEM exists locally**; never clobbers a value the user already set. In-process only, not persisted. |
| `PYTHONUTF8=1` (Windows only) | 🌐 ✏️ (`setdefault`) | Forces UTF-8 I/O so em-dashes/arrows/checkmarks render. In-process only. |
| `ssl.SSLContext` (runtime monkeypatch) | — | `enable_os_trust_store()` injects `truststore` process-wide. No file or env write. |

---

## Step 1 — `login` (OIDC PKCE, or headless email+password)

`cli/login.py`

| Target | Op | Detail |
|---|---|---|
| `<data_dir>/oidc-tokens.json` | 🔁 | `save_tokens()` — access / refresh / id tokens. `chmod 0600`. Overwritten each login. |
| `<data_dir>/vk-cache.json` | 🔁 | `save_vk_cache()` — Virtual Key from `/v1/auth/exchange`. `chmod 0600`. |

- **No backups** (caches are disposable/regenerable).
- No env vars touched.

---

## Step 2 — `setup` (write-heavy)

### A. Managed settings (highest tier) — `cli/managed.py`

| Target | Op | Detail |
|---|---|---|
| `…/ClaudeCode/managed-settings.json` | 💾 → 🔁 | Backed up (`claude-code-managed`) if it pre-existed, then **deep-merged**: our keys win, org keys preserved, a private `_gatewayCli` marker records what we own. Requires admin/sudo. |
| `…/managed-settings.d/50-gateway.json` | 💾 → 🔁 | Our drop-in fragment (beats the primary file). Backed up (`claude-code-managed-dropin`) if present, then overwritten. |
| `…/managed-settings.d/99-gateway.json` (legacy) | 💾 → 🗑️ | Stale fragment from earlier builds — backed up, then removed **only if it carries our marker**. |

POSIX writes go through `sudo tee` + `chmod 644` + `chown root`.

Keys written into the managed `env` block (via `build_gateway_env`):
`ANTHROPIC_BASE_URL`, `GATEWAY_CLI_GATEWAY_URL`, `NO_PROXY`, all `OTEL_*`
telemetry keys, and — when a CA bundle is configured — `NODE_EXTRA_CA_CERTS`,
`REQUESTS_CA_BUNDLE`, `AWS_CA_BUNDLE`, `SSL_CERT_FILE`. Plus top-level
`apiKeyHelper` and `statusLine`. (OTEL endpoint precedence: see
[`OTEL_PRECEDENCE.md`](../OTEL_PRECEDENCE.md).)

### B. User settings — `cli/setup.py`

| Target | Op | Detail |
|---|---|---|
| `~/.claude/settings.json` | 💾 → 🔁 | Backed up (`claude-code`) if present, then merged. |

Inside that file:

- ✏️/🔁 **written:** top-level `apiKeyHelper`, `model`, `availableModels` (when
  supplied); `env.ANTHROPIC_BASE_URL`, `env.ADMIN_API_URL`,
  `env.GATEWAY_CLI_GATEWAY_URL`, `env.OIDC_ISSUER_URL`, `env.OIDC_CLIENT_ID`;
  optional `env.ANTHROPIC_DEFAULT_{SONNET,OPUS,HAIKU}_MODEL`;
  `env.NODE_EXTRA_CA_CERTS` (setdefault); the `user.id` segment of
  `env.OTEL_RESOURCE_ATTRIBUTES` reconciled to the logged-in identity.
- 🗑️ **removed from this file** (via `reconcile_settings` — the single source of
  truth in `cli/reconcile.py`): `env.CLAUDE_CODE_USE_BEDROCK`,
  `env.ANTHROPIC_BEDROCK_BASE_URL`, `env.ANTHROPIC_API_KEY`, top-level
  `statusLine`, top-level `modelOverrides`. The timestamped backup above makes
  this reversible. **Only the settings.json copy is removed — never the OS/system
  env var of the same name.**

### C. Persisted OS environment variables — `cli/env.py`

On by default; disable with `--no-persist-env`.

| Target | Op | Detail |
|---|---|---|
| Windows `HKCU\Environment` (User scope) | 🔁 | `persist_env_vars()` → `_persist_windows()` writes `OIDC_ISSUER_URL`, `OIDC_CLIENT_ID`, `ADMIN_API_URL`, `ANTHROPIC_BASE_URL` as `REG_SZ` via `winreg.SetValueEx`. ⚠️ This is an **unconditional replace**: there is no read-before-write, so a pre-existing User value under any of these four names is **overwritten and lost** (no snapshot to recover it). |
| `~/.zshrc` **or** `~/.bashrc` (POSIX) | ✏️ | Idempotent `export` block appended; vars already present are skipped. Existing lines are never overwritten. |

- **User scope only.** Setup writes these four vars to the **User** scope and
  writes nothing to the System/Machine scope.
- **Additive on POSIX, replacing on Windows.** The shell-rc write skips vars that
  already exist; the Windows registry write does **not** — it clobbers any prior
  value for the four names. This is a known gap against the no-overwrite design
  rule (Windows persistence should back up or skip existing values, and does not).
- **No snapshot** of the registry / shell rc is taken. On POSIX this is safe
  (writes are additive); on Windows a clobbered prior value is unrecoverable.
- Setup does **not** remove any gateway-bypassing OS env var — that is surfaced
  by `verify` (Step 3) as manual guidance.

### D. State persistence for later steps — `cli/main.py`

| Target | Op | Detail |
|---|---|---|
| _(none)_ | — | **None.** setup persists no config for later steps. Endpoints are single-sourced from the build-time-baked defaults (`cli/site_defaults.py`), so `login`/`verify` resolve the same values `setup` did with no saved state. |

- **No config file bridges setup → login/verify.** Production builds bake their
  endpoints; staging/dev testing overrides via `GATEWAY_CLI_*` env vars, which
  persist across commands in the shell without a file.

---

## Step 3 — `verify`

`cli/verify.py`

**Read-only.** Scans the process env, `settings.json` env block, shell profiles
(POSIX) and the Windows registry (User + Machine scope). For any gateway-bypassing
variable found in the **OS/system environment**, it prints multi-level manual
removal guidance:

- **Windows** — the `[Environment]::SetEnvironmentVariable(..., $null, 'User')`
  and `...'Machine'` (Administrator) commands, the `sysdm.cpl` GUI path, which
  scope(s) were detected, and a reminder to open a new PowerShell.
- **POSIX/WSL** — the exact profile file + line holding the export (or a `grep`
  to find it), a `sudo` note for `/etc/*` files, and a restart/`source` reminder.

> **Coverage of the profile scan** (`_shell_profile_paths()` /
> `_scan_shell_profiles()`):
> - **User files scanned:** `~/.bashrc`, `~/.bash_profile`, `~/.profile`,
>   `~/.zshrc`, `~/.zprofile`, `~/.config/fish/config.fish`.
> - **System files scanned on macOS *and* Linux/WSL:** `/etc/zshrc`,
>   `/etc/zshenv`, `/etc/profile`, `/etc/bashrc`, `/etc/bash.bashrc`,
>   `/etc/environment` (PAM `KEY=VALUE` form), and every `/etc/profile.d/*.sh`
>   drop-in. (Previously these were scanned only on macOS — a bypass var exported
>   from a system file on Linux/WSL used to pass `verify` cleanly.)
> - **Syntax matched:** `export VAR=` / bare `VAR=` (covers `/etc/environment`),
>   and fish `set -x` / `--export` / `-gx` forms in `config.fish`.
>
> The process-env check still catches any such variable if it is exported into
> the shell that runs `verify`; the file scan additionally catches a persistent
> definition that a fresh Claude process would inherit but the current `verify`
> shell has not.

It **does not modify** anything.

---

## Other commands

| Command | Target | Op | Detail |
|---|---|---|---|
| `disable` | `managed-settings.json` | 🔁 / 🗑️ | Unmerge only our marked keys (from the `_gatewayCli` marker), then rewrite the reduced file; **delete the file** only if gateway-cli created it (`fileExisted=false`). A timestamped backup of the pre-removal file is snapshotted first (`_backup_existing`), so disable is reversible even in the delete-the-file branch. The marker records only owned key *names*, not prior values, so if an org value ever occupied one of our keys it is **popped, not restored** (recoverable from the snapshot). |
| `disable` | `50-gateway.json`, legacy `99-gateway.json` | 🗑️ | Marker-gated removal of our drop-ins (backed up first, label `claude-code-managed-dropin`). |
| `logout` | `oidc-tokens.json`, `vk-cache.json` | 🗑️ | `clear_tokens()` unlinks both. |
| `env --persist` | `HKCU\Environment` / shell rc | ✏️/🔁 | Same additive write as Step 2C. On Windows, any prior HKCU value about to be overwritten is snapshotted first (`gateway-cli-hkcu-env.Environment.<ts>.json.bak`). |
| `env` (no flag) | — | — | Prints values + shell snippets. Read-only. |
| `status` | — | — | Read-only. |
| `clear` | all of the above + backups | 🔁 / 🗑️ | Software-level teardown, in order: managed settings (like `disable`) → owned keys in `~/.claude/settings.json` (pre-setup values restored from the **earliest** `claude-code` snapshot) → persisted OS env vars (HKCU restore-or-delete from the earliest `gateway-cli-hkcu-env` snapshot; POSIX marker-block line removal) → tokens/VK (like `logout`) → sweep of this tool's own backup snapshots (strictly last). Unelevated; a managed-settings permission error completes the user-scope steps and prints the elevated `gateway-cli disable` one-liner. `--keep-tokens` / `--keep-os-env` / `--dry-run` / `--yes`. |
| `uninstall` | binaries, PATH, ARP key | 🗑️ (delegated) | Windows-only. Resolves `unins000.exe` from Add/Remove Programs (GUID substring + `_is1` — tolerates the Inno `}}_is1` double-brace quirk), validates it (bare `unins###.exe` path inside `GatewayCLI\`, DisplayName match), then launches it elevated + detached. Never deletes its own image. `--clear-first` runs `clear` in-process beforehand. |
| `verify --post-teardown` | — | — | Read-only inverse gate: asserts managed settings, settings.json owned keys, persisted OS env vars, token caches, and our backups are all gone/reverted; exits 1 on residue. |

---

## Backups — where recovery snapshots land

All **config** backups go to `<data_dir>/backups/` (mode `0700`), timestamped and
**never overwritten** (same-second collisions get a numeric suffix):

- `claude-code.settings.json.<ts>.bak`
- `claude-code-managed.managed-settings.json.<ts>.bak`
- `claude-code-managed-dropin.50-gateway.json.<ts>.bak`
- `gateway-cli-hkcu-env.Environment.<ts>.json.bak` — prior HKCU values the Windows
  env persist was about to overwrite (T6.2 read-before-write). `clear` restores
  from the **earliest** of these (the true pre-setup state).

**Not backed up:**

- Token / VK caches (disposable — regenerated by `login`).
- The POSIX OS-env persist writes in Step 2C — additive (skip-if-present), so a
  snapshot is unnecessary; `clear` removes the marker-block lines instead.

`gateway-cli clear` deletes all of the snapshots above as its final step
(ownership-prefix allowlist + directory-escape guard — a shared
`GATEWAY_CLI_BACKUP_DIR` keeps other tools' files).

> Historical note: an earlier revision had `setup` delete gateway-bypassing OS
> env vars and snapshot them to `registry-env.<ts>.json`. That direction was
> reversed — `setup` no longer touches OS env vars, so that snapshot file is no
> longer produced.
