# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Typed codecs for every :class:`cli.manifest.ValueKind`.

A **codec** turns a config value between two representations:

* ``parse(raw)`` — a raw string (env-var text, a CLI flag, a site-extra scalar)
  into the natural Python object for the kind (a ``str`` for scalars, a ``list``
  for ``STR_LIST``, a ``dict`` for ``JSON_OBJECT`` / ``ATTR_MAP``, a command
  record for ``COMMAND``); and
* ``encode(value, placement)`` — that Python object into the concrete fragment a
  given :class:`~cli.manifest.Placement` needs. Env placements
  (``SETTINGS_ENV`` / ``PROCESS_ENV`` / ``OS_ENV``) always want a **string** —
  env vars are strings — while ``SETTINGS_TOP`` wants the native JSON shape
  (``availableModels`` is a JSON array, ``permissions`` a JSON object). A
  scalar-only model could not represent those structured settings (finding R2),
  which is why the structured kinds exist.

This module is pure data transformation — no resolution, no composition, no
validation (those live in ``resolve``/``validators``). The emit phase (T5.x)
picks a codec by ``(value_kind, placement)`` and calls ``encode``; resolution
(T4.x) calls ``parse`` on each source value.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from cli.manifest import Placement, ValueKind

# Placements whose fragment must be a plain string (an environment variable).
_ENV_PLACEMENTS = (Placement.SETTINGS_ENV, Placement.PROCESS_ENV, Placement.OS_ENV)


def _is_env_placement(placement: Placement) -> bool:
    return placement in _ENV_PLACEMENTS


class Codec(Protocol):
    """parse(raw) → typed object; encode(typed, placement) → concrete fragment."""

    def parse(self, raw: str) -> Any: ...

    def encode(self, value: Any, placement: Placement) -> Any: ...


class ScalarCodec:
    """STR / URL / PATH / ENUM / MODEL_ALIAS — one string, placement-invariant.

    ``parse`` and ``encode`` are the identity on the string, so a scalar always
    round-trips (``encode(parse(x)) == x``). Normalisation (trailing-slash strip,
    etc.) is a resolver/deriver concern, not the codec's.
    """

    def parse(self, raw: str) -> str:
        return raw

    def encode(self, value: Any, placement: Placement) -> str:
        return value if isinstance(value, str) else str(value)


class CsvCodec:
    """CSV scalar (e.g. ``NO_PROXY``) — a comma-joined string ⇄ list of items."""

    def parse(self, raw: str) -> list[str]:
        return [part.strip() for part in raw.split(",") if part.strip()]

    def encode(self, value: Any, placement: Placement) -> str:
        items = value if isinstance(value, (list, tuple)) else [value]
        return ",".join(str(item) for item in items)


class StrListCodec:
    """STR_LIST (``availableModels``) — JSON array at SETTINGS_TOP, CSV in env.

    Accepts either a JSON array (``["a","b"]``) or a bare comma-separated string
    on ``parse`` so a site-extra scalar and a settings array both round-trip.
    """

    def parse(self, raw: str) -> list[str]:
        text = raw.strip()
        if text.startswith("["):
            data = json.loads(text)
            if not isinstance(data, list):
                raise ValueError("STR_LIST JSON must be an array")
            return [str(item) for item in data]
        return [part.strip() for part in text.split(",") if part.strip()]

    def encode(self, value: Any, placement: Placement) -> Any:
        items = list(value) if isinstance(value, (list, tuple)) else [value]
        items = [str(item) for item in items]
        if _is_env_placement(placement):
            return ",".join(items)
        return items  # SETTINGS_TOP → native JSON array


class JsonObjectCodec:
    """JSON_OBJECT (``permissions``) — a dict at SETTINGS_TOP, compact JSON in env."""

    def parse(self, raw: str) -> dict[str, Any]:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("JSON_OBJECT must be a JSON object")
        return data

    def encode(self, value: Any, placement: Placement) -> Any:
        if _is_env_placement(placement):
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return value  # SETTINGS_TOP → native JSON object


class CommandCodec:
    """COMMAND (``statusLine``) — ``{"type":"command","command": …}`` at SETTINGS_TOP.

    ``parse`` accepts either a bare command string or an already-shaped record
    (JSON object with a ``command`` key). In an env placement the bare command
    string is emitted.
    """

    def parse(self, raw: str) -> dict[str, str]:
        text = raw.strip()
        if text.startswith("{"):
            data = json.loads(text)
            if isinstance(data, dict) and "command" in data:
                return {"type": str(data.get("type", "command")), "command": str(data["command"])}
        return {"type": "command", "command": text}

    def encode(self, value: Any, placement: Placement) -> Any:
        if isinstance(value, dict):
            record = {"type": str(value.get("type", "command")), "command": str(value.get("command", ""))}
        else:
            record = {"type": "command", "command": str(value)}
        if _is_env_placement(placement):
            return record["command"]
        return record


class AttrMapCodec:
    """ATTR_MAP (``OTEL_RESOURCE_ATTRIBUTES``) — comma-joined ``key=value`` ⇄ dict.

    Insertion order is preserved on parse→encode so a value round-trips. The
    ``user.id`` authoritative-key overwrite is a compose/resolve concern (R1),
    not the codec's — this only serialises the map.
    """

    def parse(self, raw: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            key, _, value = part.partition("=")
            out[key.strip()] = value.strip()
        return out

    def encode(self, value: Any, placement: Placement) -> str:
        if not isinstance(value, dict):
            return str(value)
        return ",".join(f"{key}={val}" for key, val in value.items())


_SCALAR: Codec = ScalarCodec()

#: The codec for every ValueKind. Scalars share one identity codec; the CSV and
#: structured kinds get their own. Keyed exhaustively so ``codec_for`` never
#: KeyErrors for a catalogued field.
CODECS: dict[ValueKind, Codec] = {
    ValueKind.STR: _SCALAR,
    ValueKind.URL: _SCALAR,
    ValueKind.PATH: _SCALAR,
    ValueKind.ENUM: _SCALAR,
    ValueKind.MODEL_ALIAS: _SCALAR,
    ValueKind.CSV: CsvCodec(),
    ValueKind.STR_LIST: StrListCodec(),
    ValueKind.JSON_OBJECT: JsonObjectCodec(),
    ValueKind.COMMAND: CommandCodec(),
    ValueKind.ATTR_MAP: AttrMapCodec(),
}


def codec_for(kind: ValueKind) -> Codec:
    """Return the codec for ``kind`` (raises KeyError if a kind is uncatalogued)."""
    return CODECS[kind]
