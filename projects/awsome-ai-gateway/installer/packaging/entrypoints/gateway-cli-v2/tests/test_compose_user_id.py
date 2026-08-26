# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""The R1 / Fix-1 user.id invariant for MERGE_ATTRS resolution (T4.8, gates T4.3).

``OTEL_RESOURCE_ATTRIBUTES`` composes a site-pinned attribute map with a
dynamically-resolved ``user.id``. The invariant, for BOTH tiers:

* a site's static ``service.name`` (and any other attrs) is preserved; and
* ``user.id`` is the authenticated identity, never the site's stale/baked value
  (gateway-cli-issues.md:255-257).

The tier distinction (AR-F2), encoded by ``resolve_merge(create_if_absent=...)``:

* managed (``True``) — user.id is injected even with no site seed, matching
  ``build_gateway_env`` (``set_resource_user_id(None, user_id)``); and
* user (``False``) — the attr is emitted ONLY when the site already seeded it at
  the user tier; unseeded ⇒ not injected, even when an identity resolved.
"""

from __future__ import annotations

import pytest

from cli import managed
from cli.manifest import by_key
from cli.resolve import Source, Sources, resolve_merge
from cli.resolvers import ResolveContext

_FIELD = by_key("OTEL_RESOURCE_ATTRIBUTES")
_ID = "alice@corp"
_SEED_STALE = "service.name=claude-code,user.id=stale@old"


def _resolve(*, seed: str | None, user_id: str | None, create_if_absent: bool):
    site = {"OTEL_RESOURCE_ATTRIBUTES": seed} if seed is not None else {}
    ctx = ResolveContext(user_id=user_id)
    return resolve_merge(
        _FIELD, Sources(site_extra=site, derive_ctx=ctx), create_if_absent=create_if_absent
    )


def test_managed_seeded_overwrites_user_id_keeps_service_name() -> None:
    res = _resolve(seed=_SEED_STALE, user_id=_ID, create_if_absent=True)
    assert res.value == {"service.name": "claude-code", "user.id": _ID}
    assert res.raw == "service.name=claude-code,user.id=alice@corp"
    assert res.source is Source.MERGE
    assert "stale@old" not in res.raw


def test_managed_unseeded_injects_user_id() -> None:
    """Matches build_gateway_env: no site seed ⇒ user.id created."""
    res = _resolve(seed=None, user_id=_ID, create_if_absent=True)
    assert res.value == {"user.id": _ID}
    assert res.raw == "user.id=alice@corp"


def test_managed_equals_set_resource_user_id() -> None:
    """The merged output equals the legacy managed writer for the same inputs."""
    res = _resolve(seed=_SEED_STALE, user_id=_ID, create_if_absent=True)
    assert res.raw == managed.set_resource_user_id(_SEED_STALE, _ID)


def test_user_tier_seeded_reconciles() -> None:
    """AR-F2 seeded: user.id overwritten, service.name kept, at the user tier."""
    res = _resolve(seed=_SEED_STALE, user_id=_ID, create_if_absent=False)
    assert res.value == {"service.name": "claude-code", "user.id": _ID}
    assert res.raw == "service.name=claude-code,user.id=alice@corp"


def test_user_tier_unseeded_not_injected_even_with_identity() -> None:
    """AR-F2 absent: no seed ⇒ key never injected at the user tier."""
    res = _resolve(seed=None, user_id=_ID, create_if_absent=False)
    assert res.value is None and res.raw is None and res.source is None


def test_user_tier_seeded_without_identity_leaves_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    """No resolvable identity ⇒ seeded user tier keeps its map (no crash, guarded)."""
    # No pre-resolved id ⇒ deriver falls to the live lookup; stub both so the
    # test never touches OIDC/STS and genuinely resolves to "no identity".
    import cli.resolvers as resolvers_mod
    import cli.setup as setup_mod

    monkeypatch.setattr(resolvers_mod, "resolve_login_user_id", lambda: None)
    monkeypatch.setattr(setup_mod, "_resolve_aws_user_id", lambda: None)
    res = _resolve(seed=_SEED_STALE, user_id=None, create_if_absent=False)
    # Nothing to stamp; the site map is preserved verbatim.
    assert res.value == {"service.name": "claude-code", "user.id": "stale@old"}
