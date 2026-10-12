"""Client capability flags — lets the UI hide features the server cannot serve."""
from fastapi import APIRouter

from core.config import ALLOWED_MODELS, HARNESS_EXECUTION_ROLE_ARN
from services.registry_service import registry_enabled

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("")
def get_config() -> dict:
    """What the frontend needs to branch on. Unauthenticated-safe: no secrets,
    no ARNs (they carry the account id).

    `registryEnabled` reflects configured intent (AP_USE_REGISTRY + AGENT_REGISTRY_ID),
    so the UI hides registry management when off. Read paths still degrade to
    deployed-resource fallbacks at runtime even when this is true.
    """
    return {
        "registryEnabled": registry_enabled(),
        "harnessEnabled": bool(HARNESS_EXECUTION_ROLE_ARN),
        # Models a per-thread override may pick, for every agent, in the
        # operator's order. The server refuses any other, so the override popover
        # offers exactly this list. Empty = no model override is offered.
        "allowedModels": list(ALLOWED_MODELS),
    }
