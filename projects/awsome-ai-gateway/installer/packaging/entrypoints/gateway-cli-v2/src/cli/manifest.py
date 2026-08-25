# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Central manifest of Claude Code configuration objects.

This module is the single **declarative catalog** of every Claude Code config
object gateway-cli is aware of — the ones it writes, the ones a corporate site
passes through, the ones it actively removes, and the wider Bedrock/Mantle
surface it merely documents. Today that knowledge is scattered as bare string
literals across three modules:

* ``site_defaults.py`` — baked corporate values + ``GATEWAY_CLI_*`` overrides +
  the TLS CA it applies to its own process.
* ``managed.py`` — the env block, ``apiKeyHelper`` and ``statusLine`` written
  into Claude Code's enterprise ``managed-settings.json`` (and its drop-in).
* ``site_extra.py`` — free-form operator passthrough from a bundled
  ``site_extra.json``.

This manifest does **not** take over writing — the merge/sudo/drop-in logic
stays in ``managed.py`` and passthrough stays in ``site_extra.py``. It is a
catalog + resolver those modules can point at instead of hardcoding literals,
and a single place to answer "what does this tool touch, where does it live, and
who wins."

Grounded in the runtime docs (kept in sync with them, not a substitute):

* ``docs/FILE_AND_ENV_OPERATIONS.md`` — key locations + per-command file/env ops.
* ``docs/OTEL_PRECEDENCE.md`` — telemetry endpoint precedence.
* ``docs/PROXY_PRECEDENCE.md`` — ``*_PROXY`` / ``NO_PROXY`` ownership.

The authoritative *runtime* source for the exhaustive OTEL env set remains
``managed.build_gateway_env`` — this manifest documents the surface, it does not
re-derive the per-signal OTEL endpoints.

Descriptor schema (see docs/CONFIG_GENERALIZATION_PLAN.md §5): beyond the
identity/placement attrs, each field also declares its typed shape
(:class:`ValueKind`), how sources combine (:class:`Compose`), the CLI ``flag`` /
named ``derive`` deriver that can set it, whether ``site_extra`` may override it,
its concrete ``outputs`` targets, and whether it is ``sensitive`` (redacted in
diagnostics). These attributes are declarative metadata: the resolve/emit
consumers that act on them land in later phases; adding them here does not change
current behaviour (every existing field keeps its defaults).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# ---------------------------------------------------------------------------
# Taxonomy
# ---------------------------------------------------------------------------


class Category(str, Enum):
    """What area of Claude Code behaviour a config object controls."""

    GATEWAY = "gateway"        # routing to the LLM Gateway proxy / admin API
    AUTH = "auth"              # OIDC, AWS credentials, API keys
    BEDROCK = "bedrock"        # direct-Bedrock / Mantle toggles and endpoints
    REGION = "region"          # AWS region selection
    MODEL = "model"            # model selection / pinning
    CACHING = "caching"        # prompt caching
    TELEMETRY = "telemetry"    # OpenTelemetry (metrics/traces/logs)
    PROXY = "proxy"            # forward proxy / bypass list
    TLS = "tls"                # corporate CA / trust store
    GUARDRAILS = "guardrails"  # Bedrock Guardrails custom headers
    UI = "ui"                  # status line, permissions, presentation


class Placement(str, Enum):
    """Where the value physically lives."""

    SETTINGS_ENV = "settings.env"    # a key inside a settings file's "env" block
    SETTINGS_TOP = "settings.top"    # a top-level key in a settings file
    PROCESS_ENV = "process.env"      # gateway-cli's own process env (transient)
    OS_ENV = "os.env"                # a persisted OS/user-scope environment var


class Tier(str, Enum):
    """Which Claude Code settings tier an owned settings value is written to.

    Only meaningful for ``SETTINGS_ENV`` / ``SETTINGS_TOP`` placements. See
    :data:`SETTINGS_HIERARCHY` for the full precedence order.
    """

    MANAGED = "managed"  # enterprise managed-settings.json (+ drop-in)
    USER = "user"        # ~/.claude/settings.json
    EITHER = "either"    # written to both tiers
    NONE = "none"        # not a settings value (process/OS env)


class Status(str, Enum):
    """gateway-cli's relationship to a config object."""

    OWNED = "owned"              # gateway-cli writes it
    PASSTHROUGH = "passthrough"  # supplied by a site via site_extra.json
    BYPASS = "bypass"            # gateway-bypassing: removed from settings and/or
                                 # reported by `verify`; must NOT be set
    DOCUMENTED = "documented"    # part of Claude Code's surface; not managed here


class ValueKind(str, Enum):
    """The typed shape of a config value — drives parsing, validation, emission.

    Scalars serialize to a single env-var/settings string; the structured kinds
    parse to a Python object on input and encode per output placement (a JSON
    array, a JSON object, a command record, or a comma-joined key=value map). A
    scalar-only model could not emit ``availableModels`` / ``statusLine`` /
    ``permissions`` correctly (finding R2), hence the structured kinds.
    """

    # scalar env-var kinds
    STR = "str"
    URL = "url"
    PATH = "path"
    ENUM = "enum"
    CSV = "csv"
    MODEL_ALIAS = "model_alias"
    # structured settings kinds (parsed on input, encoded per output placement)
    STR_LIST = "str_list"          # availableModels → JSON array
    JSON_OBJECT = "json_object"    # permissions → JSON object (deep-merged)
    COMMAND = "command"            # statusLine → {"type": "command", "command": ...}
    ATTR_MAP = "attr_map"          # OTEL_RESOURCE_ATTRIBUTES → comma-joined key=value


class Compose(str, Enum):
    """How a field's resolved value composes with values from other sources.

    ``REPLACE`` is the default "highest-precedence source wins" for scalars.
    ``MERGE_ATTRS`` merges key=value maps but lets a field's ``authoritative_keys``
    (e.g. ``user.id``) always win regardless of source order — the R1 invariant.
    ``MERGE_OBJECT`` deep-merges JSON objects (site_extra ⊕ gateway-owned).
    """

    REPLACE = "replace"            # default: highest-precedence source wins (scalars)
    MERGE_ATTRS = "merge_attrs"    # merge key=value maps; authoritative_keys always win
    MERGE_OBJECT = "merge_object"  # deep-merge JSON objects (site_extra ⊕ owned)


# ---------------------------------------------------------------------------
# Config field catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Output:
    """One concrete ``(placement, tier)`` target a field's value is written to.

    A field may write to several targets (e.g. ``ANTHROPIC_BASE_URL`` lands in the
    managed env, the user env, and a persisted OS env var). Empty ``outputs`` on a
    field means "not emitted" (a DOCUMENTED/BYPASS surface entry).
    """

    placement: Placement
    tier: Tier = Tier.NONE
    #: Named runtime predicate that gates this target; the value is written here
    #: only when the predicate holds. None ⇒ always emitted. E.g.
    #: ``"user_tier_seeded"`` for OTEL_RESOURCE_ATTRIBUTES's user-tier copy, which
    #: setup writes only when site-extra already seeded the key at the user tier
    #: (setup.py:559-565) — so consumers must NOT emit it unconditionally.
    condition: str | None = None


@dataclass(frozen=True)
class ConfigField:
    """One Claude Code configuration object and how gateway-cli treats it."""

    key: str
    category: Category
    placement: Placement
    status: Status
    tier: Tier = Tier.NONE
    #: ``_site_config`` attribute this value is build-time-injected from, if any
    #: (see ``site_defaults._baked``). None ⇒ not build-injected.
    baked_from: str | None = None
    #: ``GATEWAY_CLI_*`` env var that overrides it at runtime, if any.
    env_override: str | None = None
    #: True when ``setup`` (and ``gateway-cli env --persist``) ALSO writes this key
    #: as a permanent User-scope OS environment variable (shell rc / HKCU), on top
    #: of any settings-file placement. The OS-env copy exists for tooling that
    #: reads the process environment rather than Claude Code's settings.json.
    os_persist: bool = False

    # --- typed value + composition (declarative; consumed by later phases) ----
    #: The typed shape of the value; drives parsing/validation/emission.
    value_kind: ValueKind = ValueKind.STR
    #: Constant source-level fallback written when no higher-precedence source
    #: resolves (e.g. OTEL protocol ``http/protobuf``, telemetry toggle ``1``).
    literal_default: str | None = None
    #: True when a ``site_extra`` ``managed.env`` value MAY override gateway-cli's
    #: computed value (the deliberate ``OTEL_*`` exception + forward-proxy
    #: passthrough). Default False ⇒ gateway-cli owns the key outright.
    site_extra_overridable: bool = False
    #: CLI flag that sets this field (e.g. ``--gateway-url``) when user-settable.
    flag: str | None = None
    #: Named deriver in ``RESOLVERS`` that computes the value at execution
    #: (e.g. ``otel_from_host``, ``user_id_from_identity``).
    derive: str | None = None
    #: How resolved sources combine (REPLACE default; MERGE_ATTRS / MERGE_OBJECT).
    compose: Compose = Compose.REPLACE
    #: Sub-keys a deriver is authoritative for regardless of source order — e.g.
    #: ``("user.id",)`` for OTEL_RESOURCE_ATTRIBUTES (the R1 invariant).
    authoritative_keys: tuple[str, ...] = ()
    #: Allowed values for ``ValueKind.ENUM``.
    choices: tuple[str, ...] = ()
    #: Named validator in ``VALIDATORS``; falls back to the by-kind validator.
    validate: str | None = None
    #: Concrete ``(placement, tier)`` targets this value is written to. Empty ⇒
    #: not emitted (DOCUMENTED/BYPASS), or fall back to placement/tier/os_persist.
    outputs: tuple[Output, ...] = ()
    #: True ⇒ credential-bearing; never printed in full by diagnostics/logs (R4).
    sensitive: bool = False

    #: One-line description of what it does / why it has this status.
    doc: str = ""


# NOTE: the OWNED telemetry entries now catalog the FULL OTEL_* surface
# ``managed.build_gateway_env`` emits (per-signal exporters, per-signal endpoints,
# temporality, OTEL_LOG_* / OTEL_METRICS_INCLUDE_* toggles) — T1.3b, prerequisite
# for the T2.1 manifest↔writer drift guard. ``build_gateway_env`` remains the
# authoritative runtime source that *derives* the per-signal endpoints from the
# base; this catalog documents them and their placement. ``outputs`` mirrors the
# current placement/tier/os_persist behaviour so the resolve/emit rewrite (Phase
# 5) can consume it without changing what lands on disk.
FIELDS: tuple[ConfigField, ...] = (
    # ── Gateway routing (owned) ─────────────────────────────────────────────
    ConfigField(
        "ANTHROPIC_BASE_URL", Category.GATEWAY, Placement.SETTINGS_ENV, Status.OWNED,
        tier=Tier.EITHER, baked_from="GATEWAY_URL",
        env_override="GATEWAY_CLI_GATEWAY_PROXY_URL", os_persist=True,
        value_kind=ValueKind.URL, flag="--gateway-url", validate="url",
        outputs=(
            Output(Placement.SETTINGS_ENV, Tier.MANAGED),
            Output(Placement.SETTINGS_ENV, Tier.USER),
            Output(Placement.OS_ENV),
        ),
        doc="Claude Code API base — points at the LLM Gateway proxy. C3 rationale: gateway-cli "
            "routes via the Anthropic-native path (ANTHROPIC_BASE_URL + apiKeyHelper) rather than "
            "CLAUDE_CODE_USE_BEDROCK / CLAUDE_CODE_USE_MANTLE, so all traffic flows through the "
            "gateway proxy where Virtual Keys and policy apply; the Bedrock/Mantle toggles would "
            "route around it (hence their BYPASS/DOCUMENTED status).",
    ),
    ConfigField(
        "GATEWAY_CLI_GATEWAY_URL", Category.GATEWAY, Placement.SETTINGS_ENV, Status.OWNED,
        tier=Tier.EITHER, baked_from="ADMIN_API_URL",
        value_kind=ValueKind.URL, validate="url",
        outputs=(
            Output(Placement.SETTINGS_ENV, Tier.MANAGED),
            Output(Placement.SETTINGS_ENV, Tier.USER),
        ),
        doc="Admin API URL the api-key-helper calls to issue Virtual Keys. Legacy alias — "
            "written to BOTH the managed env (managed.py) and user settings (setup.py) for compat.",
    ),
    ConfigField(
        "ADMIN_API_URL", Category.GATEWAY, Placement.SETTINGS_ENV, Status.OWNED,
        tier=Tier.USER, baked_from="ADMIN_API_URL",
        env_override="GATEWAY_CLI_ADMIN_API_URL", os_persist=True,
        value_kind=ValueKind.URL, flag="--admin-api-url", validate="url",
        outputs=(
            Output(Placement.SETTINGS_ENV, Tier.USER),
            Output(Placement.OS_ENV),
        ),
        doc="Admin API URL surfaced in user settings for tooling/diagnostics.",
    ),
    ConfigField(
        "apiKeyHelper", Category.GATEWAY, Placement.SETTINGS_TOP, Status.OWNED,
        tier=Tier.EITHER, value_kind=ValueKind.PATH,
        flag="--api-key-helper", validate="path",
        outputs=(
            Output(Placement.SETTINGS_TOP, Tier.MANAGED),
            Output(Placement.SETTINGS_TOP, Tier.USER),
        ),
        doc="Path to the api-key-helper executable (quoted if it has spaces). C3: paired with "
            "ANTHROPIC_BASE_URL to mint per-user Virtual Keys against the admin API — the "
            "Anthropic-native auth path, chosen over Bedrock/Mantle credential modes so routing "
            "and key issuance stay on the gateway.",
    ),

    # ── Auth / identity ─────────────────────────────────────────────────────
    ConfigField(
        "OIDC_ISSUER_URL", Category.AUTH, Placement.SETTINGS_ENV, Status.OWNED,
        tier=Tier.USER, baked_from="OIDC_ISSUER_URL",
        env_override="GATEWAY_CLI_OIDC_ISSUER_URL", os_persist=True,
        value_kind=ValueKind.URL, flag="--oidc-issuer-url", validate="url",
        outputs=(
            Output(Placement.SETTINGS_ENV, Tier.USER),
            Output(Placement.OS_ENV),
        ),
        doc="OIDC issuer for `login` PKCE; also persisted to User-scope OS env.",
    ),
    ConfigField(
        "OIDC_CLIENT_ID", Category.AUTH, Placement.SETTINGS_ENV, Status.OWNED,
        tier=Tier.USER, baked_from="OIDC_CLIENT_ID",
        env_override="GATEWAY_CLI_OIDC_CLIENT_ID", os_persist=True,
        value_kind=ValueKind.STR, flag="--oidc-client-id",
        outputs=(
            Output(Placement.SETTINGS_ENV, Tier.USER),
            Output(Placement.OS_ENV),
        ),
        doc="OIDC client id for `login` PKCE; also persisted to User-scope OS env.",
    ),
    ConfigField(
        "ANTHROPIC_API_KEY", Category.AUTH, Placement.SETTINGS_ENV, Status.BYPASS,
        sensitive=True,
        doc="Direct-API key — removed from settings by reconcile; verify reports OS-env copies.",
    ),
    ConfigField(
        "AWS_BEARER_TOKEN_BEDROCK", Category.AUTH, Placement.OS_ENV, Status.DOCUMENTED,
        sensitive=True,
        doc="Bedrock API-key bearer token (direct-Bedrock auth). Not used on the gateway path.",
    ),
    ConfigField(
        "AWS_PROFILE", Category.AUTH, Placement.OS_ENV, Status.DOCUMENTED,
        doc="AWS credential profile for direct-Bedrock auth.",
    ),
    ConfigField(
        "AWS_SHARED_CREDENTIALS_FILE", Category.AUTH, Placement.OS_ENV, Status.DOCUMENTED,
        value_kind=ValueKind.PATH,
        doc="Path to a non-default AWS shared credentials file (feeds region/creds resolution).",
    ),
    ConfigField(
        "AWS_CONFIG_FILE", Category.AUTH, Placement.OS_ENV, Status.DOCUMENTED,
        value_kind=ValueKind.PATH,
        doc="Path to a non-default AWS config file (feeds region/creds resolution).",
    ),
    ConfigField(
        "awsAuthRefresh", Category.AUTH, Placement.SETTINGS_TOP, Status.DOCUMENTED,
        value_kind=ValueKind.COMMAND,
        doc="settings.json script that refreshes AWS creds before each call.",
    ),
    ConfigField(
        "awsCredentialExport", Category.AUTH, Placement.SETTINGS_TOP, Status.DOCUMENTED,
        value_kind=ValueKind.COMMAND,
        doc="settings.json script that exports AWS creds as JSON.",
    ),

    # ── Bedrock / Mantle toggles (gateway path does not use these) ───────────
    ConfigField(
        "CLAUDE_CODE_USE_BEDROCK", Category.BEDROCK, Placement.SETTINGS_ENV, Status.BYPASS,
        doc="Direct-Bedrock switch — removed from settings by reconcile; verify reports OS-env copies.",
    ),
    ConfigField(
        "ANTHROPIC_BEDROCK_BASE_URL", Category.BEDROCK, Placement.SETTINGS_ENV, Status.BYPASS,
        value_kind=ValueKind.URL,
        doc="Direct-Bedrock endpoint override — removed from settings; verify reports OS-env copies.",
    ),
    ConfigField(
        "ANTHROPIC_BEDROCK_REGION_PREFIX", Category.BEDROCK, Placement.OS_ENV, Status.DOCUMENTED,
        value_kind=ValueKind.ENUM, choices=("us", "eu", "apac", "jp", "au", "global"),
        doc="Bedrock inference-profile region prefix (us/eu/apac/jp/au/global).",
    ),
    ConfigField(
        "ANTHROPIC_BEDROCK_SERVICE_TIER", Category.BEDROCK, Placement.OS_ENV, Status.DOCUMENTED,
        value_kind=ValueKind.ENUM, choices=("default", "flex", "priority"),
        doc="Bedrock service tier (default/flex/priority).",
    ),
    ConfigField(
        "CLAUDE_CODE_USE_MANTLE", Category.BEDROCK, Placement.OS_ENV, Status.DOCUMENTED,
        doc="Route through Mantle instead of direct Bedrock.",
    ),
    ConfigField(
        "ANTHROPIC_BEDROCK_MANTLE_BASE_URL", Category.BEDROCK, Placement.OS_ENV, Status.DOCUMENTED,
        value_kind=ValueKind.URL,
        doc="Mantle endpoint base URL.",
    ),
    ConfigField(
        "CLAUDE_CODE_SKIP_MANTLE_AUTH", Category.BEDROCK, Placement.OS_ENV, Status.DOCUMENTED,
        doc="Skip Mantle auth (test/dev only).",
    ),
    ConfigField(
        "CLAUDE_CODE_SKIP_AWS_CRED_CACHE", Category.BEDROCK, Placement.OS_ENV, Status.DOCUMENTED,
        doc="Disable Claude Code's AWS credential cache.",
    ),
    ConfigField(
        "CLAUDE_CODE_AWS_CHAIN_RESOLVE_TIMEOUT_MS", Category.BEDROCK, Placement.OS_ENV, Status.DOCUMENTED,
        doc="AWS credential-chain resolution timeout (ms).",
    ),
    ConfigField(
        "CLAUDE_CODE_DISABLE_BEDROCK_CONTENT_TYPE_GUARD", Category.BEDROCK, Placement.OS_ENV,
        Status.DOCUMENTED,
        doc="Skip the Bedrock streaming content-type guard when a gateway/proxy rewrites only the "
            "Content-Type header (leaves the binary event-stream body intact).",
    ),

    # ── Region ──────────────────────────────────────────────────────────────
    ConfigField(
        "AWS_REGION", Category.REGION, Placement.OS_ENV, Status.DOCUMENTED,
        doc="Primary AWS region for direct-Bedrock calls.",
    ),
    ConfigField(
        "AWS_DEFAULT_REGION", Category.REGION, Placement.OS_ENV, Status.DOCUMENTED,
        doc="AWS region fallback, tried after AWS_REGION and before the active profile's region.",
    ),
    ConfigField(
        "ANTHROPIC_SMALL_FAST_MODEL_AWS_REGION", Category.REGION, Placement.OS_ENV, Status.DOCUMENTED,
        doc="Region override for the small/fast (Haiku) model.",
    ),

    # ── Model selection / pinning ───────────────────────────────────────────
    ConfigField(
        "model", Category.MODEL, Placement.SETTINGS_TOP, Status.OWNED, tier=Tier.USER,
        value_kind=ValueKind.MODEL_ALIAS, flag="--model", validate="model_alias",
        outputs=(Output(Placement.SETTINGS_TOP, Tier.USER),),
        doc="Default model — written to user settings only when supplied to setup.",
    ),
    ConfigField(
        "availableModels", Category.MODEL, Placement.SETTINGS_TOP, Status.OWNED, tier=Tier.USER,
        value_kind=ValueKind.STR_LIST, flag="--available-models",
        outputs=(Output(Placement.SETTINGS_TOP, Tier.USER),),
        doc="Model picker list — written to user settings only when supplied to setup.",
    ),
    ConfigField(
        "ANTHROPIC_DEFAULT_OPUS_MODEL", Category.MODEL, Placement.SETTINGS_ENV, Status.OWNED,
        tier=Tier.USER, value_kind=ValueKind.MODEL_ALIAS, flag="--default-opus-model",
        validate="model_alias",
        outputs=(Output(Placement.SETTINGS_ENV, Tier.USER),),
        doc="Opus model id — written to user settings when supplied. F2: must be a gateway alias "
            "(e.g. claude-opus-4-6), NOT a us.anthropic.* Bedrock inference-profile id — the "
            "gateway resolves aliases, and a Bedrock id would not route (see models.py roster).",
    ),
    ConfigField(
        "ANTHROPIC_DEFAULT_SONNET_MODEL", Category.MODEL, Placement.SETTINGS_ENV, Status.OWNED,
        tier=Tier.USER, value_kind=ValueKind.MODEL_ALIAS, flag="--default-sonnet-model",
        validate="model_alias",
        outputs=(Output(Placement.SETTINGS_ENV, Tier.USER),),
        doc="Sonnet model id — written to user settings when supplied. F2: must be a gateway alias "
            "(e.g. claude-sonnet-4-6), NOT a us.anthropic.* Bedrock inference-profile id — the "
            "gateway resolves aliases, and a Bedrock id would not route (see models.py roster).",
    ),
    ConfigField(
        "ANTHROPIC_DEFAULT_HAIKU_MODEL", Category.MODEL, Placement.SETTINGS_ENV, Status.OWNED,
        tier=Tier.USER, value_kind=ValueKind.MODEL_ALIAS, flag="--default-haiku-model",
        validate="model_alias",
        outputs=(Output(Placement.SETTINGS_ENV, Tier.USER),),
        doc="Haiku (small/fast) model id — written to user settings when supplied. F2: must be a "
            "gateway alias (e.g. claude-haiku-4-5), NOT a us.anthropic.* Bedrock inference-profile "
            "id — the gateway resolves aliases, and a Bedrock id would not route (models.py roster).",
    ),
    ConfigField(
        "ANTHROPIC_MODEL", Category.MODEL, Placement.OS_ENV, Status.DOCUMENTED,
        value_kind=ValueKind.MODEL_ALIAS,
        doc="Primary model override via env (append [1m] for 1M-context).",
    ),
    ConfigField(
        "ANTHROPIC_SMALL_FAST_MODEL", Category.MODEL, Placement.OS_ENV, Status.DOCUMENTED,
        value_kind=ValueKind.MODEL_ALIAS,
        doc="Deprecated small/fast (Haiku-class) model selector; superseded by "
            "ANTHROPIC_DEFAULT_HAIKU_MODEL.",
    ),
    ConfigField(
        "modelOverrides", Category.MODEL, Placement.SETTINGS_TOP, Status.BYPASS,
        value_kind=ValueKind.JSON_OBJECT,
        doc="Per-model routing overrides — removed from user settings by reconcile.",
    ),

    # ── Prompt caching ──────────────────────────────────────────────────────
    ConfigField(
        "DISABLE_PROMPT_CACHING", Category.CACHING, Placement.OS_ENV, Status.DOCUMENTED,
        doc="Disable prompt caching entirely.",
    ),
    ConfigField(
        "ENABLE_PROMPT_CACHING_1H", Category.CACHING, Placement.OS_ENV, Status.DOCUMENTED,
        doc="Opt into the 1-hour prompt-cache TTL.",
    ),

    # ── Telemetry (owned; OTEL_* site-overridable — see OTEL_PRECEDENCE.md) ──
    ConfigField(
        "CLAUDE_CODE_ENABLE_TELEMETRY", Category.TELEMETRY, Placement.SETTINGS_ENV, Status.OWNED,
        tier=Tier.MANAGED, literal_default="1",
        outputs=(Output(Placement.SETTINGS_ENV, Tier.MANAGED),),
        doc="Master telemetry switch (set to 1 when an OTEL endpoint resolves).",
    ),
    ConfigField(
        "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA", Category.TELEMETRY, Placement.SETTINGS_ENV, Status.OWNED,
        tier=Tier.MANAGED, literal_default="1",
        outputs=(Output(Placement.SETTINGS_ENV, Tier.MANAGED),),
        doc="Enhanced telemetry beta flag.",
    ),
    ConfigField(
        "OTEL_METRICS_EXPORTER", Category.TELEMETRY, Placement.SETTINGS_ENV, Status.OWNED,
        tier=Tier.MANAGED, literal_default="otlp", site_extra_overridable=True,
        outputs=(Output(Placement.SETTINGS_ENV, Tier.MANAGED),),
        doc="Metrics exporter — set to otlp when an OTEL endpoint resolves.",
    ),
    ConfigField(
        "OTEL_LOGS_EXPORTER", Category.TELEMETRY, Placement.SETTINGS_ENV, Status.OWNED,
        tier=Tier.MANAGED, literal_default="otlp", site_extra_overridable=True,
        outputs=(Output(Placement.SETTINGS_ENV, Tier.MANAGED),),
        doc="Logs exporter — set to otlp when an OTEL endpoint resolves.",
    ),
    ConfigField(
        "OTEL_TRACES_EXPORTER", Category.TELEMETRY, Placement.SETTINGS_ENV, Status.OWNED,
        tier=Tier.MANAGED, literal_default="otlp", site_extra_overridable=True,
        outputs=(Output(Placement.SETTINGS_ENV, Tier.MANAGED),),
        doc="Traces exporter — set to otlp when an OTEL endpoint resolves.",
    ),
    ConfigField(
        "OTEL_EXPORTER_OTLP_ENDPOINT", Category.TELEMETRY, Placement.SETTINGS_ENV, Status.OWNED,
        tier=Tier.MANAGED, value_kind=ValueKind.URL, validate="url",
        derive="otel_from_host", site_extra_overridable=True, flag="--otel-endpoint",
        outputs=(Output(Placement.SETTINGS_ENV, Tier.MANAGED),),
        doc="Base OTLP collector endpoint. Precedence: --otel-endpoint > site-extra > auto-derived. "
            "Per-signal logs/metrics/traces endpoints are derived from this base.",
    ),
    ConfigField(
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", Category.TELEMETRY, Placement.SETTINGS_ENV, Status.OWNED,
        tier=Tier.MANAGED, value_kind=ValueKind.URL, validate="url",
        derive="otel_from_host", site_extra_overridable=True,
        outputs=(Output(Placement.SETTINGS_ENV, Tier.MANAGED),),
        doc="Per-signal logs endpoint (<base>/v1/logs) derived from OTEL_EXPORTER_OTLP_ENDPOINT; "
            "an operator may override it individually via site-extra managed.env.",
    ),
    ConfigField(
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", Category.TELEMETRY, Placement.SETTINGS_ENV, Status.OWNED,
        tier=Tier.MANAGED, value_kind=ValueKind.URL, validate="url",
        derive="otel_from_host", site_extra_overridable=True,
        outputs=(Output(Placement.SETTINGS_ENV, Tier.MANAGED),),
        doc="Per-signal metrics endpoint (<base>/v1/metrics) derived from OTEL_EXPORTER_OTLP_ENDPOINT; "
            "an operator may override it individually via site-extra managed.env.",
    ),
    ConfigField(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", Category.TELEMETRY, Placement.SETTINGS_ENV, Status.OWNED,
        tier=Tier.MANAGED, value_kind=ValueKind.URL, validate="url",
        derive="otel_from_host", site_extra_overridable=True,
        outputs=(Output(Placement.SETTINGS_ENV, Tier.MANAGED),),
        doc="Per-signal traces endpoint (<base>/v1/traces) derived from OTEL_EXPORTER_OTLP_ENDPOINT; "
            "an operator may override it individually via site-extra managed.env.",
    ),
    ConfigField(
        "OTEL_EXPORTER_OTLP_PROTOCOL", Category.TELEMETRY, Placement.SETTINGS_ENV, Status.OWNED,
        tier=Tier.MANAGED, literal_default="http/protobuf", site_extra_overridable=True,
        outputs=(Output(Placement.SETTINGS_ENV, Tier.MANAGED),),
        doc="OTLP wire protocol (http/protobuf).",
    ),
    ConfigField(
        "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE", Category.TELEMETRY,
        Placement.SETTINGS_ENV, Status.OWNED, tier=Tier.MANAGED,
        literal_default="cumulative", site_extra_overridable=True,
        outputs=(Output(Placement.SETTINGS_ENV, Tier.MANAGED),),
        doc="OTLP metrics temporality preference (cumulative).",
    ),
    ConfigField(
        "OTEL_LOG_USER_PROMPTS", Category.TELEMETRY, Placement.SETTINGS_ENV, Status.OWNED,
        tier=Tier.MANAGED, literal_default="1", site_extra_overridable=True,
        outputs=(Output(Placement.SETTINGS_ENV, Tier.MANAGED),),
        doc="Include user-prompt content in emitted logs (1).",
    ),
    ConfigField(
        "OTEL_LOG_TOOL_DETAILS", Category.TELEMETRY, Placement.SETTINGS_ENV, Status.OWNED,
        tier=Tier.MANAGED, literal_default="1", site_extra_overridable=True,
        outputs=(Output(Placement.SETTINGS_ENV, Tier.MANAGED),),
        doc="Include tool-invocation details in emitted logs (1).",
    ),
    ConfigField(
        "OTEL_LOG_TOOL_CONTENT", Category.TELEMETRY, Placement.SETTINGS_ENV, Status.OWNED,
        tier=Tier.MANAGED, literal_default="1", site_extra_overridable=True,
        outputs=(Output(Placement.SETTINGS_ENV, Tier.MANAGED),),
        doc="Include tool input/output content in emitted logs (1).",
    ),
    ConfigField(
        "OTEL_METRICS_INCLUDE_VERSION", Category.TELEMETRY, Placement.SETTINGS_ENV, Status.OWNED,
        tier=Tier.MANAGED, literal_default="true", site_extra_overridable=True,
        outputs=(Output(Placement.SETTINGS_ENV, Tier.MANAGED),),
        doc="Attach the app version as a metric attribute (true).",
    ),
    ConfigField(
        "OTEL_METRICS_INCLUDE_ENTRYPOINT", Category.TELEMETRY, Placement.SETTINGS_ENV, Status.OWNED,
        tier=Tier.MANAGED, literal_default="true", site_extra_overridable=True,
        outputs=(Output(Placement.SETTINGS_ENV, Tier.MANAGED),),
        doc="Attach the entrypoint as a metric attribute (true).",
    ),
    ConfigField(
        "OTEL_EXPORTER_OTLP_HEADERS", Category.TELEMETRY, Placement.SETTINGS_ENV, Status.OWNED,
        tier=Tier.MANAGED, site_extra_overridable=True, sensitive=True,
        flag="--otel-auth-token",
        outputs=(Output(Placement.SETTINGS_ENV, Tier.MANAGED),),
        doc="OTLP auth header (Authorization=Bearer <token>) when a token is supplied.",
    ),
    ConfigField(
        "OTEL_RESOURCE_ATTRIBUTES", Category.TELEMETRY, Placement.SETTINGS_ENV, Status.OWNED,
        tier=Tier.EITHER, value_kind=ValueKind.ATTR_MAP,
        compose=Compose.MERGE_ATTRS, authoritative_keys=("user.id",),
        derive="user_id_from_identity", site_extra_overridable=True,
        outputs=(
            Output(Placement.SETTINGS_ENV, Tier.MANAGED),
            Output(Placement.SETTINGS_ENV, Tier.USER, condition="user_tier_seeded"),
        ),
        doc="Resource attributes; user.id is stamped dynamically per logged-in identity, "
            "site-extra static attributes (service.name, …) preserved. The managed-tier "
            "copy is always emitted; the user-tier copy is reconciled ONLY when site-extra "
            "already seeded OTEL_RESOURCE_ATTRIBUTES in ~/.claude/settings.json "
            "(setup.py:559-565) — we never inject the key at the user tier when absent. "
            "This preserves delivered Fix 1 (gateway-cli-issues.md:255-257).",
    ),

    # ── Proxy (see PROXY_PRECEDENCE.md) ─────────────────────────────────────
    ConfigField(
        "HTTP_PROXY", Category.PROXY, Placement.SETTINGS_ENV, Status.PASSTHROUGH, tier=Tier.MANAGED,
        value_kind=ValueKind.URL, site_extra_overridable=True,
        outputs=(Output(Placement.SETTINGS_ENV, Tier.MANAGED),),
        doc="Forward proxy for outbound HTTP — supplied by the site via site_extra, not set here.",
    ),
    ConfigField(
        "HTTPS_PROXY", Category.PROXY, Placement.SETTINGS_ENV, Status.PASSTHROUGH, tier=Tier.MANAGED,
        value_kind=ValueKind.URL, site_extra_overridable=True,
        outputs=(Output(Placement.SETTINGS_ENV, Tier.MANAGED),),
        doc="Forward proxy for outbound HTTPS — supplied by the site via site_extra, not set here.",
    ),
    ConfigField(
        "NO_PROXY", Category.PROXY, Placement.SETTINGS_ENV, Status.OWNED, tier=Tier.MANAGED,
        baked_from="NO_PROXY_VALUE", value_kind=ValueKind.CSV,
        outputs=(Output(Placement.SETTINGS_ENV, Tier.MANAGED),),
        doc="Direct-reach bypass list — owned by gateway-cli; overrides any site-extra NO_PROXY. "
            "Must never contain FORBIDDEN_NO_PROXY_TOKEN.",
    ),

    # ── TLS / corporate CA ──────────────────────────────────────────────────
    ConfigField(
        "NODE_EXTRA_CA_CERTS", Category.TLS, Placement.SETTINGS_ENV, Status.OWNED, tier=Tier.EITHER,
        baked_from="CA_BUNDLE", env_override="GATEWAY_CLI_CA_BUNDLE",
        value_kind=ValueKind.PATH, validate="path",
        outputs=(
            Output(Placement.SETTINGS_ENV, Tier.MANAGED),
            Output(Placement.SETTINGS_ENV, Tier.USER),
        ),
        doc="Corporate CA/PEM for Claude Code (Node). Written only when a CA bundle is configured.",
    ),
    ConfigField(
        "REQUESTS_CA_BUNDLE", Category.TLS, Placement.SETTINGS_ENV, Status.OWNED, tier=Tier.MANAGED,
        baked_from="CA_BUNDLE", env_override="GATEWAY_CLI_CA_BUNDLE",
        value_kind=ValueKind.PATH, validate="path",
        outputs=(Output(Placement.SETTINGS_ENV, Tier.MANAGED),),
        doc="Corporate CA for python/requests helpers; also applied to this process (setdefault).",
    ),
    ConfigField(
        "AWS_CA_BUNDLE", Category.TLS, Placement.SETTINGS_ENV, Status.OWNED, tier=Tier.MANAGED,
        baked_from="CA_BUNDLE", env_override="GATEWAY_CLI_CA_BUNDLE",
        value_kind=ValueKind.PATH, validate="path",
        outputs=(Output(Placement.SETTINGS_ENV, Tier.MANAGED),),
        doc="Corporate CA for boto3/AWS helpers; also applied to this process (setdefault).",
    ),
    ConfigField(
        "SSL_CERT_FILE", Category.TLS, Placement.SETTINGS_ENV, Status.OWNED, tier=Tier.MANAGED,
        baked_from="CA_BUNDLE", env_override="GATEWAY_CLI_CA_BUNDLE",
        value_kind=ValueKind.PATH, validate="path",
        outputs=(Output(Placement.SETTINGS_ENV, Tier.MANAGED),),
        doc="Corporate CA for OpenSSL-based helpers; also applied to this process (setdefault).",
    ),
    ConfigField(
        "CURL_CA_BUNDLE", Category.TLS, Placement.PROCESS_ENV, Status.OWNED, tier=Tier.NONE,
        env_override="GATEWAY_CLI_CA_BUNDLE", value_kind=ValueKind.PATH, validate="path",
        outputs=(Output(Placement.PROCESS_ENV),),
        doc="Corporate CA applied to this process only (apply_ca_bundle setdefault); not persisted.",
    ),

    # ── Guardrails ──────────────────────────────────────────────────────────
    ConfigField(
        "ANTHROPIC_CUSTOM_HEADERS", Category.GUARDRAILS, Placement.SETTINGS_ENV, Status.PASSTHROUGH,
        tier=Tier.MANAGED, site_extra_overridable=True, sensitive=True,
        outputs=(Output(Placement.SETTINGS_ENV, Tier.MANAGED),),
        doc="Custom request headers (e.g. Bedrock Guardrails) — supplied via site_extra. "
            "Sensitive: a header bag may carry an Authorization/shared-secret value, so it is "
            "redacted in diagnostics (R4).",
    ),

    # ── UI / presentation ───────────────────────────────────────────────────
    ConfigField(
        "statusLine", Category.UI, Placement.SETTINGS_TOP, Status.OWNED, tier=Tier.MANAGED,
        value_kind=ValueKind.COMMAND, flag="--statusline",
        outputs=(Output(Placement.SETTINGS_TOP, Tier.MANAGED),),
        doc="Status-line command; also removed from user settings by reconcile so managed wins.",
    ),
    ConfigField(
        "permissions", Category.UI, Placement.SETTINGS_TOP, Status.PASSTHROUGH, tier=Tier.EITHER,
        value_kind=ValueKind.JSON_OBJECT, compose=Compose.MERGE_OBJECT,
        site_extra_overridable=True,
        outputs=(
            Output(Placement.SETTINGS_TOP, Tier.MANAGED),
            Output(Placement.SETTINGS_TOP, Tier.USER),
        ),
        doc="Allow/deny permission rules — supplied via site_extra.",
    ),
)


# --- Catalog lookups --------------------------------------------------------

_BY_KEY = {f.key: f for f in FIELDS}


def by_key(key: str) -> ConfigField | None:
    """Return the catalog entry for ``key``, or None if not catalogued."""
    return _BY_KEY.get(key)


def by_category(category: Category) -> tuple[ConfigField, ...]:
    """All catalog entries in one category."""
    return tuple(f for f in FIELDS if f.category is category)


def by_status(status: Status) -> tuple[ConfigField, ...]:
    """All catalog entries with a given status."""
    return tuple(f for f in FIELDS if f.status is status)


def owned() -> tuple[ConfigField, ...]:
    """Config objects gateway-cli actively writes."""
    return by_status(Status.OWNED)


def bypass_keys() -> tuple[str, ...]:
    """Gateway-bypassing keys (removed from settings and/or reported by verify)."""
    return tuple(f.key for f in FIELDS if f.status is Status.BYPASS)


def sensitive_keys() -> tuple[str, ...]:
    """Credential-bearing keys that must never be printed in full (R4 redaction).

    Single source for the redaction set consumed by ``config --explain`` (T5.4)
    and the redaction golden test (T5.5). In catalog declaration order.
    """
    return tuple(f.key for f in FIELDS if f.sensitive)


def os_persisted_keys() -> tuple[str, ...]:
    """Keys ``setup`` / ``env --persist`` also write as User-scope OS env vars.

    Single source for the operator-var set duplicated between ``cli.env`` (which
    reads them from the environment) and ``cli.setup`` (which registers them). In
    catalog declaration order.
    """
    return tuple(f.key for f in FIELDS if f.os_persist)


# ---------------------------------------------------------------------------
# Settings hierarchy (highest precedence first)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tierlevel:
    """One level of Claude Code's settings/precedence hierarchy."""

    rank: int
    name: str
    location: str
    note: str = ""


# From managed.py's docstring + docs/FILE_AND_ENV_OPERATIONS.md. An actual
# OS/shell env var beats every settings file (Claude Code behaviour, outside this
# tool's control); among managed drop-ins the lexicographically-last filename
# wins, so gateway-cli keeps its keys in 50-gateway.json and OTEL in the same file.
SETTINGS_HIERARCHY: tuple[Tierlevel, ...] = (
    Tierlevel(0, "OS / shell environment variable",
              "process environment",
              "Beats every settings file. gateway-cli detects but does not remove these."),
    Tierlevel(1, "Managed drop-in fragment",
              "…/ClaudeCode/managed-settings.d/50-gateway.json",
              "Beats the primary managed file; its env merges key-by-key over it. "
              "Lexicographically-last drop-in filename wins."),
    Tierlevel(2, "Primary managed settings",
              "…/ClaudeCode/managed-settings.json",
              "Enterprise tier — gateway-cli deep-merges its owned keys here."),
    Tierlevel(3, "Command-line arguments", "claude … --setting", ""),
    Tierlevel(4, "Project-local settings", ".claude/settings.local.json", ""),
    Tierlevel(5, "Project settings", ".claude/settings.json", ""),
    Tierlevel(6, "User settings", "~/.claude/settings.json",
              "gateway-cli's fallback tier when the managed file cannot be written."),
)


# ---------------------------------------------------------------------------
# Key filesystem locations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Location:
    """A filesystem location gateway-cli reads or writes, per platform."""

    name: str
    #: platform key ("windows"/"macos"/"linux"/"wsl") → path template
    paths: dict[str, str]
    env_override: str | None = None
    doc: str = ""


# Mirrors the "Key locations" table in docs/FILE_AND_ENV_OPERATIONS.md.
LOCATIONS: tuple[Location, ...] = (
    Location(
        "data_dir",
        {
            "windows": r"%LOCALAPPDATA%\gateway-cli\\",
            "macos": "~/Library/Application Support/gateway-cli/",
            "linux": "~/.local/share/gateway-cli/",
        },
        env_override="GATEWAY_CLI_DATA_DIR",
        doc="gateway-cli's own data directory (token caches, backups).",
    ),
    Location(
        "backups_dir",
        {"all": "<data_dir>/backups/"},
        env_override="GATEWAY_CLI_BACKUP_DIR",
        doc="Timestamped, never-overwritten config backups (mode 0700).",
    ),
    Location(
        "managed_root",
        {
            "windows": r"C:\Program Files\ClaudeCode\\",
            "macos": "/Library/Application Support/ClaudeCode/",
            "linux": "/etc/claude-code/",
            "wsl": "/mnt/c/Program Files/ClaudeCode/ (Windows Claude) or /etc/claude-code/ (native-Linux Claude)",
        },
        doc="Enterprise managed-settings root (admin/sudo to write).",
    ),
    Location(
        "managed_primary",
        {"all": "<managed_root>/managed-settings.json"},
        doc="Primary managed settings file (deep-merged; org keys preserved).",
    ),
    Location(
        "managed_dropin",
        {"all": "<managed_root>/managed-settings.d/50-gateway.json"},
        doc="gateway-cli's managed drop-in fragment (beats the primary file).",
    ),
    Location(
        "user_settings",
        {"all": "~/.claude/settings.json"},
        env_override="GATEWAY_CLI_SETTINGS_PATH",
        doc="User settings — fallback tier + where reconcile strips bypass keys.",
    ),
    Location(
        "oidc_cache",
        {"all": "<data_dir>/oidc-tokens.json"},
        env_override="GATEWAY_CLI_OIDC_CACHE",
        doc="OIDC access/refresh/id tokens (chmod 0600; disposable).",
    ),
    Location(
        "vk_cache",
        {"all": "<data_dir>/vk-cache.json"},
        env_override="GATEWAY_CLI_VK_CACHE",
        doc="Virtual Key cache from /v1/auth/exchange (chmod 0600; disposable).",
    ),
)


# ---------------------------------------------------------------------------
# Per-key precedence rules (the non-obvious "who wins" cases)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrecedenceRule:
    """Who wins for a specific key or key family, highest source first."""

    keys: tuple[str, ...]
    order: tuple[str, ...]
    doc: str


PRECEDENCE: tuple[PrecedenceRule, ...] = (
    PrecedenceRule(
        ("OTEL_EXPORTER_OTLP_ENDPOINT",),
        ("--otel-endpoint CLI arg", "site-extra managed.env", "auto-derived from gateway host"),
        "Base OTLP endpoint; per-signal logs/metrics/traces endpoints derive from the winner. "
        "See docs/OTEL_PRECEDENCE.md.",
    ),
    PrecedenceRule(
        ("OTEL_*",),
        ("site-extra managed.env", "gateway-cli computed"),
        "OTEL_*-prefixed keys are the deliberate exception where site-extra beats gateway-cli, "
        "so a site can pin its own collector. OTEL_RESOURCE_ATTRIBUTES.user.id stays dynamic.",
    ),
    PrecedenceRule(
        ("NO_PROXY",),
        ("gateway-cli build_gateway_env", "site-extra managed.env"),
        "gateway-cli owns NO_PROXY and overwrites any site-extra value. See docs/PROXY_PRECEDENCE.md.",
    ),
    PrecedenceRule(
        ("HTTP_PROXY", "HTTPS_PROXY"),
        ("site-extra managed.env",),
        "gateway-cli does not set these; the site controls the forward-proxy address.",
    ),
)


# ---------------------------------------------------------------------------
# Resolved gateway/OIDC configuration (replaces the OnboardingCard dict layer)
# ---------------------------------------------------------------------------

#: JSON keys required before `setup` can run, in the order setup reports them.
SETUP_REQUIRED_KEYS: tuple[str, ...] = (
    "gatewayUrl",
    "adminApiUrl",
    "oidcIssuerUrl",
    "oidcClientId",
)

#: Map the camelCase JSON keys used across the CLI ↔ the ResolvedConfig fields.
#: Single-sources the naming the old card dict+property layer duplicated.
_JSON_TO_FIELD: dict[str, str] = {
    "gatewayUrl": "gateway_url",
    "adminApiUrl": "admin_api_url",
    "oidcIssuerUrl": "oidc_issuer_url",
    "oidcClientId": "oidc_client_id",
    "email": "email",
}


@dataclass(frozen=True)
class ResolvedConfig:
    """The effective gateway/OIDC values for one command invocation.

    A direct, immutable value object — no disk I/O, no property indirection. It
    is the resolved counterpart to the OWNED gateway/auth entries in :data:`FIELDS`:
    baked corporate defaults < ``GATEWAY_CLI_*`` env overrides < explicit flags
    (resolution lives in ``site_defaults``; this only holds the result).
    """

    gateway_url: str = ""
    admin_api_url: str = ""
    oidc_issuer_url: str = ""
    oidc_client_id: str = ""
    email: str = ""

    @classmethod
    def from_values(cls, values: dict[str, str]) -> "ResolvedConfig":
        """Build from a camelCase values dict (blanks ignored)."""
        kwargs: dict[str, str] = {}
        for json_key, field_name in _JSON_TO_FIELD.items():
            value = values.get(json_key)
            if value is not None and str(value).strip():
                kwargs[field_name] = str(value).strip()
        return cls(**kwargs)

    def as_values(self) -> dict[str, str]:
        """Back to a camelCase values dict (blanks dropped) for legacy call sites."""
        out: dict[str, str] = {}
        for json_key, field_name in _JSON_TO_FIELD.items():
            value = getattr(self, field_name)
            if value:
                out[json_key] = value
        return out

    def missing_for_setup(self) -> list[str]:
        """camelCase keys still blank that `setup` requires, in report order."""
        values = self.as_values()
        return [k for k in SETUP_REQUIRED_KEYS if not values.get(k, "").strip()]
