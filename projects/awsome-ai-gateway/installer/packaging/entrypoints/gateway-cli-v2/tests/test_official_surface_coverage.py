# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""TD.1 (C2/C3): every official Bedrock/Mantle env var is accounted for in the manifest.

``docs/official_surface.csv`` (seeded by TD.0) transcribes the environment-variable
surface from the official Claude Code Amazon Bedrock docs, one row per key, each
tagged with how gateway-cli treats it:

* ``owned`` / ``passthrough`` / ``bypass`` / ``documented`` — the key MUST be
  catalogued in :data:`cli.manifest.FIELDS`, and its manifest ``Status`` must
  match the tag; and
* ``gap`` — a deliberately un-modelled official key (generic AWS SDK credential
  material, or a non-Bedrock var gateway-cli fixes itself). It MUST be absent
  from the manifest, so a ``gap`` row can never mask a key we actually catalogue.

Together these fail the moment an official key drifts: a new key catalogued but
left ``gap`` in the CSV, a key tagged ``documented`` but never added to FIELDS, or
a status that no longer matches. Adding a genuinely new official env var to the
docs means adding a row here, which forces a conscious classification.
"""

from __future__ import annotations

import csv
from pathlib import Path

from cli import manifest

_PKG_ROOT = Path(__file__).resolve().parents[1]  # …/entrypoints/gateway-cli-v2
_SURFACE_CSV = _PKG_ROOT / "docs" / "official_surface.csv"

#: The classification vocabulary a row may use (TD.1 status set).
_MANIFEST_STATUSES = {"owned", "passthrough", "bypass", "documented"}
_VALID_STATUSES = _MANIFEST_STATUSES | {"gap"}


def _rows() -> list[dict[str, str]]:
    with _SURFACE_CSV.open(encoding="utf-8", newline="") as fh:
        return [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(fh)]


def _manifest_status_by_key() -> dict[str, str]:
    return {f.key: f.status.value for f in manifest.FIELDS}


def test_surface_csv_is_well_formed() -> None:
    """The seed file parses, lists real keys, and uses only known statuses."""
    rows = _rows()
    assert rows, f"{_SURFACE_CSV} has no rows — TD.0 seed missing?"
    keys = [r["key"] for r in rows]
    assert all(keys), "every row must name a key"
    assert len(keys) == len(set(keys)), (
        f"duplicate keys in {_SURFACE_CSV.name}: "
        f"{sorted(k for k in keys if keys.count(k) > 1)}"
    )
    bad = {r["key"]: r["status"] for r in rows if r["status"] not in _VALID_STATUSES}
    assert not bad, f"rows with an unknown status (allowed: {sorted(_VALID_STATUSES)}): {bad}"


def test_catalogued_keys_are_in_manifest_with_matching_status() -> None:
    """Any non-gap official key must exist in FIELDS with the tagged status."""
    manifest_status = _manifest_status_by_key()
    mismatched: list[str] = []
    for row in _rows():
        key, status = row["key"], row["status"]
        if status == "gap":
            continue
        actual = manifest_status.get(key)
        if actual != status:
            mismatched.append(f"{key}: surface={status!r} manifest={actual!r}")
    assert not mismatched, (
        "official keys uncatalogued or with drifted status — add/fix the manifest "
        "ConfigField or correct docs/official_surface.csv:\n  " + "\n  ".join(mismatched)
    )


def test_gap_keys_are_absent_from_manifest() -> None:
    """A key tagged ``gap`` must not be catalogued (else the CSV is stale)."""
    catalogued = set(_manifest_status_by_key())
    leaked = [r["key"] for r in _rows() if r["status"] == "gap" and r["key"] in catalogued]
    assert not leaked, (
        f"keys tagged 'gap' but present in the manifest: {leaked} — re-tag them with "
        "their real status (owned/passthrough/bypass/documented)"
    )


def test_t1_3_named_vars_are_catalogued() -> None:
    """The five C2 vars T1.3 was scoped to add are present and documented."""
    manifest_status = _manifest_status_by_key()
    for key in (
        "CLAUDE_CODE_DISABLE_BEDROCK_CONTENT_TYPE_GUARD",
        "ANTHROPIC_SMALL_FAST_MODEL",
        "AWS_DEFAULT_REGION",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_CONFIG_FILE",
    ):
        assert manifest_status.get(key) == "documented", f"{key} missing/miscatalogued"
