"""Admin API for the Slack DM integration (integrations/slack/service.py).

Every route is admin-only: linking an app hands a workspace a way into an
agent, and the tokens are credentials.

Token bodies are parsed by hand, not through a Pydantic model: FastAPI's 422
response echoes the request body back as `input`, which would put the tokens in
the response (and in any log of it). A malformed body is a bare 400.
"""
import json
import logging
from typing import Any, Dict, Tuple

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Request

from core.auth import AuthUser, require_admin
from integrations.slack.repository import IntegrationExists
from integrations.slack.service import (
    DeletionIncomplete,
    IntegrationNotFound,
    SlackNotConfigured,
    ValidationFailed,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["slack"])


def _service():
    # Lazy: core.dependencies builds every service; tests swap this function.
    from core.dependencies import slack_service

    return slack_service


async def _tokens(request: Request) -> Tuple[str, str]:
    try:
        body: Any = json.loads(await request.body() or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="요청 본문이 올바르지 않습니다.")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="요청 본문이 올바르지 않습니다.")
    bot, app = body.get("bot_token"), body.get("app_token")
    if not isinstance(bot, str) or not isinstance(app, str) or not bot.strip() or not app.strip():
        raise HTTPException(status_code=400, detail="bot_token 과 app_token 이 필요합니다.")
    return bot.strip(), app.strip()


def _fail(exc: Exception) -> None:
    if isinstance(exc, HTTPException):
        raise exc
    if isinstance(exc, SlackNotConfigured):
        raise HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, IntegrationNotFound):
        raise HTTPException(status_code=404, detail="이 Harness에는 Slack 연동이 없습니다.")
    if isinstance(exc, IntegrationExists):
        detail = (
            "이 Slack 앱은 이미 다른 Harness에 연결되어 있습니다."
            if exc.reason == "app"
            else "이 Harness에는 이미 Slack 연동이 있습니다."
        )
        raise HTTPException(status_code=409, detail=detail)
    if isinstance(exc, ValidationFailed):
        raise HTTPException(status_code=400, detail={"message": "검사를 통과하지 못했습니다.", **exc.result})
    if isinstance(exc, DeletionIncomplete):
        raise HTTPException(
            status_code=502,
            detail={"message": "일부 정리 단계가 실패했습니다. 다시 시도해 주세요.", "steps": exc.steps},
        )
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        raise HTTPException(status_code=502, detail=f"AWS error ({code})")
    # Neither str(exc) nor the traceback (whose last line is str(exc)): nothing
    # here should carry a token, but a 500 is not the place to find out. The
    # location is enough to start from.
    tb = exc.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    where = f"{tb.tb_frame.f_code.co_filename}:{tb.tb_lineno}" if tb is not None else "?"
    logger.error("Slack route error: %s at %s", type(exc).__name__, where)
    raise HTTPException(status_code=500, detail="Slack 연동 요청을 처리하지 못했습니다.")


@router.get("/slack/integrations")
async def list_integrations(_: AuthUser = Depends(require_admin)) -> Dict[str, Any]:
    try:
        return {"integrations": await _service().list()}
    except Exception as exc:
        _fail(exc)


@router.get("/harnesses/{harness_id}/slack")
async def get_integration(harness_id: str, _: AuthUser = Depends(require_admin)) -> Dict[str, Any]:
    try:
        return {"integration": await _service().get(harness_id)}
    except Exception as exc:
        _fail(exc)


@router.get("/harnesses/{harness_id}/slack/manifest")
async def get_manifest(harness_id: str, _: AuthUser = Depends(require_admin)) -> Dict[str, Any]:
    try:
        return await _service().manifest(harness_id)
    except Exception as exc:
        _fail(exc)


@router.post("/harnesses/{harness_id}/slack/test")
async def test_integration(
    harness_id: str, request: Request, _: AuthUser = Depends(require_admin)
) -> Dict[str, Any]:
    try:
        bot, app = await _tokens(request)
        return await _service().test(harness_id, bot, app)
    except Exception as exc:
        _fail(exc)


@router.post("/harnesses/{harness_id}/slack")
async def create_integration(
    harness_id: str, request: Request, user: AuthUser = Depends(require_admin)
) -> Dict[str, Any]:
    try:
        bot, app = await _tokens(request)
        return {"integration": await _service().create(harness_id, bot, app, user)}
    except Exception as exc:
        _fail(exc)


@router.delete("/harnesses/{harness_id}/slack")
async def delete_integration(harness_id: str, user: AuthUser = Depends(require_admin)) -> Dict[str, Any]:
    try:
        return await _service().delete(harness_id, user)
    except Exception as exc:
        _fail(exc)
