# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Drift guard: the manifest catalog must match what ``build_gateway_env`` emits.

``managed.build_gateway_env`` is the authoritative runtime source for the exact
env-var block gateway-cli writes into Claude Code's **managed** settings tier.
The manifest (:data:`cli.manifest.FIELDS`) is the declarative catalog that later
phases (resolve/emit, config --explain) trust. These two must never diverge:

* every key the writer can emit must be catalogued as :class:`Status.OWNED`
  (else a newly-emitted key would be invisible to the catalog), and
* every OWNED field the catalog says lands in the **managed** ``env`` block must
  actually be emitted by the writer (else the catalog over-claims).

AR-F2 boundary: this guard trusts the manifest ``outputs`` for the **managed**
tier ONLY. The user-tier ``user.id`` reconcile of ``OTEL_RESOURCE_ATTRIBUTES``
happens in ``setup.py`` (not ``build_gateway_env``) and is modelled as a
conditional user-tier Output; this guard must neither expect nor forbid it here,
so the equality checks are scoped to ``Placement.SETTINGS_ENV`` + ``Tier.MANAGED``.
"""

from __future__ import annotations

from cli import managed, manifest
from cli.manifest import Placement, Status, Tier


def _fully_populated_env() -> dict[str, str]:
    """The maximal env the writer emits — every optional branch (CA, OTEL,
    auth token, user.id) supplied so all conditional keys appear."""
    return managed.build_gateway_env(
        gateway_url="https://gw.example.com",
        admin_api_url="https://admin.example.com",
        otel_endpoint="http://collector.example.com:4318",
        otel_auth_token="secret-token",
        user_id="alice@corp",
        ca_bundle="/etc/ssl/corp.pem",
    )


def _manifest_managed_env_keys() -> set[str]:
    """OWNED fields the catalog says land in the managed settings ``env`` block."""
    return {
        f.key
        for f in manifest.owned()
        if any(
            o.placement is Placement.SETTINGS_ENV and o.tier is Tier.MANAGED
            for o in f.outputs
        )
    }


def test_every_emitted_key_is_owned() -> None:
    """The primary gate: the writer must not emit a key absent from the catalog."""
    for key in _fully_populated_env():
        entry = manifest.by_key(key)
        assert entry is not None, f"build_gateway_env emits uncatalogued key {key!r}"
        assert entry.status is Status.OWNED, (
            f"{key!r} is emitted by build_gateway_env but catalogued as "
            f"{entry.status.value!r}, expected OWNED"
        )


def test_manifest_managed_env_matches_writer() -> None:
    """Catalog's managed-tier env set == the writer's maximal emission (both ways)."""
    emitted = set(_fully_populated_env())
    catalogued = _manifest_managed_env_keys()
    missing_from_manifest = emitted - catalogued
    over_claimed_by_manifest = catalogued - emitted
    # NO_PROXY is catalogued unconditionally (it IS emitted in a site build), but
    # the writer drops it in a bare build where the baked NO_PROXY_VALUE is blank
    # — no proxy topology is baked, so put() emits nothing (see managed.py). This
    # is not catalog drift: the manifest is build-agnostic while emission is gated
    # on the baked value. Exempt NO_PROXY from the over-claim check only then.
    if not managed.NO_PROXY_VALUE.strip():
        over_claimed_by_manifest -= {"NO_PROXY"}
    assert not missing_from_manifest, (
        f"writer emits managed env keys the catalog omits: {sorted(missing_from_manifest)}"
    )
    assert not over_claimed_by_manifest, (
        f"catalog claims managed env keys the writer never emits: "
        f"{sorted(over_claimed_by_manifest)}"
    )


def test_otel_surface_is_fully_catalogued() -> None:
    """Every OTEL_* key the writer emits is OWNED (T1.3b prerequisite for this guard)."""
    otel = [k for k in _fully_populated_env() if k.startswith("OTEL_")]
    assert otel, "expected build_gateway_env to emit OTEL_* keys with full inputs"
    for key in otel:
        entry = manifest.by_key(key)
        assert entry is not None and entry.status is Status.OWNED, key


def test_user_tier_user_id_write_is_out_of_writer_scope() -> None:
    """AR-F2: the conditional user-tier user.id write is NOT a writer emission.

    ``OTEL_RESOURCE_ATTRIBUTES`` carries a managed Output (emitted by the writer)
    AND a conditional user-tier Output that ``setup.py`` applies only when
    site-extra already seeded the key. This guard must recognise the managed copy
    as writer-emitted while treating the user-tier copy as out of scope — it must
    not require ``build_gateway_env`` to emit a user-tier value.
    """
    attr = manifest.by_key("OTEL_RESOURCE_ATTRIBUTES")
    assert attr is not None
    managed_outputs = [
        o for o in attr.outputs
        if o.placement is Placement.SETTINGS_ENV and o.tier is Tier.MANAGED
    ]
    user_outputs = [
        o for o in attr.outputs
        if o.placement is Placement.SETTINGS_ENV and o.tier is Tier.USER
    ]
    # Managed copy is unconditional and thus part of the writer's emission.
    assert managed_outputs and all(o.condition is None for o in managed_outputs)
    # User-tier copy exists but is gated by a condition — never emitted here.
    assert user_outputs and all(o.condition == "user_tier_seeded" for o in user_outputs)
    # And the writer does emit the managed copy when a user_id is supplied.
    assert "OTEL_RESOURCE_ATTRIBUTES" in _fully_populated_env()
