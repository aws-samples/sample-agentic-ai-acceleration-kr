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

Threads the retired basic-chat path pinned (`__basic_chat__`, no record) are
moved onto the default agent's record on their next turn; the model they were
pinned to is carried as the thread's override when it is still allowed.

Known gap, kept deliberately: a server with neither a default runtime nor a
record for the turn has nothing to bind against and lets the request through
with a warning. Deployed stacks always have AGENT_RUNTIME_ARN.
"""
import logging
from typing import Any, Dict, Optional, Tuple

from botocore.exceptions import ClientError

from core import config as cfg
from core.auth import AuthUser
from services.default_agent import default_record
from services.registry_service import RegistryNotConfigured, is_registry_unavailable
from services.team_access import can_see, team_of, visibility_known

logger = logging.getLogger(__name__)

Target = Dict[str, Optional[str]]


class AgentTargetMismatch(Exception):
    """The request names an execution target the bound agent does not own.

    Mapped to 403 by the route: this is about what the caller may invoke, not a
    conflict with the thread's state (that is ThreadAgentMismatch, 409).
    """


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


def _resolve_record(record_id: str, registry_service: Any) -> Optional[Any]:
    """The record a turn binds to, or None when the registry cannot answer.

    None mirrors `StreamingService._require_approved`: a registry that is off or
    failing on the AWS side degrades to the deployed-resource fallback and the
    turn keeps what the client sent. A verdict about the record itself (bad id,
    deleted) is not an outage and fails closed.
    """
    try:
        if record_id.startswith("deployed:"):
            # Lazy: routes import services, not the other way round.
            from routes.registry import _deployed_detail

            return _deployed_detail(record_id)
        # Same revision the approval gate accepted: the approved one, which
        # is also whose ARNs curation reviewed.
        return registry_service.chattable_record(record_id)
    except RegistryNotConfigured:
        return None
    except Exception as exc:  # noqa: BLE001 — classified below
        if is_registry_unavailable(exc) and not is_record_verdict(exc):
            logger.warning("Registry unavailable; cannot derive target for %s: %s", record_id, exc)
            return None
        raise AgentTargetMismatch(f"Agent record {record_id} could not be resolved: {exc}") from exc


def _target_of(record: Any, default_client: Any) -> Optional[Target]:
    """The ARNs a resolved record points at."""
    runtime_arn = getattr(record, "agent_runtime_arn", None)
    harness_arn = getattr(record, "harness_arn", None)
    if not runtime_arn and not harness_arn:
        # A record that names no target is answered by the default runtime, as
        # it always was (`_client_for` falls back to it) — bind to that so a
        # client cannot slip a foreign ARN in under such a record.
        return _default_target(default_client)
    return _target(runtime_arn, harness_arn, getattr(record, "qualifier", None))


def _record_target(record_id: str, registry_service: Any, default_client: Any) -> Optional[Target]:
    """The ARNs the record resolves to, or None when the registry cannot answer."""
    record = _resolve_record(record_id, registry_service)
    return None if record is None else _target_of(record, default_client)


def _teams_enabled(team_service: Any) -> bool:
    return bool(getattr(team_service, "enabled", False))


def _is_restricted(caller: Optional[AuthUser], teams_enabled: bool) -> bool:
    """Only a non-admin caller in a deployment with teams can be refused a record."""
    return teams_enabled and caller is not None and not caller.is_admin


def _ensure_visible(
    record_id: str,
    team: Optional[str],
    caller: Optional[AuthUser],
    *,
    teams_enabled: bool,
    known: bool = True,
) -> None:
    if not can_see(team, caller, teams_enabled=teams_enabled, known=known):
        raise AgentTargetMismatch(f"Agent record {record_id} is not available to this user")


def _deployed_team(record_id: str) -> Tuple[Optional[str], bool]:
    """(team, known) of a `deployed:<arn>` id, from the harness `Team` tag the
    fallback listing reads. Never a GetRegistryRecord: the id is not a record."""
    from routes.registry import _deployed_detail  # lazy: routes import services

    try:
        detail = _deployed_detail(record_id)
    except Exception as exc:  # noqa: BLE001 — gone or unreadable: fail closed
        logger.warning("Could not resolve deployed record %s: %s", record_id, exc)
        return None, False
    return team_of(detail), visibility_known(detail)


def _pinned_team(
    record_id: str, registry_service: Any, caller: Optional[AuthUser], teams_enabled: bool
) -> Tuple[Optional[str], bool]:
    """(team, known) of a record already pinned to the thread — looked up only
    for a caller who could be refused (a non-admin in a deployment with teams),
    so every other turn costs no extra call."""
    if not _is_restricted(caller, teams_enabled):
        return None, True
    if record_id.startswith("deployed:"):
        return _deployed_team(record_id)
    try:
        return registry_service.team_of_record(record_id), True
    except RegistryNotConfigured:
        return None, False
    except Exception as exc:  # noqa: BLE001
        if is_registry_unavailable(exc) and not is_record_verdict(exc):
            # Cannot tell whose record this is: fail closed for this caller.
            return None, False
        raise AgentTargetMismatch(f"Agent record {record_id} could not be resolved: {exc}") from exc


def _check_model_override(
    config: Dict[str, Any], caller: Optional[AuthUser], team_service: Any, record_team: Optional[str]
) -> None:
    """A per-turn `model_id` override (InvokeHarness `model`, the runtime
    payload's model) must be on the operator's global list, then on the team's
    when one applies. The global list is a ceiling for everyone, admins
    included — it is what the deployment was configured to run. The record's
    own team decides when it has one (that is the role the turn runs as);
    otherwise the caller's teams do."""
    model_id = config.get("model_id")
    if not isinstance(model_id, str) or not model_id.strip():
        return
    model_id = model_id.strip()
    if model_id not in cfg.ALLOWED_MODELS:
        raise AgentTargetMismatch(f"Model '{model_id}' is not allowed on this server")
    if team_service is None or not _teams_enabled(team_service):
        return
    if caller is None or caller.is_admin:
        return
    names = [record_team] if record_team else list(caller.teams)
    if not names:
        return
    limit = team_service.allowed_models_for(names)
    if limit is not None and model_id not in limit:
        raise AgentTargetMismatch(f"Model '{model_id}' is not allowed for your team")


def _bind_record(
    config: Dict[str, Any],
    record: Any,
    record_id: str,
    *,
    caller: Optional[AuthUser],
    team_service: Any,
    teams_enabled: bool,
    default_client: Any,
) -> Tuple[Dict[str, Any], Optional[Target]]:
    """Bind a turn to a record already resolved: visibility, model, target."""
    team = team_of(record)
    _ensure_visible(record_id, team, caller, teams_enabled=teams_enabled, known=visibility_known(record))
    _check_model_override(config, caller, team_service, team)
    config["record_team"] = team
    expected = _target_of(record, default_client)
    if expected is None:
        return config, None
    _apply(config, expected)
    return config, expected


def _override_allowed(
    model_id: str, caller: Optional[AuthUser], team_service: Any, record_team: Optional[str]
) -> bool:
    """Whether `model_id` would pass _check_model_override for this caller."""
    try:
        _check_model_override({"model_id": model_id}, caller, team_service, record_team)
    except AgentTargetMismatch:
        return False
    return True


def _translate_legacy_basic_chat(
    config: Dict[str, Any],
    existing_thread: Any,
    registry_service: Any,
    caller: Optional[AuthUser],
    team_service: Any,
) -> Any:
    """Point a basic-chat turn at the default agent's record.

    Two shapes arrive: a thread the retired path pinned (`__basic_chat__`), and a
    request from a tab loaded before the retirement (`basic_chat: true`, no
    record id). Both become an ordinary turn on the default record.

    The model: an override the request names *and that differs from the
    thread's pin* is the user's choice and is checked like any override (403
    outside the lists). Anything else — the pin itself, the retired request
    field, or the web resending the pin it seeded into its override — is the
    pin coming back, and a pin is carried only if the global and team lists
    still allow it. Otherwise it is dropped and the thread answers with the
    agent's default model: refusing would leave the thread stuck on
    `__basic_chat__` with a 403 on every turn. `adopted_model_id` tells
    ThreadService what to write into the thread's override when it re-pins.
    """
    record = default_record(registry_service)
    if record is None:
        raise AgentTargetMismatch("The default agent is not deployed; this basic-chat thread cannot be continued")
    requested = config.get("registry_record_id")
    if requested and requested not in (cfg.LEGACY_BASIC_CHAT_RECORD_ID, record.record_id):
        raise AgentTargetMismatch("A basic-chat thread can only continue with the default agent")
    sent = (config.get("model_id") or "").strip()
    pinned = getattr(existing_thread, "basic_chat_model_id", None) or ""
    if sent and sent != pinned:
        # The user's own choice this turn: _check_model_override judges it.
        carried: Optional[str] = sent
    else:
        legacy = sent or (config.get("basic_chat_model_id") or "").strip() or pinned
        carried = (
            legacy
            if legacy and _override_allowed(legacy, caller, team_service, team_of(record))
            else None
        )
        if legacy and not carried:
            logger.info(
                "Legacy basic-chat model %s is no longer allowed; continuing with the agent's default", legacy
            )
    config.pop("basic_chat", None)
    config.pop("basic_chat_model_id", None)
    config["registry_record_id"] = record.record_id
    config["registry_agent_name"] = getattr(record, "name", "") or ""
    config["adopted_model_id"] = carried
    if carried:
        config["model_id"] = carried
    else:
        config.pop("model_id", None)
    return record


def bind_execution(
    config: Optional[Dict[str, Any]],
    *,
    existing_thread: Any = None,
    registry_service: Any,
    default_client: Any,
    caller: Optional[AuthUser] = None,
    team_service: Any = None,
) -> Tuple[Dict[str, Any], Optional[Target]]:
    """Decide the execution target of a turn.

    Returns the request config with the target filled in, plus the target to
    remember on the thread (None when nothing new was learned: the registry was
    unavailable, or the stored target was reused).

    - A thread already pinned to an agent supplies the record when the request
      omits it, so a continuing turn cannot drift to another target by leaving
      fields blank.
    - A registry record supplies its own ARNs; a request that sent different
      ones is refused.
    - No record at all: only the server's default runtime may be invoked.
    - A `model_id` override is checked against the global allow-list and the
      team's, whichever path the turn takes.
    - A basic-chat thread or request (retired path) is translated onto the
      default agent's record first, then bound like any record.
    """
    config = dict(config or {})
    pinned_record = getattr(existing_thread, "agent_record_id", "") or ""
    teams_enabled = _teams_enabled(team_service)

    if pinned_record == cfg.LEGACY_BASIC_CHAT_RECORD_ID or config.get("basic_chat"):
        record = _translate_legacy_basic_chat(config, existing_thread, registry_service, caller, team_service)
        return _bind_record(
            config, record, record.record_id,
            caller=caller, team_service=team_service, teams_enabled=teams_enabled, default_client=default_client,
        )
    # The retired field on a request that is not basic chat: ignore, never 400 —
    # an old tab may send it next to a real record id.
    config.pop("basic_chat_model_id", None)

    record_id = config.get("registry_record_id") or pinned_record

    if not record_id:
        _check_model_override(config, caller, team_service, None)
        config["record_team"] = None
        expected = _default_target(default_client)
        if expected is None:
            logger.warning("No agent record and no default runtime; target left as sent")
            return config, None
        _apply(config, expected)
        return config, None

    config["registry_record_id"] = record_id
    stored = getattr(existing_thread, "agent_target", None) if pinned_record == record_id else None
    if stored and _sent_matches(config, stored):
        team, known = _pinned_team(record_id, registry_service, caller, teams_enabled)
        _ensure_visible(record_id, team, caller, teams_enabled=teams_enabled, known=known)
        _check_model_override(config, caller, team_service, team)
        config["record_team"] = team
        _apply(config, stored)
        return config, None

    record = _resolve_record(record_id, registry_service)
    if record is None:
        # Registry unreachable: the client-sent target cannot be checked against
        # any team, so a caller who could be refused is refused.
        _ensure_visible(record_id, None, caller, teams_enabled=teams_enabled, known=False)
        _check_model_override(config, caller, team_service, None)
        config["record_team"] = None
        return config, None
    return _bind_record(
        config, record, record_id,
        caller=caller, team_service=team_service, teams_enabled=teams_enabled, default_client=default_client,
    )
