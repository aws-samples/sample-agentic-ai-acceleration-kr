# OTEL endpoint precedence

`gateway-cli` configures Claude Code's client-side OpenTelemetry (metrics, traces,
logs) by writing `OTEL_*` env vars into the enterprise managed-settings file and
its `managed-settings.d/50-gateway.json` drop-in.

This document defines **which OTEL collector endpoint wins** when more than one
source specifies it. Corporate sites on isolated networks must be able to force
their own collector, so site-extra takes precedence over the tool's default.

## Precedence order (highest first)

The base collector endpoint (`OTEL_EXPORTER_OTLP_ENDPOINT`) is resolved in
`cli.setup.run_setup`, highest priority first:

1. **`--otel-endpoint` CLI argument** — an explicit value passed to `run_setup`.
2. **site-extra `managed.env.OTEL_EXPORTER_OTLP_ENDPOINT`** — the value bundled in
   `site_extra.json` (or the file pointed to by the `GATEWAY_CLI_SITE_EXTRA`
   environment variable). This is how a corporate site pins its own collector.
3. **Auto-derived from the gateway hostname** — `http://<gateway-hostname>:80`,
   used only when neither of the above is set.

Whichever endpoint wins is fed into `build_gateway_env`, so **every per-signal
endpoint is derived from the same base**:

- `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT`    = `<base>/v1/logs`
- `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` = `<base>/v1/metrics`
- `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`  = `<base>/v1/traces`

This avoids a base/derived split where the base points at the corporate collector
but the per-signal endpoints still point at the gateway host.

## Per-key overrides

During the managed-settings merge in `cli.managed.write_gateway_settings`, the
gateway/routing env keys (`ANTHROPIC_BASE_URL`, `GATEWAY_CLI_GATEWAY_URL`, the TLS
CA bundle vars, …) always override org-provided values of the same name — those
are owned by gateway-cli and must not be broken by site config.

**`OTEL_`-prefixed keys are the exception.** Any `OTEL_*` key present in site-extra
`managed.env` wins over gateway-cli's computed value. So beyond the base endpoint,
an operator may also override an individual per-signal endpoint (e.g. send traces
to a different host than metrics) simply by setting that specific key in
site-extra.

## How to pin a corporate collector

Set the endpoint in site-extra `managed.env`:

```json
{
  "managed": {
    "env": {
      "OTEL_EXPORTER_OTLP_ENDPOINT": "http://otel-collector.example.com:4318"
    }
  }
}
```

The resulting `50-gateway.json` will carry that endpoint plus the three derived
per-signal endpoints, all pointing at the corporate collector.

## Caveat outside this tool's control

An actual OS/shell environment variable of the same name still takes precedence
over any settings file. That is a Claude Code behavior, not something gateway-cli
can override — document it for operators.
