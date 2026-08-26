# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Drift guard: manifest ``flag`` ↔ the CLI's Click options stay in sync.

A field that declares a ``flag`` (e.g. ``--gateway-url``) promises the user can
set it from the command line; the actual ``@click.option`` lives in ``main.py``
(and ``env.py`` for the ``env`` sub-command). These must never diverge:

* every manifest ``flag`` must resolve to a real Click option (else the catalog
  advertises a CLI knob that does not exist), and
* every Click option must either map to a manifest ``flag`` or be an explicitly
  listed *operational* option — one that steers command flow rather than setting
  a catalogued config value (else a newly-added config option that forgot its
  manifest entry slips through uncatalogued).

AR-F3: :data:`cli.manifest.PRECEDENCE` names CLI args as precedence sources
(e.g. ``"--otel-endpoint CLI arg"`` is the top source for
``OTEL_EXPORTER_OTLP_ENDPOINT``). The plain field-with-flag check would let a
*phantom* precedence source slip through — a doc that names ``--otel-endpoint``
even if no such option/flag existed. So we additionally assert every
``--``-prefixed token named in a PRECEDENCE rule maps to BOTH a real Click option
AND a real ``field.flag``.
"""

from __future__ import annotations

import re
from pathlib import Path

from cli import manifest

_PKG_ROOT = Path(__file__).resolve().parents[1]  # …/entrypoints/gateway-cli-v2
_CLI_DIR = _PKG_ROOT / "src" / "cli"

#: Click options that intentionally have no catalogued ConfigField because they
#: steer command flow rather than set a Claude Code config value. Kept explicit
#: so a *new* config option that forgets its manifest ``flag`` fails the reverse
#: check instead of hiding here.
_OPERATIONAL_OPTIONS: frozenset[str] = frozenset(
    {
        "--verbose",          # global: debug logging
        "--redirect-port",    # login/onboard: OIDC callback port
        "--timeout",          # login/onboard: OIDC callback wait
        "--no-browser",       # login: force headless email+password
        "--email",            # recorded into ResolvedConfig; not a written config key
        "--password-stdin",   # login: read headless password from stdin (credential intake)
        "--persist-env",      # setup: also write OS env vars (mode toggle)
        "--no-persist-env",   # setup: negative half of --persist-env
        "--persist",          # env: same OS-env persist toggle
        "--explain",          # config: print resolved fields + sources (view mode)
        "--keep-tokens",      # clear: skip the token/VK step (scope toggle)
        "--keep-os-env",      # clear: skip the OS-env revert step (scope toggle)
        "--dry-run",          # clear/uninstall: print the plan, change nothing
        "--yes",              # clear/uninstall: skip the confirmation prompt
        "--clear-first",      # uninstall: run clear in-process before delegating
        "--post-teardown",    # verify: assert teardown left no residue (view mode)
    }
)


def _click_option_flags() -> set[str]:
    """Every long (``--``) option declared via ``@click.option`` in the CLI.

    Parses the first positional string of each ``@click.option(...)`` decorator
    (the option declaration, e.g. ``"--persist-env/--no-persist-env"`` or
    ``"--verbose"``) so help-text mentions of a flag are not miscounted as
    definitions.
    """
    flags: set[str] = set()
    for name in ("main.py", "env.py"):
        text = (_CLI_DIR / name).read_text(encoding="utf-8")
        for decl in re.findall(r'@click\.option\(\s*"([^"]+)"', text):
            flags.update(re.findall(r"--[a-zA-Z0-9][a-zA-Z0-9-]*", decl))
    return flags


def _manifest_flags() -> set[str]:
    """Every CLI flag the catalog says a field is settable by."""
    return {f.flag for f in manifest.FIELDS if f.flag}


def _precedence_named_flags() -> set[str]:
    """Every ``--`` token named as a precedence source in PRECEDENCE rules."""
    flags: set[str] = set()
    for rule in manifest.PRECEDENCE:
        for source in rule.order:
            flags.update(re.findall(r"--[a-zA-Z0-9][a-zA-Z0-9-]*", source))
    return flags


def test_every_manifest_flag_has_a_click_option() -> None:
    """A field's declared ``flag`` must resolve to a real Click option."""
    manifest_flags = _manifest_flags()
    assert manifest_flags, "expected the catalog to declare CLI flags (--gateway-url, …)"
    click_flags = _click_option_flags()
    assert click_flags, "parsed no @click.option declarations — regex/format drift?"
    missing = manifest_flags - click_flags
    assert not missing, (
        f"manifest flags with no matching @click.option: {sorted(missing)} "
        f"(CLI defines {sorted(click_flags)})"
    )


def test_every_click_option_maps_to_a_field_or_is_operational() -> None:
    """Reverse: a Click option is either a catalogued flag or a listed op flag."""
    click_flags = _click_option_flags()
    manifest_flags = _manifest_flags()
    unmapped = click_flags - manifest_flags - _OPERATIONAL_OPTIONS
    assert not unmapped, (
        f"Click options that map to no manifest field and are not listed as "
        f"operational: {sorted(unmapped)} — add a manifest ``flag`` for the "
        f"field they set, or list them in _OPERATIONAL_OPTIONS with a reason"
    )


def test_precedence_named_args_are_real_options_and_flags() -> None:
    """AR-F3: every CLI arg named in PRECEDENCE is a real option AND a field.flag.

    Guards against a phantom precedence source — a rule that names, say,
    ``--otel-endpoint`` while no such Click option or manifest ``flag`` exists.
    """
    named = _precedence_named_flags()
    assert "--otel-endpoint" in named, (
        "expected the OTEL_EXPORTER_OTLP_ENDPOINT precedence rule to name the "
        "--otel-endpoint CLI arg — has PRECEDENCE drifted?"
    )
    click_flags = _click_option_flags()
    manifest_flags = _manifest_flags()
    for flag in sorted(named):
        assert flag in click_flags, (
            f"PRECEDENCE names {flag!r} as a source but no @click.option defines it"
        )
        assert flag in manifest_flags, (
            f"PRECEDENCE names {flag!r} as a source but no manifest field declares "
            f"flag={flag!r} — phantom precedence source"
        )
