# Config Items, Placeholders & Default Values — gateway-cli-v2 installer

**Scope:** every configuration value the installer bakes, resolves, or writes —
its source of truth, its placeholder fallback, and what a *bare* build (no
`-Param`, no env var, no `site-config.json`) produces. Version stamped:
`pyproject.toml` = **0.1.0**.

Sources of truth (do not duplicate values elsewhere):
- `src/cli/site_defaults.py` — baked corporate defaults (gateway/admin/OIDC/CA).
- `src/cli/managed.py` — proxy constants (`EXPECTED_PROXY_URL`, `NO_PROXY_VALUE`,
  `FORBIDDEN_NO_PROXY_TOKEN`).
- `src/cli/manifest.py` `FIELDS` — the full catalog of every Claude Code config
  object the tool touches.
- `packaging/build.ps1` — build params that bake the environment-specific values.
- `packaging/site-config.json` — the one JSON a site admin edits.
- `src/cli/site_extra.py` + `packaging/site-extra.json` — free-form passthrough.

---

## 1. Two precedence chains

There are **two independent chains** — one at build time (what gets baked into
the frozen exe), one at runtime (what a given command resolves).

**Build-time (bakes `src/cli/_site_config.py`, regenerated every build):**
```
-Param  >  GATEWAY_CLI_DEFAULT_* env var  >  packaging/site-config.json  >  literal fallback in source
```
`build.ps1` writes only the *non-blank* values into `_site_config.py`; anything
left blank falls through to the literal fallback in `site_defaults.py` /
`managed.py` at import time.

**Runtime (per command — `login` / `setup` / `verify`):**
```
explicit CLI flag  >  GATEWAY_CLI_* env override  >  baked default (_site_config.py or literal)
```

> ⚠️ `GATEWAY_CLI_DEFAULT_*` (build-time) and `GATEWAY_CLI_*` (runtime) are
> **different** variable families. See §2 and §3.

---

## 2. Build-time bakeable items

The 8 environment-specific values `build.ps1` can bake. **All fallbacks are now
blank** (2026-08-14) — a bare build (no `-Param`, no env, no `site-config.json`)
bakes **nothing**, so the frozen exe applies nothing environment-specific by
default. The *only* default the tool still applies is the model (§4, `models.py`).
"Bare-build result" = what happens when nothing is supplied.

| Item | Source const | `build.ps1` param | Build env var (`GATEWAY_CLI_DEFAULT_*`) | `site-config.json` key | Fallback | Bare-build result |
|---|---|---|---|---|---|---|
| Gateway URL | `site_defaults.DEFAULT_GATEWAY_URL` | `-GatewayUrl` | `…_GATEWAY_URL` | `gatewayUrl` | `""` (blank) | blank → `setup` demands `--gateway-url` |
| Admin API URL | `site_defaults.DEFAULT_ADMIN_API_URL` | `-AdminApiUrl` | `…_ADMIN_API_URL` | `adminApiUrl` | `""` (blank) | blank → `setup` demands `--admin-api-url` |
| OIDC issuer URL | `site_defaults.DEFAULT_OIDC_ISSUER_URL` | `-OidcIssuerUrl` | `…_OIDC_ISSUER_URL` | `oidcIssuerUrl` | `""` (blank) | blank → `setup` demands `--oidc-issuer-url` |
| OIDC client id | `site_defaults.DEFAULT_OIDC_CLIENT_ID` | `-OidcClientId` | `…_OIDC_CLIENT_ID` | `oidcClientId` | `""` (blank) | blank → `setup` demands `--oidc-client-id` |
| CA bundle (PEM path) | `site_defaults.DEFAULT_CA_BUNDLE` | `-CaBundle` | `…_CA_BUNDLE` | `caBundle` | `""` (blank, all platforms) | blank → CA-bundle env vars **not written**; rely on OS trust store |
| Expected proxy URL | `managed.EXPECTED_PROXY_URL` | `-ExpectedProxyUrl` | `…_EXPECTED_PROXY_URL` | *(not mapped)* | `""` (blank) | blank → `verify` proxy check **skipped** |
| NO_PROXY value | `managed.NO_PROXY_VALUE` | `-NoProxyValue` | `…_NO_PROXY_VALUE` | *(not mapped)* | `""` (blank) | blank → managed `NO_PROXY` **not written** |
| Forbidden NO_PROXY token | `managed.FORBIDDEN_NO_PROXY_TOKEN` | `-ForbiddenNoProxyToken` | `…_FORBIDDEN_NO_PROXY_TOKEN` | *(not mapped)* | `""` (blank) | blank → `verify` NO_PROXY-token check **skipped** |

> **Blank ⇒ absent, not empty-value.** A blank baked constant resolves to
> `value=None` (`resolve._default_raw` guards on `if baked:`), and the emit path
> writes nothing for a `None` — so blanks *drop the key entirely*, they never
> write `KEY=""`. A real value flows through only when a build injects it or the
> user supplies it at runtime.

> **Asymmetry to note:** `site-config.json` only maps **5** keys
> (`oidcIssuerUrl`, `oidcClientId`, `gatewayUrl`, `adminApiUrl`, `caBundle` —
> `build.ps1:234-240`). The three **proxy** values can be set **only** via
> `-Param` or their `GATEWAY_CLI_DEFAULT_*` env var — editing `site-config.json`
> will not touch them.

---

## 3. Runtime override env vars (`GATEWAY_CLI_*`)

Set on the *end-user's* machine to override a baked value at command time
(precedence: flag > this env > baked). From `manifest.FIELDS[*].env_override`.

| Overrides | Runtime env var |
|---|---|
| `ANTHROPIC_BASE_URL` (gateway URL) | `GATEWAY_CLI_GATEWAY_PROXY_URL` |
| `ADMIN_API_URL` | `GATEWAY_CLI_ADMIN_API_URL` |
| `OIDC_ISSUER_URL` | `GATEWAY_CLI_OIDC_ISSUER_URL` |
| `OIDC_CLIENT_ID` | `GATEWAY_CLI_OIDC_CLIENT_ID` |
| all CA fields (`NODE_EXTRA_CA_CERTS`/`REQUESTS_CA_BUNDLE`/`AWS_CA_BUNDLE`/`SSL_CERT_FILE`/`CURL_CA_BUNDLE`) | `GATEWAY_CLI_CA_BUNDLE` |

Location overrides (`manifest.LOCATIONS[*].env_override`): `GATEWAY_CLI_DATA_DIR`,
`GATEWAY_CLI_BACKUP_DIR`, `GATEWAY_CLI_SETTINGS_PATH`, `GATEWAY_CLI_OIDC_CACHE`,
`GATEWAY_CLI_VK_CACHE`. Site-extra path override: `GATEWAY_CLI_SITE_EXTRA`.

---

## 4. Full config catalog (`manifest.FIELDS`)

Every Claude Code config object the tool is aware of. **Status** = the tool's
relationship: `OWNED` (writes it), `PASSTHROUGH` (site supplies via site-extra),
`BYPASS` (must NOT be set — removed by `reconcile`), `DOCUMENTED` (Claude Code
surface, not managed here). **Placement** = where it lives. **Tier** = which
settings tier (managed/user/either). **OS-env** = also persisted as a User-scope
OS var by `setup`/`env --persist`. **Literal** = constant source-level default.

### Gateway routing — `OWNED`
| Key | Placement | Tier | Baked from | Runtime env | OS-env | Flag |
|---|---|---|---|---|---|---|
| `ANTHROPIC_BASE_URL` | settings.env | either | `GATEWAY_URL` | `GATEWAY_CLI_GATEWAY_PROXY_URL` | ✅ | `--gateway-url` |
| `GATEWAY_CLI_GATEWAY_URL` | settings.env | either | `ADMIN_API_URL` | — | — | — |
| `ADMIN_API_URL` | settings.env | user | `ADMIN_API_URL` | `GATEWAY_CLI_ADMIN_API_URL` | ✅ | `--admin-api-url` |
| `apiKeyHelper` | settings.top | either | — | — | — | `--api-key-helper` |

### Auth / identity
| Key | Placement | Status | Tier | Baked from | Runtime env | OS-env | Flag |
|---|---|---|---|---|---|---|---|
| `OIDC_ISSUER_URL` | settings.env | OWNED | user | `OIDC_ISSUER_URL` | `GATEWAY_CLI_OIDC_ISSUER_URL` | ✅ | `--oidc-issuer-url` |
| `OIDC_CLIENT_ID` | settings.env | OWNED | user | `OIDC_CLIENT_ID` | `GATEWAY_CLI_OIDC_CLIENT_ID` | ✅ | `--oidc-client-id` |
| `ANTHROPIC_API_KEY` | settings.env | BYPASS (sensitive) | — | — | — | — | — |
| `AWS_BEARER_TOKEN_BEDROCK` | os.env | DOCUMENTED (sensitive) | — | — | — | — | — |
| `AWS_PROFILE` | os.env | DOCUMENTED | — | — | — | — | — |
| `AWS_SHARED_CREDENTIALS_FILE` | os.env | DOCUMENTED | — | — | — | — | — |
| `AWS_CONFIG_FILE` | os.env | DOCUMENTED | — | — | — | — | — |
| `awsAuthRefresh` | settings.top | DOCUMENTED | — | — | — | — | — |
| `awsCredentialExport` | settings.top | DOCUMENTED | — | — | — | — | — |

### Bedrock / Mantle toggles (gateway path does **not** use these)
| Key | Placement | Status | Notes |
|---|---|---|---|
| `CLAUDE_CODE_USE_BEDROCK` | settings.env | BYPASS | removed by reconcile |
| `ANTHROPIC_BEDROCK_BASE_URL` | settings.env | BYPASS | removed by reconcile |
| `ANTHROPIC_BEDROCK_REGION_PREFIX` | os.env | DOCUMENTED | enum: us/eu/apac/jp/au/global |
| `ANTHROPIC_BEDROCK_SERVICE_TIER` | os.env | DOCUMENTED | enum: default/flex/priority |
| `CLAUDE_CODE_USE_MANTLE` | os.env | DOCUMENTED | |
| `ANTHROPIC_BEDROCK_MANTLE_BASE_URL` | os.env | DOCUMENTED | |
| `CLAUDE_CODE_SKIP_MANTLE_AUTH` | os.env | DOCUMENTED | |
| `CLAUDE_CODE_SKIP_AWS_CRED_CACHE` | os.env | DOCUMENTED | |
| `CLAUDE_CODE_AWS_CHAIN_RESOLVE_TIMEOUT_MS` | os.env | DOCUMENTED | |
| `CLAUDE_CODE_DISABLE_BEDROCK_CONTENT_TYPE_GUARD` | os.env | DOCUMENTED | |

### Region — `DOCUMENTED`
`AWS_REGION`, `AWS_DEFAULT_REGION`, `ANTHROPIC_SMALL_FAST_MODEL_AWS_REGION` (all os.env).

### Model selection
| Key | Placement | Status | Tier | Flag | Notes |
|---|---|---|---|---|---|
| `model` | settings.top | OWNED | user | `--model` | written only when supplied |
| `availableModels` | settings.top | OWNED | user | `--available-models` | JSON array |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` | settings.env | OWNED | user | `--default-opus-model` | must be a gateway alias |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | settings.env | OWNED | user | `--default-sonnet-model` | must be a gateway alias |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | settings.env | OWNED | user | `--default-haiku-model` | must be a gateway alias |
| `ANTHROPIC_MODEL` | os.env | DOCUMENTED | — | — | append `[1m]` for 1M-context |
| `ANTHROPIC_SMALL_FAST_MODEL` | os.env | DOCUMENTED | — | — | deprecated |
| `modelOverrides` | settings.top | BYPASS | — | — | removed by reconcile |

Model defaults are **not baked** — they come from `src/cli/models.py`:
`FALLBACK_MODELS = ("claude-sonnet-4-6", "claude-opus-4-6", "claude-haiku-4-5")`,
`DEFAULT_MODEL = "claude-sonnet-4-6"` (the `--model` default; runtime precedence
`--model` > `GATEWAY_CLI_MODEL` > `DEFAULT_MODEL`).

### Telemetry (OTEL) — `OWNED`, all managed tier, all `site_extra_overridable`
| Key | Literal default | Notes |
|---|---|---|
| `CLAUDE_CODE_ENABLE_TELEMETRY` | `1` | master switch (set when an OTEL endpoint resolves) |
| `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA` | `1` | |
| `OTEL_METRICS_EXPORTER` | `otlp` | |
| `OTEL_LOGS_EXPORTER` | `otlp` | |
| `OTEL_TRACES_EXPORTER` | `otlp` | |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | *(derived)* | base; flag `--otel-endpoint`; per-signal endpoints derive from it |
| `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` | *(derived `<base>/v1/logs`)* | |
| `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` | *(derived `<base>/v1/metrics`)* | |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | *(derived `<base>/v1/traces`)* | |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/protobuf` | |
| `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` | `cumulative` | |
| `OTEL_LOG_USER_PROMPTS` | `1` | |
| `OTEL_LOG_TOOL_DETAILS` | `1` | |
| `OTEL_LOG_TOOL_CONTENT` | `1` | |
| `OTEL_METRICS_INCLUDE_VERSION` | `true` | |
| `OTEL_METRICS_INCLUDE_ENTRYPOINT` | `true` | |
| `OTEL_EXPORTER_OTLP_HEADERS` | *(none)* | sensitive; flag `--otel-auth-token` |
| `OTEL_RESOURCE_ATTRIBUTES` | *(derived)* | `user.id` stamped per identity (authoritative); site-extra attrs preserved |

### Proxy (see `docs/PROXY_PRECEDENCE.md`)
| Key | Placement | Status | Tier | Baked from | Notes |
|---|---|---|---|---|---|
| `HTTP_PROXY` | settings.env | PASSTHROUGH | managed | — | site supplies via site-extra; tool never sets it |
| `HTTPS_PROXY` | settings.env | PASSTHROUGH | managed | — | site supplies via site-extra; tool never sets it |
| `NO_PROXY` | settings.env | OWNED | managed | `NO_PROXY_VALUE` | tool owns when baked (overrides site-extra); blank fallback ⇒ key not written (a site-extra `NO_PROXY` may then fill it, since owned site-extra is last-resort) |

### TLS / corporate CA — `OWNED`, all baked from `CA_BUNDLE`, runtime env `GATEWAY_CLI_CA_BUNDLE`
| Key | Placement | Tier | Notes |
|---|---|---|---|
| `NODE_EXTRA_CA_CERTS` | settings.env | either | Claude Code (Node). "Written only when a CA bundle is configured" — see §5 |
| `REQUESTS_CA_BUNDLE` | settings.env | managed | python/requests helpers; also applied to this process |
| `AWS_CA_BUNDLE` | settings.env | managed | boto3/AWS helpers; also applied to this process |
| `SSL_CERT_FILE` | settings.env | managed | OpenSSL helpers; also applied to this process |
| `CURL_CA_BUNDLE` | process.env | none | this process only; not persisted |

### Guardrails / UI
| Key | Placement | Status | Tier | Notes |
|---|---|---|---|---|
| `ANTHROPIC_CUSTOM_HEADERS` | settings.env | PASSTHROUGH (sensitive) | managed | e.g. Bedrock Guardrails; via site-extra |
| `statusLine` | settings.top | OWNED | managed | flag `--statusline`; removed from user tier by reconcile |
| `permissions` | settings.top | PASSTHROUGH | either | deep-merged JSON; via site-extra |

---

## 5. Bare-build behaviour — "nothing except model"

**As of 2026-08-14 every fallback/default is blank.** A bare build (no
`-Param`, no `GATEWAY_CLI_DEFAULT_*`, no `site-config.json`) bakes and applies
**nothing environment-specific**. The one default the tool still applies is the
**model** (`cli/models.py` `DEFAULT_MODEL = "claude-sonnet-4-6"`, plus the
`FALLBACK_MODELS` list) — that is intentional and out of scope for this change.

What a bare build does at `setup`:

| Item | Bare-build behaviour |
|---|---|
| Gateway URL / Admin API URL | blank → `setup` requires `--gateway-url` / `--admin-api-url` (in `SETUP_REQUIRED_KEYS`); nothing routed until supplied |
| OIDC issuer / client id | blank → `setup` requires `--oidc-issuer-url` / `--oidc-client-id` |
| CA bundle | blank → `NODE_EXTRA_CA_CERTS` / `REQUESTS_CA_BUNDLE` / `AWS_CA_BUNDLE` / `SSL_CERT_FILE` **not written**; process relies on the OS trust store (truststore) |
| NO_PROXY | blank → managed `NO_PROXY` key **not emitted** |
| Expected proxy / forbidden token | blank → `verify`'s corporate-proxy check **returns early** (no warnings off-network) |
| model | `claude-sonnet-4-6` (the sole applied default) |

### How "blank ⇒ absent" is enforced (code map)

- **`site_defaults.py`** — `DEFAULT_GATEWAY_URL`, `DEFAULT_ADMIN_API_URL`,
  `DEFAULT_OIDC_ISSUER_URL`, `DEFAULT_OIDC_CLIENT_ID`, `DEFAULT_CA_BUNDLE` all
  `_baked(name, "")`. The former Windows CA fallback `C:\corp-proxy-ca.pem` is
  gone (blank on every platform).
- **`managed.py`** — `EXPECTED_PROXY_URL`, `FORBIDDEN_NO_PROXY_TOKEN`,
  `NO_PROXY_VALUE` all `_baked(name, "")`.
- **`resolve._default_raw`** — returns `None` for a blank baked constant
  (`if baked:`), so the field resolves to `value=None`.
- **`managed.build_gateway_env`** — `put(key, None)` emits nothing; the CA block
  is gated on `if ca_bundle:` and `NO_PROXY` is emitted via `const("NO_PROXY")`
  which is now `None`. Result: neither key is written.
- **`verify._check_proxy_settings`** — returns early when both
  `EXPECTED_PROXY_URL` and `FORBIDDEN_NO_PROXY_TOKEN` are blank. This guard is
  load-bearing: a blank forbidden token would otherwise make `"" in no_proxy`
  (always `True`) raise a spurious `NO_PROXY` warning on every box.

### To ship a real (corporate-network) build

Supply values at build time — any of, in precedence order:
`-GatewayUrl … -CaBundle … -ExpectedProxyUrl …` params > matching
`GATEWAY_CLI_DEFAULT_*` env vars > `packaging/site-config.json` (gateway/admin/
OIDC/CA only — the 3 proxy values are param/env-only, see §2). Anything supplied
is baked into `_site_config.py` and flows through exactly as before.

> **Runtime still works too.** Even a bare build can be pointed at real infra on
> the target machine via the `GATEWAY_CLI_*` runtime env vars (§3) or explicit
> flags — the blank bake only removes the *default*, not the capability.

> **Remaining placeholder — docs/examples only:** `site-extra.py`'s template
> still shows `"HTTPS_PROXY": "http://proxy.example.com:8080"` as an *example*
> inside `packaging/site-extra.json`. That file is operator-authored and absent
> by default, so it bakes nothing unless an operator ships it.

---

## 6. site-extra passthrough (`packaging/site-extra.json`)

Optional operator JSON, bundled at build time, deep-merged into settings at
`setup`. Two sections; gateway-owned keys are applied *after* it so it can never
break routing. All injected keys recorded under the `_gatewayCli` marker so
`disable` unmerges cleanly. Template shape:
```json
{
  "managed": { "env": { "HTTPS_PROXY": "http://proxy.example.com:8080" },
               "permissions": { "allow": ["Bash(git*)"] } },
  "user":    { "env": { "MY_TEAM_FLAG": "1" } }
}
```
Absent ⇒ injection is a no-op. Path override: `GATEWAY_CLI_SITE_EXTRA`.

---

## 7. Other `build.ps1` parameters (not config values)

| Param | Env var | Default | Purpose |
|---|---|---|---|
| `-WheelDir` | — | *(online)* | offline wheel cache for air-gapped builds |
| `-Version` | — | pyproject `0.1.0` | installer filename version |
| `-SignThumbprint` | `GATEWAY_CLI_SIGN_THUMBPRINT` | *(none)* | Authenticode cert-store thumbprint |
| `-SignPfxFile` | `GATEWAY_CLI_SIGN_PFX` | *(none)* | PFX file for signing |
| `-SignPfxPassword` | `GATEWAY_CLI_SIGN_PFX_PASSWORD` | *(none)* | PFX password |
| `-TimestampUrl` | — | `http://timestamp.digicert.com` | RFC-3161 timestamp server |
| `-SignToolPath` | — | *(auto-resolve)* | explicit signtool.exe path |
| `-SkipInstaller` | — | false | build exes only, skip Inno compile |

When no signing credential is supplied the build **succeeds with UNSIGNED**
binaries (fine for internal testing; trips AV/SmartScreen on locked-down fleets).

---

## 8. Key file locations (`manifest.LOCATIONS`)

| Name | Windows | macOS | Linux | Env override |
|---|---|---|---|---|
| data_dir | `%LOCALAPPDATA%\gateway-cli\` | `~/Library/Application Support/gateway-cli/` | `~/.local/share/gateway-cli/` | `GATEWAY_CLI_DATA_DIR` |
| backups_dir | `<data_dir>/backups/` | ← | ← | `GATEWAY_CLI_BACKUP_DIR` |
| managed_root | `C:\Program Files\ClaudeCode\` | `/Library/Application Support/ClaudeCode/` | `/etc/claude-code/` | — |
| managed_primary | `<managed_root>/managed-settings.json` | ← | ← | — |
| managed_dropin | `<managed_root>/managed-settings.d/50-gateway.json` | ← | ← | — |
| user_settings | `~/.claude/settings.json` | ← | ← | `GATEWAY_CLI_SETTINGS_PATH` |
| oidc_cache | `<data_dir>/oidc-tokens.json` | ← | ← | `GATEWAY_CLI_OIDC_CACHE` |
| vk_cache | `<data_dir>/vk-cache.json` | ← | ← | `GATEWAY_CLI_VK_CACHE` |

Settings precedence (highest first): OS/shell env var → managed drop-in
(`50-gateway.json`) → primary managed → CLI args → project-local → project →
user settings. (`manifest.SETTINGS_HIERARCHY`.)
