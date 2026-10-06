"""Client capability flags — lets the UI hide features the server cannot serve."""
from fastapi import APIRouter

from agents.agent_config import AgentConfig
from core.config import BASIC_CHAT_ALLOWED_MODELS, BASIC_CHAT_RUNTIME_ARN, HARNESS_EXECUTION_ROLE_ARN
from services.registry_service import registry_enabled

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("")
def get_config() -> dict:
    """What the frontend needs to branch on. Unauthenticated-safe: no secrets.

    `registryEnabled` reflects configured intent (AP_USE_REGISTRY + AGENT_REGISTRY_ID),
    so the UI hides registry management when off. Read paths still degrade to
    deployed-resource fallbacks at runtime even when this is true.
    """
    return {
        "registryEnabled": registry_enabled(),
        "harnessEnabled": bool(HARNESS_EXECUTION_ROLE_ARN),
        # Chat with the default runtime and a curated model, no registry agent.
        # The list is the allow-list the server enforces, so the picker can only
        # offer what a turn will be accepted with. A stack deployed without any
        # Runtime agent still ships the default model list, so the runtime has
        # to be present too, or every basic-chat turn fails at bind time.
        "basicChat": {
            "configured": bool(BASIC_CHAT_ALLOWED_MODELS) and bool(
                BASIC_CHAT_RUNTIME_ARN or AgentConfig.get_agent_runtime_arn()
            ),
            "models": list(BASIC_CHAT_ALLOWED_MODELS),
        },
    }
