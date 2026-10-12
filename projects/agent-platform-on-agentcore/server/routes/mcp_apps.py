"""MCP Apps(SEP-1865) 릴레이 라우트.

`current_user`로 게이트한다 — 앱 안의 버튼 클릭은 채팅하는 사람의 것이고,
`routes/ui_actions.py`가 같은 이유로 그랬다.

이것이 /api/mcp/* 와 분리된 이유는 인증 수준이 아니라 신뢰 기준점이다. 그쪽은 요청
본문의 server_config로 임의 명령을 실행할 수 있어 관리자 전용이어야 한다. 이 라우트는
레지스트리 레코드에서만 엔드포인트를 얻으므로 일반 사용자에게 열 수 있다.
"""
import logging
import traceback

from fastapi import APIRouter, Depends, HTTPException

from core.auth import AuthUser, current_user
from mcp_core.app_session import UiResourcePayload  # noqa: F401  (테스트가 참조)
from mcp_core.errors import root_causes as _root_causes
from mcp_core.ui_meta import UI_URI_SCHEME
from models.mcp_apps import (
    McpAppResourceReadRequest,
    McpAppResourceReadResponse,
    McpAppToolCallRequest,
    McpAppToolCallResponse,
    McpAppToolDescribeRequest,
    McpAppToolDescribeResponse,
)
from services.mcp_apps_service import McpAppsRelay, shared_relay

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp-apps", tags=["mcp-apps"])

def _relay() -> McpAppsRelay:
    return shared_relay()


# 이 라우트들은 일부러 `def`(동기)다. `async def`가 아니다.
#
# 릴레이 메서드는 `open_session`으로 MCP 세션을 여는데, 그 안에서
# `anyio.from_thread.start_blocking_portal().call(...)`이 호출 스레드를 **블로킹**한다.
# `async def` 라우트는 이벤트 루프 스레드에서 실행되므로, 그 블로킹이 곧 이벤트 루프
# 정지다 — 그 시간 동안 서버 전체가 응답을 멈춘다(헬스 체크 포함). 실측으로 확인했다:
# 갓 배포된 런타임의 콜드 스타트로 tools/call 하나가 AgentCore에서 60초 걸리자, 그
# 호출이 루프를 잡았고 재시도가 쌓이면서 단일 워커 서버가 완전히 얼어붙었다.
#
# `def`로 두면 FastAPI가 이 핸들러를 스레드풀에서 돌린다. 느린 AgentCore invoke는
# 이제 워커 스레드 하나만 잡고 이벤트 루프는 자유롭다. AgentCore invoke는 자체 상한
# (~60초)이 있어 스레드는 결국 풀린다. 다시 `async def`로 바꾸면 정지가 돌아온다.
@router.post("/resources/read", response_model=McpAppResourceReadResponse)
def read_ui_resource(
    request: McpAppResourceReadRequest, _: AuthUser = Depends(current_user)
):
    """ui:// 리소스를 릴레이한다."""
    if not request.uri.startswith(UI_URI_SCHEME):
        # 임의 URL을 호스트가 가져오게 하면 안 된다.
        raise HTTPException(
            status_code=400, detail=f"{UI_URI_SCHEME} 스킴만 허용됩니다."
        )

    try:
        payload = _relay().read_ui_resource(request.record_id, request.uri)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("resources/read 릴레이 실패: %s", exc)
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=502, detail=_root_causes(exc))

    return McpAppResourceReadResponse(
        success=True,
        uri=payload.uri,
        mime_type=payload.mime_type,
        text=payload.text,
        ui_meta=payload.ui_meta,
    )


@router.post("/tools/call", response_model=McpAppToolCallResponse)
def call_app_tool(
    request: McpAppToolCallRequest, _: AuthUser = Depends(current_user)
):
    """앱 발신 tools/call. visibility에 "app"이 없으면 거부한다 (규격 MUST).

    검사와 호출이 한 MCP 세션 안에서 일어난다 — 따로 열면 세션이 두 번 생기고, 그
    사이에 서버가 visibility를 바꾸면 검사한 것과 호출한 것이 달라진다.
    """
    relay = _relay()

    try:
        result = relay.call_app_tool_checked(
            request.record_id, request.tool_name, request.arguments
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("tools/call 릴레이 실패: %s", exc)
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=502, detail=_root_causes(exc))

    return McpAppToolCallResponse(success=True, result=result)


@router.post("/tools/describe", response_model=McpAppToolDescribeResponse)
def describe_app_tool(
    request: McpAppToolDescribeRequest, _: AuthUser = Depends(current_user)
):
    """앱을 띄운 툴의 정의를 돌려준다 — 호스트가 `hostContext.toolInfo` 로 View 에 넘긴다.

    visibility 를 보지 않는다: 이 툴은 모델이 이미 호출해 앱을 띄운 진입점이고, 정의를
    읽는 것은 호출이 아니다. 호출은 여전히 /tools/call 의 검사를 거친다.
    """
    try:
        tool = _relay().describe_tool(request.record_id, request.tool_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("tools/describe 릴레이 실패: %s", exc)
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=502, detail=_root_causes(exc))

    return McpAppToolDescribeResponse(success=True, tool=tool)
