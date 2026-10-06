"""Server-side binding of a chat turn to the execution target it may reach.

The stream request carries `agent_runtime_arn` / `harness_arn` / `qualifier`
from the browser. Those used to be invoked as sent, with only the record's
APPROVED status checked — so any signed-in user could point a thread at any
runtime or harness the server's role can invoke, approved record or not. The
target now comes from the registry record itself (or from the server's default
runtime when there is no record), and a client value that disagrees is refused.

The record is not re-read on every turn: GetRegistryRecord is slow on a cold
client and the thread already pins its agent. The target derived at pin time is
stored on the thread (Thread.agent_target) and reused while the client keeps
sending the same ARNs; only a change — a recomposed harness has a new ARN —
goes back to the registry to verify the new value.

Basic chat is the one target with no record: the default runtime answering with
a model from the operator's allow-list. It is bound here too, so the ARN and the
model a basic-chat turn runs with are the server's choice, never the client's.

Known gap, kept deliberately: a server with neither a default runtime nor a
record for the turn has nothing to bind against and lets the request through
with a warning. Deployed stacks always have AGENT_RUNTIME_ARN.
"""
import logging
from typing import Any, Dict, Optional, Tuple

from botocore.exceptions import ClientError

from core import config as cfg
from services.registry_service import RegistryNotConfigured, is_registry_unavailable

logger = logging.getLogger(__name__)

Target = Dict[str, Optional[str]]


class AgentTargetMismatch(Exception):
    """The request names an execution target the bound agent does not own.

    Mapped to 403 by the route: this is about what the caller may invoke, not a
    conflict with the thread's state (that is ThreadAgentMismatch, 409).
    """


class BasicChatUnavailable(Exception):
    """Basic chat was requested but is not configured or named a model outside the allow-list."""


# The AWS error codes that are a verdict about *this record id* rather than about
# the registry being reachable. `is_registry_unavailable` lumps every ClientError
# into "fall back to deployed resources", which is right for an SCP or an outage
# but would let a made-up record id skip the checks that depend on the record.
_RECORD_VERDICT_CODES = {"ValidationException", "ResourceNotFoundException"}


def is_record_verdict(exc: Exception) -> bool:
    """True when the failure says the record id itself is bad, not the registry."""
    if not isinstance(exc, ClientError):
        return False
    return exc.response.get("Error", {}).get("Code") in _RECORD_VERDICT_CODES


def _target(runtime_arn: Optional[str], harness_arn: Optional[str], qualifier: Optional[str]) -> Target:
    return {
        "agent_runtime_arn": runtime_arn or None,
        "harness_arn": harness_arn or None,
        "qualifier": qualifier or None,
    }


def _default_target(default_client: Any) -> Optional[Target]:
    runtime_arn = getattr(default_client, "agent_runtime_arn", None)
    if not runtime_arn:
        return None
    return _target(runtime_arn, None, getattr(default_client, "qualifier", None))


def _apply(config: Dict[str, Any], expected: Target) -> None:
    for key, value in expected.items():
        sent = config.get(key)
        if sent and sent != value:
            raise AgentTargetMismatch(
                f"{key} does not belong to the agent this turn is addressed to"
            )
        config[key] = value


def _sent_matches(config: Dict[str, Any], stored: Target) -> bool:
    """Every ARN the client sent agrees with what the thread already bound."""
    return all(not config.get(key) or config.get(key) == stored.get(key) for key in stored)


def _bind_basic_chat(config: Dict[str, Any], default_client: Any) -> Tuple[Dict[str, Any], Target]:
    if config.get("harness_arn"):
        raise AgentTargetMismatch("Basic chat cannot name a harness")
    model_id = config.get("basic_chat_model_id")
    if not cfg.BASIC_CHAT_ALLOWED_MODELS:
        raise BasicChatUnavailable("Basic chat is not configured on this server")
    if not isinstance(model_id, str) or model_id not in cfg.BASIC_CHAT_ALLOWED_MODELS:
        raise BasicChatUnavailable(f"Model '{model_id or 'none'}' is not allowed for basic chat")
    runtime_arn = cfg.BASIC_CHAT_RUNTIME_ARN or getattr(default_client, "agent_runtime_arn", None)
    if not runtime_arn:
        raise BasicChatUnavailable("Basic chat has no runtime to run on")
    expected = _target(runtime_arn, None, getattr(default_client, "qualifier", None))
    _apply(config, expected)
    config.update(
        {
            "basic_chat": True,
            "registry_record_id": cfg.BASIC_CHAT_RECORD_ID,
            "registry_agent_name": cfg.BASIC_CHAT_AGENT_NAME,
            # What the runtime reads (agent-runtime/main.py); ThreadService pins
            # the same value onto the thread as basic_chat_model_id.
            "model_id": model_id,
        }
    )
    return config, expected


def _record_target(record_id: str, registry_service: Any, default_client: Any) -> Optional[Target]:
    """The ARNs the record resolves to, or None when the registry cannot answer.

    None mirrors `StreamingService._require_approved`: a registry that is off or
    failing on the AWS side degrades to the deployed-resource fallback and the
    turn keeps what the client sent. A verdict about the record itself (bad id,
    deleted) is not an outage and fails closed.
    """
    try:
        if record_id.startswith("deployed:"):
            # Lazy: routes import services, not the other way round.
            from routes.registry import _deployed_detail

            record = _deployed_detail(record_id)
        else:
            record = registry_service.get_record(record_id)
    except RegistryNotConfigured:
        return None
    except Exception as exc:  # noqa: BLE001 — classified below
        if is_registry_unavailable(exc) and not is_record_verdict(exc):
            logger.warning("Registry unavailable; cannot derive target for %s: %s", record_id, exc)
            return None
        raise AgentTargetMismatch(f"Agent record {record_id} could not be resolved: {exc}") from exc
    runtime_arn = getattr(record, "agent_runtime_arn", None)
    harness_arn = getattr(record, "harness_arn", None)
    if not runtime_arn and not harness_arn:
        # A record that names no target is answered by the default runtime, as
        # it always was (`_client_for` falls back to it) — bind to that so a
        # client cannot slip a foreign ARN in under such a record.
        return _default_target(default_client)
    return _target(runtime_arn, harness_arn, getattr(record, "qualifier", None))


def bind_execution(
    config: Optional[Dict[str, Any]],
    *,
    existing_thread: Any = None,
    registry_service: Any,
    default_client: Any,
) -> Tuple[Dict[str, Any], Optional[Target]]:
    """Decide the execution target of a turn.

    Returns the request config with the target filled in, plus the target to
    remember on the thread (None when nothing new was learned: the registry was
    unavailable, or the stored target was reused).

    - A thread already pinned to an agent supplies the record (and, for basic
      chat, the model) when the request omits them, so a continuing turn cannot
      drift to another target by leaving fields blank.
    - A registry record supplies its own ARNs; a request that sent different
      ones is refused.
    - No record at all: only the server's default runtime may be invoked.
    """
    config = dict(config or {})
    pinned_record = getattr(existing_thread, "agent_record_id", "") or ""
    record_id = config.get("registry_record_id") or pinned_record

    if config.get("basic_chat") or record_id == cfg.BASIC_CHAT_RECORD_ID:
        if record_id and record_id != cfg.BASIC_CHAT_RECORD_ID:
            raise AgentTargetMismatch("Basic chat cannot be combined with a registry agent")
        if not config.get("basic_chat_model_id") and pinned_record == cfg.BASIC_CHAT_RECORD_ID:
            config["basic_chat_model_id"] = getattr(existing_thread, "basic_chat_model_id", None)
        return _bind_basic_chat(config, default_client)

    if config.get("basic_chat_model_id"):
        raise AgentTargetMismatch("basic_chat_model_id is only valid for basic chat")

    if not record_id:
        expected = _default_target(default_client)
        if expected is None:
            logger.warning("No agent record and no default runtime; target left as sent")
            return config, None
        _apply(config, expected)
        return config, None

    config["registry_record_id"] = record_id
    stored = getattr(existing_thread, "agent_target", None) if pinned_record == record_id else None
    if stored and _sent_matches(config, stored):
        _apply(config, stored)
        return config, None

    expected = _record_target(record_id, registry_service, default_client)
    if expected is None:
        return config, None
    _apply(config, expected)
    return config, expected
