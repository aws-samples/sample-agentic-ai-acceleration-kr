# Proxy env-var precedence

`gateway-cli` configures Claude Code's outbound HTTP proxy behavior by writing
`HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY` env vars into the enterprise
managed-settings file and its `managed-settings.d/50-gateway.json` drop-in.

On the isolated corporate network these three variables decide **which traffic
goes through the forward proxy and which is reached directly**. Getting them
wrong breaks Claude Code either way: too broad a proxy and internal endpoints
(the gateway, OTEL collector, OIDC) are sent through a proxy that cannot reach
them; too broad a `NO_PROXY` and outbound Anthropic/model traffic never leaves
the network. This document defines where each value comes from and which source
wins.

## The three keys

| Key | Owner | Purpose |
|---|---|---|
| `HTTP_PROXY` / `HTTPS_PROXY` | site-extra `managed.env` | Forward proxy for outbound (external) HTTP/HTTPS. |
| `NO_PROXY` | **gateway-cli (`build_gateway_env`)** | Comma-separated hosts/domains/CIDRs reached **directly**, bypassing the proxy. |

`HTTP_PROXY` / `HTTPS_PROXY` are **not** set by gateway-cli itself — they are
supplied by a corporate site through `site_extra.json`. `NO_PROXY` **is** owned
by gateway-cli and set unconditionally in `cli.managed.build_gateway_env`.

## `NO_PROXY` is owned by gateway-cli

`build_gateway_env` (in `cli/managed.py`) always writes:

```
NO_PROXY = localhost,10.0.0.0/8,192.168.0.0/16,.example.com
```

This guarantees the internal gateway, admin API, OTEL collector and OIDC
endpoints are reached directly rather than pushed through the forward proxy. It
covers the corporate domain suffixes and the RFC-1918 / internal CIDR ranges so
that any internal host resolves direct regardless of the proxy setting.

## Precedence order (highest first)

During the managed-settings merge in `cli.managed.write_gateway_settings`,
site-extra `managed.env` is merged **first**, then the gateway-owned env is
layered on top:

1. **gateway-cli's `NO_PROXY`** — the hardcoded value above wins over any
   `NO_PROXY` a site sets in `site_extra.json`. Unlike the `OTEL_*` keys,
   `NO_PROXY` is **not** on the site-overridable exception list, so a site-extra
   `NO_PROXY` is overwritten.
2. **site-extra `managed.env.HTTP_PROXY` / `HTTPS_PROXY`** — since gateway-cli
   does not set these, the site-extra values pass through unchanged and are what
   Claude Code actually uses.

In short: **the site controls the proxy address (`*_PROXY`); gateway-cli
controls the bypass list (`NO_PROXY`).**

## How to set the forward proxy

Set the proxy in site-extra `managed.env`:

```json
{
  "managed": {
    "env": {
      "HTTP_PROXY": "http://proxy.example.com:8080",
      "HTTPS_PROXY": "http://proxy.example.com:8080"
    }
  }
}
```

The resulting `managed-settings.json` and `50-gateway.json` will carry these two
values alongside the gateway-owned `NO_PROXY`.

> **Do not add `NO_PROXY` here.** A site-extra `NO_PROXY` is silently overwritten
> by gateway-cli's value (see precedence above). If the bypass list must change,
> edit `NO_PROXY` in `build_gateway_env` (`cli/managed.py`) — that is the single
> authoritative source.
>
> **Do not put the forbidden corporate suffix (`FORBIDDEN_NO_PROXY_TOKEN`) in
> `NO_PROXY`.** The gateway, admin API and OIDC endpoints live under that suffix
> but are fronted such that they must **not** be listed as direct — they are
> covered by the internal CIDR ranges instead. The owned `NO_PROXY` value
> deliberately lists the allowed corporate suffixes only, never the forbidden one.

## Drop-in tie-break — a later-sorting fragment can override us

gateway-cli writes its keys to `managed-settings.d/50-gateway.json`. A drop-in
beats the primary `managed-settings.json`, and among drop-ins **the
lexicographically-last filename wins** — its `env` merges key-by-key over the
earlier fragments. So a fragment whose name sorts *after* `50-gateway.json` can
override the gateway/OTEL/proxy keys we wrote, silently breaking routing:

- `99-anything.json` (a higher numeric prefix), or
- `50-otel.json` — same prefix, but `o` > `g`, so it sorts after `50-gateway`.

gateway-cli **never renames or deletes a drop-in another party authored** (it
only removes its own `50-gateway.json` and the legacy `99-gateway.json` it used
to write). Instead, at write time it scans `managed-settings.d` and logs a
`gateway_dropin_may_be_shadowed` warning naming any foreign fragment that sorts
after ours (see `_shadowing_dropins` in `cli/managed.py`). If you see that
warning:

1. Confirm whether the later-sorting fragment actually sets any of our keys
   (`NO_PROXY`, `ANTHROPIC_BASE_URL`, the `OTEL_*` endpoints, …).
2. If it does and that is not intended, **rename it to sort before
   `50-gateway.json`** (e.g. `40-org.json`) so gateway-cli's values win.

Keep gateway/OTEL/proxy keys in `50-gateway.json` — do not move them into a
later-sorting file, or another drop-in could shadow them.

## Caveat outside this tool's control

An actual OS/shell environment variable of the same name (`HTTP_PROXY`,
`HTTPS_PROXY`, `NO_PROXY`) still takes precedence over any settings file. That is
a Claude Code / OS behavior, not something gateway-cli can override — document it
for operators, and make sure a pre-existing shell `NO_PROXY` does not re-introduce
the forbidden corporate suffix or drop the internal CIDR ranges.
