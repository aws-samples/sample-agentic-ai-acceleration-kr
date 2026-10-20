"""Which agent record is the deployment's default agent.

The default runtime (AGENT_RUNTIME_ARN) used to be reachable without a record,
through the retired basic-chat path. Now it is addressed like every other agent:
by the registry record that points at it. This module decides which record that
is, so the picker can pin it and a thread the old path left behind can be moved
onto it (services/agent_access.py).
"""
import logging
from typing import Any, Iterable, List, Optional

from agents.agent_config import AgentConfig

logger = logging.getLogger(__name__)


def _runtime_arn(default_runtime_arn: Optional[str]) -> str:
    if default_runtime_arn is not None:
        return default_runtime_arn
    return AgentConfig.get_agent_runtime_arn() or ""


def _chattable(record: Any) -> bool:
    """Same rule as the web picker (lib/registry.ts isChattable): an APPROVED
    record, or one whose approved revision is still served while a later
    revision is DRAFT. A DEPRECATED record is terminal and can never bind.
    A harness delete / re-sync leaves such records on the same ARN, so without
    this the default could land on one chat cannot use."""
    status = (getattr(record, "status", None) or "").upper()
    if status == "DEPRECATED":
        return False
    return status == "APPROVED" or getattr(record, "discoverable", None) is True


def is_default_record(record: Any, default_runtime_arn: Optional[str] = None) -> bool:
    """A chattable standalone-runtime record whose runtime is the server's
    default runtime.

    Harness records are excluded even when their companion runtime ARN matches:
    InvokeHarness answers for them, not the runtime.
    """
    arn = _runtime_arn(default_runtime_arn)
    if not arn or getattr(record, "harness_arn", None):
        return False
    return getattr(record, "agent_runtime_arn", None) == arn and _chattable(record)


def _runtime_name(arn: str) -> str:
    """`.../runtime/bap_default-AbC12` -> `bap_default`: the name deploy.sh
    registers the default runtime's record under."""
    tail = arn.rsplit("/", 1)[-1]
    return tail.rsplit("-", 1)[0] if "-" in tail else tail


def pick_default(records: Iterable[Any], default_runtime_arn: Optional[str] = None) -> Optional[Any]:
    """The one default record among those pointing at the default runtime.

    Any number of records may point at that runtime: the registry lets anyone
    with a runtime ARN register another agent on it, each with its own team and
    overrides. Flagging all of them left the web to take whichever came first
    in the listing; at the 1,000-user test (bst, 2026-10-18) 91 records did, and
    every new chat opened on a seeded `load_agent_075` instead of `bst_default`.
    One is chosen, the same way everywhere: the record named after the runtime
    (what deploy.sh registers), else the first registered, then by id.
    """
    arn = _runtime_arn(default_runtime_arn)
    candidates = [r for r in records if is_default_record(r, arn)]
    if not candidates:
        return None
    name = _runtime_name(arn)
    return min(candidates, key=lambda r: (getattr(r, "name", None) != name,
                                          getattr(r, "created_at", None) or "~",
                                          getattr(r, "record_id", "")))


def mark_default(records: Iterable[Any], default_runtime_arn: Optional[str] = None) -> List[Any]:
    """Set `is_default` on every record in place (on one of them, see
    pick_default) and return them as a list."""
    out = list(records)
    chosen = pick_default(out, default_runtime_arn)
    for record in out:
        record.is_default = record is chosen
    return out


def _deployed_records() -> List[Any]:
    """The registry-off listing. Lazy import: routes import services, not the
    other way round. Patched in tests."""
    from routes.registry import _sync

    return _sync().deployed_agent_records()


def default_record(registry_service: Any, default_runtime_arn: Optional[str] = None) -> Optional[Any]:
    """The default agent's record: from the registry when it has one, else from
    the deployed-resource fallback. None when nothing points at the default
    runtime (or there is none)."""
    arn = _runtime_arn(default_runtime_arn)
    if not arn:
        return None
    try:
        record = pick_default(registry_service.agent_records(), arn)
        if record is not None:
            return record
    except Exception as exc:  # noqa: BLE001 — off, unreachable, or outage: fall back
        logger.warning("Registry could not list agent records; using deployed fallback: %s", exc)
    try:
        return pick_default(_deployed_records(), arn)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Deployed listing failed while finding the default agent: %s", exc)
    return None
