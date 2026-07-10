# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""admin-chat-agent 의 admin-api endpoints.

docs/admin-chat-agent-spec.md §4.1 / §4.2 / §4.3 참조.

엔드포인트:
- POST /admin/chat/sessions          — 새 세션 생성
- GET  /admin/chat/sessions          — 사용자의 세션 목록
- GET  /admin/chat/sessions/{id}/history — 메시지 history
- POST /admin/chat/sessions/{id}/messages — 메시지 전송 (SSE stream)
- POST /admin/chat/render-chart      — deterministic chart spec
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from typing import Any, AsyncIterator, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, require_admin
from app.core.db import AsyncSessionLocal, get_db_session

router = APIRouter(prefix="/admin/chat", tags=["AdminChat"])


logger = logging.getLogger(__name__)

# ─── Config ───
AGENTCORE_RUNTIME_ARN = os.environ.get("AGENTCORE_RUNTIME_ARN", "")
AGENTCORE_REGION = os.environ.get("AGENTCORE_REGION", "ap-northeast-2")
SESSION_TTL_HOURS = 8


# ─── render_chart ───
class ChartRequest(BaseModel):
    """admin-chat-agent (또는 AgentCore Gateway) 에서 호출.

    Code Specialist 가 PNG 를 만들 땐 이 endpoint 가 아니라 S3 presigned URL
    을 반환해서 admin-ui 가 직접 image embed. 이 endpoint 는 SQL 결과의
    recharts spec 변환용.
    """

    kind: Literal["bar", "line", "pie", "area", "table", "kpi"] = Field(
        description="차트 종류"
    )
    data: list[dict[str, Any]] = Field(
        description="renderer 가 그대로 받아 쓸 데이터 (행 array)"
    )
    x: str = Field(description="X 축 컬럼 이름")
    y: str | list[str] = Field(description="Y 축 컬럼 이름 (단일 또는 multi-series)")
    color: str | None = Field(default=None, description="series 분리 컬럼")
    title: str | None = Field(default=None)


class ChartSpec(BaseModel):
    """admin-ui 가 그대로 사용할 chart spec."""

    kind: str
    data: list[dict[str, Any]]
    encoding: dict[str, Any]
    title: str | None = None


@router.post("/render-chart", response_model=ChartSpec)
async def render_chart(
    req: ChartRequest,
    _admin: CurrentUser = Depends(require_admin),
) -> ChartSpec:
    """admin-chat-agent 의 deterministic tool.

    LLM (Viz Specialist) 가 결정한 kind/encoding 과 SQL 결과 (data) 를 받아
    admin-ui 의 ChartRenderer 가 그대로 입력으로 받는 spec 으로 변환.

    spec 자체는 단순한 pass-through 에 가깝지만, 권한 체크 (admin only) 와
    audit log 작성을 보장하기 위해 admin-api 안에서 처리.
    """
    encoding: dict[str, Any] = {"x": req.x, "y": req.y}
    if req.color:
        encoding["color"] = req.color

    return ChartSpec(
        kind=req.kind,
        data=req.data,
        encoding=encoding,
        title=req.title,
    )


# ─── Sessions ───
class SessionCreate(BaseModel):
    pass


class SessionResponse(BaseModel):
    session_id: str
    created_at: str
    expires_at: str


class SessionListItem(BaseModel):
    id: str
    title: str | None
    status: str
    updated_at: str
    message_count: int


@router.post("/sessions", response_model=SessionResponse)
async def create_session(
    _: SessionCreate,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
) -> SessionResponse:
    """새 chat 세션. AgentCore session_id 는 첫 메시지 호출 시 발급 (lazy)."""
    from datetime import datetime, timedelta, timezone

    new_id = str(uuid.uuid4())
    expires = datetime.now(timezone.utc) + timedelta(hours=SESSION_TTL_HOURS)

    await session.execute(
        text(
            """
            INSERT INTO chat_agent.sessions (id, user_id, expires_at)
            VALUES (CAST(:id AS uuid), CAST(:uid AS uuid), :exp)
            """
        ).bindparams(
            id=new_id,
            uid=str(admin.user_id),
            exp=expires,
        )
    )
    await session.commit()

    # 프리워밍(§54): AgentCore microVM 콜드스타트를 사용자의 첫 질문 전에 흡수.
    # __ping__ 은 agent 가 LLM/도구 없이 즉시 done 반환(비용 0). fire-and-forget —
    # 실패해도 세션 생성에 영향 없음(첫 질문이 조금 느릴 뿐).
    if AGENTCORE_RUNTIME_ARN:
        asyncio.get_running_loop().create_task(_prewarm_agent(new_id))

    return SessionResponse(
        session_id=new_id,
        created_at=datetime.now(timezone.utc).isoformat(),
        expires_at=expires.isoformat(),
    )


async def _prewarm_agent(session_id: str) -> None:
    """세션 생성 직후 AgentCore 를 __ping__ 으로 깨워 microVM 을 선예열."""
    import boto3

    def _ping() -> None:
        client = boto3.client("bedrock-agentcore", region_name=AGENTCORE_REGION)
        resp = client.invoke_agent_runtime(
            agentRuntimeArn=AGENTCORE_RUNTIME_ARN,
            runtimeSessionId=_agentcore_session_id(session_id),
            payload=json.dumps({"content": "__ping__", "session_id": session_id}).encode(),
        )
        body = resp.get("response")
        if hasattr(body, "read"):
            body.read()  # 스트림 소진(커넥션 정리)

    try:
        await asyncio.to_thread(_ping)
        logger.info("agent prewarmed session_id=%s", session_id)
    except Exception as exc:  # noqa: BLE001 — 프리워밍 실패는 무해(로그만)
        logger.warning("agent prewarm failed session_id=%s err=%s", session_id, exc)


@router.get("/sessions")
async def list_sessions(
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
) -> list[SessionListItem]:
    rows = await session.execute(
        text(
            """
            SELECT id, title, status, updated_at, message_count
            FROM chat_agent.sessions
            WHERE user_id = CAST(:uid AS uuid) AND status != 'archived'
            ORDER BY updated_at DESC
            LIMIT 50
            """
        ).bindparams(uid=str(admin.user_id))
    )
    return [
        SessionListItem(
            id=str(r.id),
            title=r.title,
            status=r.status,
            updated_at=r.updated_at.isoformat() if r.updated_at else "",
            message_count=r.message_count,
        )
        for r in rows
    ]


@router.get("/sessions/{session_id}/history")
async def get_history(
    session_id: str,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    # 권한 검증 — 자신의 세션만
    sess_row = (
        await session.execute(
            text(
                "SELECT user_id FROM chat_agent.sessions WHERE id = CAST(:sid AS uuid)"
            ).bindparams(sid=session_id)
        )
    ).first()
    if not sess_row:
        raise HTTPException(404, "Session not found")
    if str(sess_row.user_id) != str(admin.user_id):
        raise HTTPException(403, "Forbidden")

    rows = await session.execute(
        text(
            """
            SELECT id, role, content, tool_calls, charts, validator,
                   cost_usd, duration_ms, created_at
            FROM chat_agent.messages
            WHERE session_id = CAST(:sid AS uuid)
            ORDER BY created_at
            """
        ).bindparams(sid=session_id)
    )
    return {
        "messages": [
            {
                "id": str(r.id),
                "role": r.role,
                "content": r.content,
                "tool_calls": r.tool_calls,
                "charts": r.charts,
                "validator": r.validator,
                "cost_usd": float(r.cost_usd) if r.cost_usd else None,
                "duration_ms": r.duration_ms,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


# ─── 핸드오프 릴레이 (§56) ───
# 문제: 기존엔 SSE generator 가 AgentCore 소비+영속화를 겸해, 클라이언트가 페이지를
# 떠나면(스트림 close) 소비도 영속화도 함께 죽었다 — 심층 분석이 "브라우저를 떠나면
# 증발". 해법: AgentCore 소비를 **독립 background task**(producer)로 분리하고, SSE
# 응답은 in-memory 릴레이를 tail 만 한다(consumer). 클라가 끊겨도 producer 는 끝까지
# 돌아 DB 에 영속화 → 사용자가 다른 메뉴에 다녀와도 결과가 history 로 남는다.
# 릴레이는 단일 파드 메모리(현 admin-api replica=1). 멀티 replica 확장 시 Redis
# pub/sub 로 교체 지점.
class _StreamRelay:
    """per-message 이벤트 릴레이. producer 가 append, 소비자는 비동기 tail."""

    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.done = False
        self._cond = asyncio.Condition()

    async def publish(self, frame: bytes) -> None:
        async with self._cond:
            self.frames.append(frame)
            self._cond.notify_all()

    async def finish(self) -> None:
        async with self._cond:
            self.done = True
            self._cond.notify_all()

    async def tail(self) -> AsyncIterator[bytes]:
        idx = 0
        while True:
            async with self._cond:
                while idx >= len(self.frames) and not self.done:
                    # keepalive 는 호출측(타임아웃)에서 처리
                    try:
                        await asyncio.wait_for(self._cond.wait(), timeout=10.0)
                    except TimeoutError:
                        break
            if idx < len(self.frames):
                while idx < len(self.frames):
                    yield self.frames[idx]
                    idx += 1
            elif self.done:
                return
            else:
                yield b": keepalive\n\n"


# 세션별 활성 릴레이. 새 메시지가 오면 교체(세션당 동시 1개 분석 가정 — UI 가
# isStreaming 중 전송을 막음). 페이지 복귀 시 진행 중 분석을 재구독할 수 있다.
_active_relays: dict[str, _StreamRelay] = {}


# ─── Messages (SSE stream proxy) ───
class MessageCreate(BaseModel):
    content: str
    # "지금 보는 화면" 컨텍스트(선택). admin-ui 가 페이지 등록 데이터를 동봉 →
    # agent 가 컨텍스트 질의에 활용. {page, period?, data?} 형태. 없으면 None.
    screen_context: dict | None = None
    # 모드(§55): "quick"(퀵챗 드로어 — 즉답) | "deep"(사이드바 Chat — plan-first
    # 심층분석). agent 가 orchestrator 프로필을 선택한다. 기본 quick(하위호환).
    mode: Literal["quick", "deep"] = "quick"


@router.post("/sessions/{session_id}/messages")
async def post_message(
    session_id: str,
    req: MessageCreate,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
) -> StreamingResponse:
    """AgentCore InvokeAgentRuntime 호출 후 SSE 변환.

    - admin 권한은 require_admin 이 검증 → AgentCore 는 admin-api 를 IAM(SigV4)
      으로 신뢰 (런타임이 SigV4 authorizer)
    - 현재 agent 는 단일 JSON 반환 → _agentcore_stream 이 text+done SSE 로 변환
    - admin-api 는 assistant 응답을 chat_agent.messages 테이블에 저장
    """
    if not AGENTCORE_RUNTIME_ARN:
        raise HTTPException(503, "AgentCore runtime not configured")

    # 세션 권한 + 만료 확인
    sess_row = (
        await session.execute(
            text(
                "SELECT user_id, expires_at FROM chat_agent.sessions "
                "WHERE id = CAST(:sid AS uuid)"
            ).bindparams(sid=session_id)
        )
    ).first()
    if not sess_row:
        raise HTTPException(404, "Session not found")
    if str(sess_row.user_id) != str(admin.user_id):
        raise HTTPException(403, "Forbidden")

    # user 메시지 저장
    user_msg_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO chat_agent.messages (id, session_id, role, content) "
            "VALUES (CAST(:id AS uuid), CAST(:sid AS uuid), 'user', :c)"
        ).bindparams(id=user_msg_id, sid=session_id, c=req.content)
    )
    await session.commit()

    # 핸드오프(§56): AgentCore 소비+영속화는 독립 background task 로 — 클라가
    # 페이지를 떠나 SSE 가 끊겨도 분석은 끝까지 돌고 결과는 DB 에 저장된다.
    # SSE 응답은 릴레이 tail(구독)일 뿐. 복귀 시 GET /stream 으로 재구독 가능.
    relay = _StreamRelay()
    _active_relays[session_id] = relay
    asyncio.get_running_loop().create_task(
        _agentcore_producer(
            relay, session_id, req.content, req.screen_context, req.mode
        )
    )

    return StreamingResponse(
        relay.tail(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/sessions/{session_id}/stream")
async def reattach_stream(
    session_id: str,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
) -> StreamingResponse:
    """진행 중 분석 재구독(§56) — 다른 메뉴에 다녀온 뒤 이어서 스트림.

    릴레이가 살아있으면(분석 진행/방금 완료) 처음부터 tail(이미 발행된 프레임
    재생 + 이후 실시간). 없으면 404 — 클라는 history 로 폴백(완료분은 DB 에 있음).
    """
    sess_row = (
        await session.execute(
            text(
                "SELECT user_id FROM chat_agent.sessions WHERE id = CAST(:sid AS uuid)"
            ).bindparams(sid=session_id)
        )
    ).first()
    if not sess_row:
        raise HTTPException(404, "Session not found")
    if str(sess_row.user_id) != str(admin.user_id):
        raise HTTPException(403, "Forbidden")

    relay = _active_relays.get(session_id)
    if relay is None:
        raise HTTPException(404, "No active stream")
    return StreamingResponse(
        relay.tail(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# AgentCore runtimeSessionId 는 최소 33자 요구. 우리 세션 UUID(36자)는 충족하지만
# 짧을 가능성에 대비해 패딩.
_AGENTCORE_SESSION_MIN = 33


def _agentcore_session_id(session_id: str) -> str:
    if len(session_id) >= _AGENTCORE_SESSION_MIN:
        return session_id
    return (session_id + "-" + "0" * _AGENTCORE_SESSION_MIN)[:_AGENTCORE_SESSION_MIN]


def _extract_reply(raw: str) -> str:
    """AgentCore 단일 JSON 응답에서 reply 텍스트 추출.

    우리 agent.main.invoke 는 {"reply", "agent", "phase"} dict 반환.
    파싱 실패 시 raw 그대로 반환 (관대).
    """
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict) and "reply" in obj:
            return str(obj["reply"])
    except (ValueError, TypeError):
        pass
    return raw


async def _agentcore_producer(
    relay: _StreamRelay,
    session_id: str,
    content: str,
    screen_context: dict | None = None,
    mode: str = "quick",
) -> None:
    """AgentCore InvokeAgentRuntime → 릴레이 발행 + DB 영속화 (background, §56).

    핸드오프 핵심: 이 task 는 **클라이언트 SSE 연결과 독립**이다. 사용자가 다른
    메뉴로 이동해 스트림이 끊겨도 AgentCore 소비·영속화는 끝까지 진행되고,
    완료분은 chat_agent.messages 에 저장된다(복귀 시 history/재구독으로 복원).
    DB 는 요청 스코프 세션 대신 **자체 AsyncSessionLocal** 사용(요청 세션은
    응답 반환 후 닫히므로 background 에서 쓰면 안 됨).

    인증: 런타임이 SigV4(IAM) authorizer 이므로 admin-api 의 IAM 자격증명
    (EKS IRSA role) 으로 호출. admin 권한은 post_message 진입 시 검증됨.
    """
    import boto3
    from botocore.config import Config as BotoConfig

    payload = {"content": content, "session_id": session_id, "mode": mode}
    if screen_context:
        payload["screen_context"] = screen_context

    try:
        # read_timeout 900s — boto3 기본(60s)이면 deep 분석의 AgentCore flush 간격
        # (§51: sub-agent blocking 중 최대 ~60s+)에 ReadTimeout 으로 producer 가
        # 중간에 죽어 결과가 증발한다(§56 실측). retries=0: 재시도하면 같은 질문이
        # agent 에 중복 invoke 됨.
        client = boto3.client(
            "bedrock-agentcore",
            region_name=AGENTCORE_REGION,
            config=BotoConfig(
                read_timeout=900, connect_timeout=15, retries={"max_attempts": 0}
            ),
        )
        # boto3 호출은 blocking → 이벤트 루프 양보 위해 thread 로
        response = await asyncio.to_thread(
            client.invoke_agent_runtime,
            agentRuntimeArn=AGENTCORE_RUNTIME_ARN,
            runtimeSessionId=_agentcore_session_id(session_id),
            payload=json.dumps(payload).encode(),
        )

        # SigV4 응답 본문은 'response' (StreamingBody). 구버전 'payload' fallback.
        body = response.get("response") or response.get("payload")
        accumulated: list[str] = []
        # 영속화 누적: 스트림 종료 후 chat_agent.messages 에 저장해 새로고침/재조회
        # 시에도 SQL/차트/검증이 보이도록. UI applyEvent 와 **같은 형태**로 재구성:
        #   tool_calls = [{tool, args, result, status:'done'}] (tool_call+result 병합)
        #   charts = [spec, ...], validator = 마지막 단일 객체.
        # (이전엔 content 만 저장 → tool_calls/charts/validator 컬럼이 NULL 로 남아
        #  read 경로는 조회하는데 표시할 게 없었음.)
        acc_tool_calls: list[dict] = []
        acc_charts: list[dict] = []
        acc_validator: dict | None = None
        acc_reports: list[dict] = []  # 다운로드 리포트 카드(report 이벤트)

        # Phase 4: agent 가 async-generator 면 AgentCore 가 `data: <json>\n\n` 프레임을
        # 점진적으로 흘린다. StreamingBody.iter_lines() 로 한 줄씩 읽어 admin-ui SSE 로
        # 재발행. 각 next() 는 blocking 이라 to_thread 로 감싸 이벤트 루프 양보(전체
        # 루프를 감싸면 재버퍼링되므로 next() 단위로만).
        if hasattr(body, "iter_lines"):
            # chunk_size=1 — botocore 기본(1024)은 1KB 가 모일 때까지 read 가 블록돼
            # 첫 thinking/heartbeat 프레임이 ~5초 묶여 도착(§52 실측: 기본 5.03s →
            # 1바이트 0.51s). SSE 는 초당 수백 바이트 수준이라 1바이트 read 의
            # syscall 비용은 무시 가능 — 체감 첫 프레임이 즉시 도달하는 게 압도적 이득.
            line_iter = body.iter_lines(chunk_size=1)

            def _next_line():
                return next(line_iter, None)

            # heartbeat: 다음 라인을 기다리는 동안 HEARTBEAT_SECS 마다 SSE 코멘트
            # (`: keepalive`)를 흘려 (1) 연결 idle drop 방지 (2) 클라가 살아있음 인지.
            # 5-agent 파이프라인의 긴 침묵(SQL/Validator 실행) 구간 대응.
            # next() 를 한 번만 to_thread 로 띄워두고, 완료될 때까지 timeout 으로 폴링.
            HEARTBEAT_SECS = 10.0
            pending = asyncio.ensure_future(asyncio.to_thread(_next_line))
            while True:
                try:
                    line = await asyncio.wait_for(asyncio.shield(pending), HEARTBEAT_SECS)
                except asyncio.TimeoutError:
                    # 다음 라인 대기 — keepalive 는 relay.tail() 이 자체 발행
                    continue
                if line is None:
                    break
                # 다음 라인 prefetch 시작 (처리와 병행)
                pending = asyncio.ensure_future(asyncio.to_thread(_next_line))
                if not line:
                    continue  # SSE 빈 줄 구분자
                text_line = (
                    line.decode() if isinstance(line, (bytes, bytearray)) else line
                )
                if not text_line.startswith("data:"):
                    continue
                data_str = text_line[len("data:"):].strip()
                if data_str in ("", "[DONE]"):
                    continue
                try:
                    evt = json.loads(data_str)
                except (ValueError, TypeError):
                    evt = {"type": "text", "chunk": data_str}

                # AgentCore in-band 에러 프레임 (HTTP 는 200 유지)
                if isinstance(evt, dict) and "error_type" in evt:
                    err = json.dumps(
                        {
                            "error": evt.get("error", "stream error"),
                            "type": evt.get("error_type", "StreamError"),
                        }
                    )
                    await relay.publish(b"event: error\n")
                    await relay.publish(f"data: {err}\n\n".encode())
                    break

                etype = evt.get("type") if isinstance(evt, dict) else None
                if etype == "chart" and isinstance(evt.get("spec"), dict):
                    # strip: 표시 텍스트에서 제거할 원문 JSON 블록(admin-ui 가 사용)
                    payload_out = {"spec": evt["spec"]}
                    if evt.get("strip"):
                        payload_out["strip"] = evt["strip"]
                    acc_charts.append(evt["spec"])  # 영속화: charts[]
                    await relay.publish(b"event: chart\n")
                    await relay.publish(f"data: {json.dumps(payload_out)}\n\n".encode())
                elif etype == "thinking":
                    # "작업 중" 신호 — 본문에 누적하지 않고 그대로 전달
                    await relay.publish(b"event: thinking\n")
                    await relay.publish(f"data: {json.dumps({'text': evt.get('text', '')})}\n\n".encode())
                elif etype == "reasoning":
                    # 추론 요약 델타(orchestrator display:summarized) — 침묵 구간을
                    # 메우는 "사고 과정" 스트림. 본문(accumulated)에 누적하지 않고
                    # 그대로 전달(답변과 분리, DB 영속화 대상 아님).
                    await relay.publish(b"event: reasoning\n")
                    await relay.publish(f"data: {json.dumps({'chunk': evt.get('chunk', '')})}\n\n".encode())
                elif etype == "tool_call":
                    # 도구 호출 투명성 (어떤 specialist 를 부르는지)
                    tool_name = evt.get("tool", "")
                    # 영속화: UI applyEvent 와 동일하게 running 상태로 추가
                    acc_tool_calls.append(
                        {"tool": tool_name, "args": evt.get("args", {}), "status": "running"}
                    )
                    await relay.publish(b"event: tool_call\n")
                    await relay.publish(f"data: {json.dumps({'tool': tool_name, 'args': evt.get('args', {})})}\n\n".encode())
                elif etype == "tool_result":
                    # 실행된 코드/구조화 결과 (Code Specialist 의 code 포함)
                    tool_name = evt.get("tool", "")
                    tool_result = evt.get("result", {})
                    # 영속화: 같은 이름 중 **result 가 아직 없는 첫 항목**에 채움(§57 fix).
                    # 끝에서 스캔하면 같은 이름 호출 N개가 모두 마지막 항목에 덮어써져
                    # 앞 항목들이 영원히 running 으로 남는다(복원 시 "실행 중..." 고착).
                    for i in range(len(acc_tool_calls)):
                        if (
                            acc_tool_calls[i]["tool"] == tool_name
                            and "result" not in acc_tool_calls[i]
                        ):
                            acc_tool_calls[i]["result"] = tool_result
                            acc_tool_calls[i]["status"] = "done"
                            break
                    else:
                        acc_tool_calls.append(
                            {"tool": tool_name, "result": tool_result, "status": "done"}
                        )
                    await relay.publish(b"event: tool_result\n")
                    await relay.publish(f"data: {json.dumps({'tool': tool_name, 'result': tool_result})}\n\n".encode())
                elif etype == "validator":
                    # reconciliation gate WARN 등. 영속화: 마지막 verdict 단일 객체.
                    acc_validator = evt.get("result", {})
                    await relay.publish(b"event: validator\n")
                    await relay.publish(f"data: {json.dumps({'result': evt.get('result', {})})}\n\n".encode())
                elif etype == "verification":
                    # L3 실행기반 후보선택 검증 메타(§58, deep 모드만). agreement/k/
                    # verdict 를 그대로 전달 — UI 가 "검증됨" 카드로 렌더(설명가능성).
                    # 휘발성 진행표시라 DB 영속화 안 함(validator 와 동일 취급).
                    await relay.publish(b"event: verification\n")
                    await relay.publish(
                        f"data: {json.dumps({'result': evt.get('result', {})})}\n\n".encode()
                    )
                elif etype == "audit":
                    # L5 독립 답변 감사(§60, deep+고위험만). 최종 산문 수치 cite 무결성
                    # verdict/defects 를 그대로 전달 — UI advisory 카드(비파괴). validator
                    # 와 동일하게 휘발성 진행표시로 취급(DB 영속화 안 함).
                    await relay.publish(b"event: audit\n")
                    await relay.publish(
                        f"data: {json.dumps({'result': evt.get('result', {})})}\n\n".encode()
                    )
                elif etype == "heartbeat":
                    # 공백 없는 스트리밍 생존신호(진행 단계/경과시간). 본문(accumulated)에
                    # 절대 누적하지 않고 그대로 전달 — DB 영속화 대상 아님(휘발성 진행표시).
                    # ⚠️ 명시 elif 필수: 없으면 아래 else 가 'phase'/'label' 없는 dict 라
                    # chunk 추출 실패로 무시되거나, 키가 겹치면 본문 오염. (transport-level
                    # `: keepalive` SSE 코멘트와는 다른, 데이터 이벤트.)
                    await relay.publish(b"event: heartbeat\n")
                    await relay.publish((
                        "data: "
                        + json.dumps(
                            {
                                "phase": evt.get("phase", ""),
                                "label": evt.get("label", ""),
                                "elapsed_ms": evt.get("elapsed_ms", 0),
                            }
                        )
                        + "\n\n"
                    ).encode())
                elif etype == "plan":
                    # deep 모드 분석 계획(§57 PlanCard). strip 으로 본문에서 raw
                    # JSON 펜스 제거(차트와 동일 패턴). 영속화는 본문 텍스트에
                    # 펜스가 남으므로 별도 컬럼 불필요(복원 시 UI 가 재추출).
                    payload_out = {"plan": evt.get("plan", {})}
                    if evt.get("strip"):
                        payload_out["strip"] = evt["strip"]
                    await relay.publish(b"event: plan\n")
                    await relay.publish(
                        f"data: {json.dumps(payload_out)}\n\n".encode()
                    )
                elif etype == "report":
                    # 다운로드 리포트 카드. s3_uri 는 그대로 전달(UI 가 다운로드 클릭 시
                    # /reports/download 로 presign 요청 — URL 을 미리 굽지 않음, 만료·검증
                    # 우회 방지). 영속화: tool_calls 와 함께 acc 에 저장해 새로고침 후도
                    # 카드 유지(사용자 결정: 세션 중만이지만 DB 에 흔적은 남겨 둠).
                    rep = {
                        "s3_uri": evt.get("s3_uri", ""),
                        "file_name": evt.get("file_name", "report"),
                        "format": evt.get("format", "pdf"),
                        "summary": evt.get("summary", ""),
                        "page_count": evt.get("page_count"),
                    }
                    acc_reports.append(rep)
                    await relay.publish(b"event: report\n")
                    await relay.publish(f"data: {json.dumps(rep)}\n\n".encode())
                elif etype == "done":
                    continue  # 루프 종료 후 자체 done 발행
                else:
                    chunk = (
                        (evt.get("chunk") or evt.get("delta") or evt.get("reply") or "")
                        if isinstance(evt, dict)
                        else str(evt)
                    )
                    if chunk:
                        accumulated.append(chunk)
                        await relay.publish(b"event: text\n")
                        await relay.publish(f"data: {json.dumps({'chunk': chunk})}\n\n".encode())
        else:
            # Fallback: 비스트리밍 단일 JSON 응답
            raw = body.read().decode() if hasattr(body, "read") else json.dumps(response, default=str)
            reply = _extract_reply(raw)
            accumulated.append(reply)
            await relay.publish(b"event: text\n")
            await relay.publish(f"data: {json.dumps({'chunk': reply})}\n\n".encode())

        await relay.publish(b"event: done\n")
        await relay.publish(b"data: {}\n\n")

        # assistant 메시지 저장 (스트림 누적분 + tool_calls/charts/validator).
        # jsonb 컬럼은 None 이면 SQL NULL, 있으면 JSON 문자열을 ::jsonb 캐스팅.
        # 빈 리스트는 저장 안 함(NULL) → read 경로가 undefined 로 매핑(UI 와 일관).
        # tool_result 없는 도구(render_chart 등)가 running 으로 영속화되면 복원
        # 시 "실행 중..." 고착(§57) — 스트림 종료 시점엔 전부 완료이므로 done 처리.
        for tc in acc_tool_calls:
            if tc.get("status") == "running":
                tc["status"] = "done"
        full_reply = "".join(accumulated)
        # [SUGGESTIONS]...[/SUGGESTIONS] 마커는 UI 전용(후속질문 칩) — DB 본문에서
        # 제거(§55). UI 도 동일 정규식으로 추출·제거하므로 양쪽 일관.
        full_reply = re.sub(
            r"\s*\[SUGGESTIONS\][\s\S]*?\[/SUGGESTIONS\]\s*", "", full_reply
        ).rstrip()
        async with AsyncSessionLocal() as own_db:
            await own_db.execute(
                text(
                    "INSERT INTO chat_agent.messages "
                    "(session_id, role, content, tool_calls, charts, validator) "
                    "VALUES (CAST(:sid AS uuid), 'assistant', :c, "
                    "CAST(:tc AS jsonb), CAST(:ch AS jsonb), CAST(:vd AS jsonb))"
                ).bindparams(
                    sid=session_id,
                    c=full_reply[:8000],
                    tc=json.dumps(acc_tool_calls) if acc_tool_calls else None,
                    ch=json.dumps(acc_charts) if acc_charts else None,
                    vd=json.dumps(acc_validator) if acc_validator else None,
                )
            )
            await own_db.commit()

    except Exception as exc:
        err = json.dumps({"error": str(exc), "type": type(exc).__name__})
        await relay.publish(b"event: error\n")
        await relay.publish(f"data: {err}\n\n".encode())
    finally:
        # tail 구독자 종료 신호 + 릴레이 정리(완료 후 재구독은 history 폴백).
        await relay.finish()
        if _active_relays.get(session_id) is relay:
            _active_relays.pop(session_id, None)


# ─── 인라인 SQL 재실행 (§57 Phase2-B — deep-insight 차용) ───
# UI 의 SQL 블록에서 분석가가 SQL 을 보고/수정해 **LLM 미경유**로 재실행(ms~s 단위,
# 토큰 비용 0). 보안: query_db Lambda 의 Layer A 검증 스택(sqlglot AST + 테이블
# 화이트리스트 + SELECT-only + EXPLAIN cost + LIMIT 강제 + read-only role +
# statement_timeout)을 **그대로 재사용** — admin-api 는 검증 로직을 중복 구현하지
# 않고 Lambda 에 위임한다. require_admin + 세션 소유 검증.
LAMBDA_QUERY_DB = os.environ.get(
    "LAMBDA_QUERY_DB", "llm-gateway-dev-chat-agent-query-db"
)


class SqlExecuteRequest(BaseModel):
    sql: str
    session_id: str


@router.post("/sql/execute")
async def execute_sql(
    req: SqlExecuteRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """편집된 SQL 을 LLM 없이 직접 실행(검증은 query_db Lambda 가 수행).

    Lambda 의 ValidationError(화이트리스트 위반/DML 등)는 ok:false + error 로
    그대로 반환 — 에디터 UX 의 핵심(분석가가 에러를 보고 고침).
    """
    # 세션 소유 검증(임의 세션 키로 호출 방지)
    sess_row = (
        await session.execute(
            text(
                "SELECT user_id FROM chat_agent.sessions WHERE id = CAST(:sid AS uuid)"
            ).bindparams(sid=req.session_id)
        )
    ).first()
    if not sess_row:
        raise HTTPException(404, "Session not found")
    if str(sess_row.user_id) != str(admin.user_id):
        raise HTTPException(403, "Forbidden")

    import boto3

    def _invoke() -> dict:
        client = boto3.client("lambda", region_name=AGENTCORE_REGION)
        resp = client.invoke(
            FunctionName=LAMBDA_QUERY_DB,
            Payload=json.dumps(
                {
                    "sql": req.sql,
                    "session_id": req.session_id,
                    "step_id": f"manual-{uuid.uuid4().hex[:8]}",
                }
            ).encode(),
        )
        return json.loads(resp["Payload"].read())

    try:
        result = await asyncio.to_thread(_invoke)
    except Exception as exc:  # Lambda 호출 자체 실패(권한/네트워크)
        raise HTTPException(502, f"query execution failed: {exc}") from exc
    # ok:false(검증 실패 등)도 200 으로 그대로 — 에디터가 에러 메시지 표시
    return result


# ─── Report download (presigned URL) ───
# report_specialist 가 생성한 리포트 파일(S3 reports/ prefix)을 presigned URL 로
# 다운로드. URL 을 agent/스트림에 미리 굽지 않고 클릭 시점에 발급 — 만료(5분) 통제
# + 검증 우회 방지. 보안 2중: (1) 버킷==staging 버킷 AND key prefix=='reports/'
# 화이트리스트(임의 객체 다운로드 차단), (2) require_admin(API 전체 관리자 전용).
REPORT_STAGING_BUCKET = os.environ.get("CHAT_STAGING_BUCKET", "")
REPORT_PRESIGN_EXPIRY = int(os.environ.get("REPORT_PRESIGN_EXPIRY_SECONDS", "300"))
_REPORT_PREFIX = "reports/"
_CONTENT_TYPES = {
    "pdf": "application/pdf",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _parse_report_uri(uri: str) -> tuple[str, str]:
    """s3://bucket/key 를 (bucket, key) 로. 검증 실패 시 400.

    보안 화이트리스트: 버킷은 staging 버킷과 정확히 일치해야 하고, key 는
    'reports/' prefix 로 시작해야 한다. '..' 세그먼트(이중 인코딩 포함 단일
    디코드 후)도 차단.
    """
    if not uri.startswith("s3://"):
        raise HTTPException(400, "Invalid S3 URI")
    rest = uri[len("s3://"):]
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        raise HTTPException(400, "Malformed S3 URI")
    if not REPORT_STAGING_BUCKET or bucket != REPORT_STAGING_BUCKET:
        raise HTTPException(403, "Bucket not allowed")
    if not key.startswith(_REPORT_PREFIX):
        raise HTTPException(403, "Key prefix not allowed")
    if ".." in key:
        raise HTTPException(400, "Invalid key")
    return bucket, key


@router.get("/reports/download")
async def download_report(
    uri: str,
    admin: CurrentUser = Depends(require_admin),
) -> dict:
    """리포트 파일 presigned 다운로드 URL 발급(5분 만료).

    require_admin 으로 비관리자 접근 차단 + prefix/버킷 화이트리스트로 임의 S3
    객체 다운로드 차단. URL 은 호출 시점에만 발급해 만료를 통제한다.
    """
    import boto3
    from botocore.exceptions import ClientError

    bucket, key = _parse_report_uri(uri)
    file_name = os.path.basename(key) or "report"
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    content_type = _CONTENT_TYPES.get(ext, "application/octet-stream")

    s3 = boto3.client("s3", region_name=AGENTCORE_REGION)
    # 존재/권한 확인 — AccessDenied 와 NotFound 를 구분(blanket 404 금지).
    try:
        s3.head_object(Bucket=bucket, Key=key)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            raise HTTPException(404, "Report not found") from e
        if code in ("403", "AccessDenied"):
            # presign 권한 자체 문제 — 운영 진단을 위해 구분(IAM s3:GetObject/kms).
            raise HTTPException(502, "Storage access denied (check IAM)") from e
        raise HTTPException(502, "Storage error") from e

    # RFC 5987 — 다국어/특수문자 파일명 안전(헤더 인젝션 방지 + 깨짐 방지).
    from urllib.parse import quote

    disposition = f"attachment; filename*=UTF-8''{quote(file_name)}"
    url = s3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": bucket,
            "Key": key,
            "ResponseContentDisposition": disposition,
            "ResponseContentType": content_type,
        },
        ExpiresIn=REPORT_PRESIGN_EXPIRY,
    )
    return {"download_url": url, "file_name": file_name, "expires_in": REPORT_PRESIGN_EXPIRY}


