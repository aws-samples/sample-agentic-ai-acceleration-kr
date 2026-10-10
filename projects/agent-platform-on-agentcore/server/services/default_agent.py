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


def mark_default(records: Iterable[Any], default_runtime_arn: Optional[str] = None) -> List[Any]:
    """Set `is_default` on every record in place and return them as a list."""
    out = list(records)
    arn = _runtime_arn(default_runtime_arn)
    for record in out:
        record.is_default = is_default_record(record, arn)
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
        for record in registry_service.agent_records():
            if is_default_record(record, arn):
                return record
    except Exception as exc:  # noqa: BLE001 — off, unreachable, or outage: fall back
        logger.warning("Registry could not list agent records; using deployed fallback: %s", exc)
    try:
        for record in _deployed_records():
            if is_default_record(record, arn):
                return record
    except Exception as exc:  # noqa: BLE001
        logger.warning("Deployed listing failed while finding the default agent: %s", exc)
    return None
