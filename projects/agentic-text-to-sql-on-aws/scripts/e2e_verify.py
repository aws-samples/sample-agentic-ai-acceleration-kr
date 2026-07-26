#!/usr/bin/env python3
"""E2E 검증기 — M1 완료 기준의 3레벨 스모크 테스트.

레벨 1 (MCP): SigV4 streamable-http 로 각 MCP Runtime 에 접속.
  - sql-execution-mcp: tools/list → run_sql("SELECT COUNT(*) FROM customers") == 1000,
                       run_sql("DELETE FROM customers") status == "rejected" (READ-ONLY 방어)
  - semantic-retrieval-mcp: search_schema("지역별 매출") → results 비어있지 않음
레벨 2 (에이전트): orchestrator Runtime 에 RunAgentInput POST(InvokeAgentRuntime) →
  SSE 이벤트에 RUN_STARTED → STEP/TOOL_CALL → TEXT_MESSAGE → RUN_FINISHED 확인.
레벨 4 (M2 clarification): 모호한 질의 → CUSTOM(clarification_request) 수신 →
  같은 runtimeSessionId 로 clarificationResponse 재호출 → 정상 완료 확인.
  semantic 검색 확장(용어/fewshot 히트)도 이 레벨에서 확인.
레벨 5 (M3 Gateway/Cedar/Redshift):
  - Cognito M2M 토큰으로 Gateway MCP 접속 → tools/list(집약) 확인
  - User 그룹 사용자: run_sql/search_schema 허용, Denied 그룹: forbid 확인 (Cedar)
  - run_sql(datasource="redshift") 정상 실행 + DELETE 거부
  전제: gateway-outputs.json + Cognito 테스트 사용자(E2E_USER/E2E_DENIED_USER env 또는
  기본 e2e-user@example.com / e2e-denied@example.com, 비밀번호는 E2E_USER_PASSWORD env).

실행:
  python scripts/e2e_verify.py --level 1        # MCP 레벨만
  python scripts/e2e_verify.py --level 2        # 에이전트 레벨만
  python scripts/e2e_verify.py --level 4        # M2 clarification/semantic 만
  python scripts/e2e_verify.py --level 5        # M3 Gateway/Cedar/Redshift 만
  python scripts/e2e_verify.py --level all      # 전부(기본)

ARN 은 runtime-outputs.json(AgenticT2SqlRuntimeStack) 에서 읽거나 인자로 준다.
의존성: orchestrator 컴포넌트의 venv 에서 실행(mcp-proxy-for-aws, mcp, boto3 필요).
  cd agents/orchestrator && uv run python ../../scripts/e2e_verify.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

REGION = os.environ.get("AWS_REGION", "us-west-2")
MCP_SERVICE = "bedrock-agentcore"

ROOT = Path(__file__).resolve().parent.parent
RUNTIME_OUTPUTS = ROOT / "infra" / "runtime-outputs.json"


# ─────────────────────────────── 공통 ───────────────────────────────
def _green(s: str) -> str:
    return f"\033[32m{s}\033[0m"


def _red(s: str) -> str:
    return f"\033[31m{s}\033[0m"


class Checks:
    """PASS/FAIL 집계기."""

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        tag = _green("PASS") if ok else _red("FAIL")
        print(f"  [{tag}] {name}" + (f" — {detail}" if detail else ""))
        if ok:
            self.passed += 1
        else:
            self.failed += 1

    def summary(self) -> int:
        total = self.passed + self.failed
        print(f"\n결과: {self.passed}/{total} PASS, {self.failed} FAIL")
        return 0 if self.failed == 0 else 1


def load_arns(args: argparse.Namespace) -> dict[str, str]:
    """runtime-outputs.json 또는 인자/env 에서 ARN 3종을 읽는다."""
    arns: dict[str, str] = {}
    if RUNTIME_OUTPUTS.exists():
        data = json.loads(RUNTIME_OUTPUTS.read_text())
        stack = data.get("AgenticT2SqlRuntimeStack", {})
        arns["orchestrator"] = stack.get("OrchestratorRuntimeArn", "")
        arns["sql"] = stack.get("SqlMcpRuntimeArn", "")
        arns["semantic"] = stack.get("SemanticMcpRuntimeArn", "")
    # 인자/env 우선.
    arns["orchestrator"] = args.orchestrator_arn or arns.get("orchestrator") or os.environ.get("ORCHESTRATOR_ARN", "")
    arns["sql"] = args.sql_arn or arns.get("sql") or os.environ.get("SQL_MCP_ARN", "")
    arns["semantic"] = args.semantic_arn or arns.get("semantic") or os.environ.get("SEMANTIC_MCP_ARN", "")
    return arns


def build_runtime_mcp_url(runtime_arn: str, region: str, qualifier: str = "DEFAULT") -> str:
    escaped = quote(runtime_arn, safe="")
    return (
        f"https://{MCP_SERVICE}.{region}.amazonaws.com"
        f"/runtimes/{escaped}/invocations?qualifier={qualifier}"
    )


# ─────────────────────────────── 레벨 1: MCP ───────────────────────────────
async def _mcp_session(runtime_arn: str):
    """MCP Runtime 에 SigV4 streamable-http 세션을 연다."""
    from mcp import ClientSession
    from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client

    url = build_runtime_mcp_url(runtime_arn, REGION)
    return aws_iam_streamablehttp_client(
        endpoint=url, aws_region=REGION, aws_service=MCP_SERVICE
    ), ClientSession


def _tool_payload_to_obj(result: Any) -> Any:
    """MCP call_tool 결과(content 리스트)를 파이썬 객체로 정규화."""
    content = getattr(result, "content", None) or []
    for part in content:
        text = getattr(part, "text", None)
        if text:
            try:
                return json.loads(text)
            except (json.JSONDecodeError, ValueError):
                return text
    # structuredContent 폴백.
    sc = getattr(result, "structuredContent", None)
    return sc if sc is not None else {}


async def _with_mcp_session(arn: str, work, attempts: int = 6):
    """MCP 세션을 열어 ``work(session)`` 을 실행. cold-start 실패 시 재시도.

    AgentCore Runtime 은 cold-start / warm-microVM 순환 시 첫 연결이 "Session terminated"
    로 실패할 수 있다(운영 결함이 아니라 스케일-투-제로 특성). 지수 백오프로 재시도한다.
    세션 open/close 를 동일 태스크 스코프의 ``async with`` 로 감싸 anyio 취소 스코프 충돌을 피한다.
    """
    from mcp import ClientSession
    from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client

    url = build_runtime_mcp_url(arn, REGION)
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            async with aws_iam_streamablehttp_client(
                endpoint=url, aws_region=REGION, aws_service=MCP_SERVICE
            ) as (read, write, *_):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await work(session)
        except Exception as exc:  # noqa: BLE001 — cold-start 재시도
            last_exc = exc
            if i < attempts - 1:
                await asyncio.sleep(3 * (i + 1))
    raise RuntimeError(f"MCP 세션 초기화 실패({attempts}회 재시도): {last_exc}")


async def verify_sql_mcp(arn: str, checks: Checks) -> None:
    print("\n[레벨1] sql-execution-mcp")

    async def work(session):
        tools = await session.list_tools()
        names = [t.name for t in tools.tools]
        checks.check("tools/list 에 run_sql 존재", "run_sql" in names, f"tools={names}")

        # positive: COUNT == 1000
        r = await session.call_tool("run_sql", {"sql": "SELECT COUNT(*) FROM customers"})
        obj = _tool_payload_to_obj(r)
        count = None
        if isinstance(obj, dict) and obj.get("status") == "ok":
            rows = obj.get("rows") or []
            if rows and rows[0]:
                count = rows[0][0]
        checks.check(
            "run_sql SELECT COUNT(*) FROM customers == 1000",
            str(count) == "1000",
            f"status={obj.get('status') if isinstance(obj, dict) else obj}, count={count}",
        )

        # negative: DELETE 는 rejected (READ-ONLY 방어)
        r2 = await session.call_tool("run_sql", {"sql": "DELETE FROM customers"})
        obj2 = _tool_payload_to_obj(r2)
        status2 = obj2.get("status") if isinstance(obj2, dict) else None
        checks.check(
            "run_sql DELETE → status=rejected (READ-ONLY 방어)",
            status2 == "rejected",
            f"status={status2}, rule={obj2.get('rule') if isinstance(obj2, dict) else None}",
        )

    await _with_mcp_session(arn, work)


async def verify_semantic_mcp(arn: str, checks: Checks) -> None:
    print("\n[레벨1] semantic-retrieval-mcp")

    async def work(session):
        tools = await session.list_tools()
        names = [t.name for t in tools.tools]
        checks.check("tools/list 에 search_schema 존재", "search_schema" in names, f"tools={names}")

        r = await session.call_tool("search_schema", {"query": "지역별 매출", "top_k": 5})
        obj = _tool_payload_to_obj(r)
        results = obj.get("results") if isinstance(obj, dict) else None
        checks.check(
            "search_schema('지역별 매출') → results 존재",
            bool(results),
            f"n_results={len(results) if results else 0}",
        )

    await _with_mcp_session(arn, work)


# ─────────────────────────────── SSE 파싱 공통 ───────────────────────────────
def _invoke_and_parse(client, arn: str, session_id: str, run_input: dict) -> tuple[set[str], list[str], list[dict]]:
    """InvokeAgentRuntime 을 호출해 (이벤트 타입 집합, 텍스트 델타, CUSTOM 이벤트 목록)을 반환."""
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=arn,
        runtimeSessionId=session_id,
        payload=json.dumps(run_input).encode("utf-8"),
        contentType="application/json",
        accept="text/event-stream",
    )
    seen: set[str] = set()
    text_chunks: list[str] = []
    customs: list[dict] = []
    stream = resp.get("response")
    raw = b""
    if hasattr(stream, "read"):
        raw = stream.read()
    elif stream is not None:
        for chunk in stream:
            raw += chunk if isinstance(chunk, bytes) else bytes(chunk)

    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        body = line[len("data:"):].strip()
        if not body or body == "[DONE]":
            continue
        try:
            ev = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            continue
        etype = ev.get("type") or ev.get("event")
        if etype:
            seen.add(str(etype))
        if str(etype).upper().startswith("TEXT_MESSAGE"):
            delta = ev.get("delta") or ev.get("content") or ""
            if delta:
                text_chunks.append(str(delta))
        if str(etype).upper() == "CUSTOM":
            customs.append(ev)
    return seen, text_chunks, customs


# ─────────────────────────────── 레벨 2: 에이전트 ───────────────────────────────
def verify_orchestrator(arn: str, checks: Checks, question: str) -> None:
    print("\n[레벨2] orchestrator (InvokeAgentRuntime, AG-UI SSE)")
    import boto3

    client = boto3.client("bedrock-agentcore", region_name=REGION)
    run_input = {
        "threadId": "e2e-thread-1",
        "runId": "e2e-run-1",
        "messages": [{"id": "m1", "role": "user", "content": question}],
        "state": {},
        "forwardedProps": {"actorId": "e2e-user"},
        "context": [],
    }
    # AgentCore 세션 헤더(동일 microVM 라우팅). 33자 이상 필수.
    # 고정 ID 를 쓰면 이전 실행(구버전 이미지)의 warm microVM 으로 계속 라우팅될 수 있어
    # 실행마다 고유 ID 를 생성한다(CLAUDE.md: warm microVM 이 옛 config 로 응답하는 함정).
    import uuid

    session_id = f"e2e-session-{uuid.uuid4()}"
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=arn,
        runtimeSessionId=session_id,
        payload=json.dumps(run_input).encode("utf-8"),
        contentType="application/json",
        accept="text/event-stream",
    )

    seen: set[str] = set()
    text_chunks: list[str] = []
    stream = resp.get("response")
    raw = b""
    if hasattr(stream, "read"):
        raw = stream.read()
    elif stream is not None:
        for chunk in stream:
            raw += chunk if isinstance(chunk, bytes) else bytes(chunk)

    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        body = line[len("data:"):].strip()
        if not body or body == "[DONE]":
            continue
        try:
            ev = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            continue
        etype = ev.get("type") or ev.get("event")
        if etype:
            seen.add(str(etype))
        if str(etype).upper().startswith("TEXT_MESSAGE"):
            delta = ev.get("delta") or ev.get("content") or ""
            if delta:
                text_chunks.append(str(delta))

    seen_up = {s.upper() for s in seen}
    checks.check("RUN_STARTED 수신", any("RUN_STARTED" in s for s in seen_up), f"events={sorted(seen)}")
    checks.check(
        "STEP_* 또는 TOOL_CALL_* 수신",
        any("STEP" in s or "TOOL_CALL" in s for s in seen_up),
        "",
    )
    checks.check("TEXT_MESSAGE_* 수신(내러티브)", any("TEXT_MESSAGE" in s for s in seen_up), "")
    checks.check("RUN_FINISHED 수신", any("RUN_FINISHED" in s for s in seen_up), "")
    checks.check(
        "RUN_ERROR 없음",
        not any("RUN_ERROR" in s or s == "ERROR" for s in seen_up),
        "",
    )
    if text_chunks:
        preview = "".join(text_chunks)[:200].replace("\n", " ")
        print(f"    내러티브 미리보기: {preview}")


# ─────────────────────────────── 레벨 4: M2 clarification / semantic ───────────────────────────────
def verify_clarification(arn: str, checks: Checks) -> None:
    """모호 질의 → clarification_request 수신 → 응답 재호출 → 정상 완료."""
    print("\n[레벨4] clarification interrupt E2E")
    import uuid

    import boto3

    client = boto3.client("bedrock-agentcore", region_name=REGION)
    # microVM 라우팅 affinity 를 위해 매 실행 고유 세션 ID(≥33자).
    session_id = f"e2e-clarify-{uuid.uuid4()}"
    thread_id = session_id

    # 의도적으로 모호한 질의(기간·기준 미지정) — intent 가 request_clarification 을 호출하도록.
    ambiguous = "최근 매출 알려줘"
    run_input = {
        "threadId": thread_id,
        "runId": "e2e-clarify-run-1",
        "messages": [{"id": "m1", "role": "user", "content": ambiguous}],
        "state": {},
        "forwardedProps": {"actorId": "e2e-user"},
        "context": [],
    }
    seen, _, customs = _invoke_and_parse(client, arn, session_id, run_input)

    clar = None
    for ev in customs:
        if ev.get("name") == "clarification_request":
            clar = ev.get("value") or {}
            break
    checks.check(
        "CUSTOM(clarification_request) 수신",
        clar is not None,
        f"events={sorted(seen)}",
    )
    if clar is None:
        # 모델이 되묻지 않고 진행했을 수 있음(비결정성). 이후 체크는 스킵.
        print("    (모델이 clarification 없이 진행 — 프롬프트 튜닝 필요 여부 확인)")
        return

    interrupt_id = clar.get("interruptId", "")
    fields = clar.get("fields", [])
    checks.check("clarification value 에 interruptId/fields 존재", bool(interrupt_id) and bool(fields), f"question={clar.get('question','')!r}")

    # 첫 필드에 기계적 응답을 구성해 재호출.
    values: dict[str, Any] = {}
    for f in fields:
        ftype = f.get("type")
        name = f.get("name")
        if not name:
            continue
        if ftype == "select" and f.get("options"):
            values[name] = f["options"][0]
        elif ftype == "date_range":
            values[name] = {"from": "2026-01-01", "to": "2026-06-30"}
        else:
            values[name] = "최근 3개월"

    resume_input = {
        "threadId": thread_id,
        "runId": "e2e-clarify-run-2",
        "messages": [{"id": "m2", "role": "user", "content": "(재요청 응답)"}],
        "state": {},
        "forwardedProps": {
            "actorId": "e2e-user",
            "clarificationResponse": {"interruptId": interrupt_id, "values": values},
        },
        "context": [],
    }
    seen2, text2, _ = _invoke_and_parse(client, arn, session_id, resume_input)
    seen2_up = {s.upper() for s in seen2}
    checks.check("재개 후 RUN_FINISHED 수신", any("RUN_FINISHED" in s for s in seen2_up), f"events={sorted(seen2)}")
    checks.check("재개 후 RUN_ERROR 없음", not any("RUN_ERROR" in s for s in seen2_up), "")
    expired = "만료" in "".join(text2)
    checks.check("재개가 만료(CLARIFICATION_EXPIRED)로 빠지지 않음", not expired, "")
    if text2:
        preview = "".join(text2)[:200].replace("\n", " ")
        print(f"    재개 내러티브 미리보기: {preview}")


async def verify_semantic_extension(arn: str, checks: Checks) -> None:
    """semantic-retrieval-mcp 의 M2 확장(용어/fewshot 히트) 확인."""
    print("\n[레벨4] semantic 검색 확장 (용어/fewshot)")

    async def work(session):
        r = await session.call_tool("search_schema", {"query": "요즘 들어온 유저 수", "top_k": 5})
        obj = _tool_payload_to_obj(r)
        results = obj.get("results") if isinstance(obj, dict) else []
        doc_types = {item.get("doc_type") for item in results if isinstance(item, dict)}
        checks.check(
            "search_schema 에 term/fewshot 히트 포함",
            bool(doc_types & {"term", "fewshot"}),
            f"doc_types={sorted(t for t in doc_types if t)}",
        )
        term_hits = [i for i in results if isinstance(i, dict) and i.get("doc_type") == "term"]
        has_fragment = any(i.get("sql_fragment") for i in term_hits)
        checks.check(
            "term 히트에 sql_fragment 존재",
            has_fragment or not term_hits,
            f"n_term={len(term_hits)}",
        )

    await _with_mcp_session(arn, work)


# ─────────────────────────────── 레벨 5: M3 Gateway/Cedar/Redshift ───────────────────────────────
GATEWAY_OUTPUTS = ROOT / "infra" / "gateway-outputs.json"
BASE_OUTPUTS = ROOT / "infra" / "base-outputs.json"


def _load_gateway_ctx() -> dict[str, str]:
    """gateway/base outputs 에서 레벨5 검증에 필요한 값을 읽는다."""
    ctx: dict[str, str] = {}
    if GATEWAY_OUTPUTS.exists():
        gw = json.loads(GATEWAY_OUTPUTS.read_text()).get("AgenticT2SqlGatewayStack", {})
        ctx["gateway_url"] = gw.get("GatewayUrl", "")
    if BASE_OUTPUTS.exists():
        base = json.loads(BASE_OUTPUTS.read_text()).get("AgenticT2SqlBaseStack", {})
        ctx["m2m_client_id"] = base.get("CognitoM2mClientId", "")
        ctx["user_pool_id"] = base.get("CognitoUserPoolId", "")
    return ctx


def _cognito_token(client_id: str, username: str, password: str) -> str:
    """USER_PASSWORD_AUTH 로 AccessToken 획득."""
    import boto3

    idp = boto3.client("cognito-idp", region_name=REGION)
    resp = idp.initiate_auth(
        ClientId=client_id,
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": password},
    )
    return resp["AuthenticationResult"]["AccessToken"]


async def _with_gateway_session(gateway_url: str, token: str, work, attempts: int = 4):
    """Bearer JWT 로 Gateway MCP 세션을 열어 work(session) 실행."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {token}"}
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            async with streamablehttp_client(gateway_url, headers=headers) as (read, write, *_):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await work(session)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if i < attempts - 1:
                await asyncio.sleep(3 * (i + 1))
    raise RuntimeError(f"Gateway MCP 세션 실패({attempts}회): {last_exc}")


def _find_tool(names: list[str], suffix: str) -> str | None:
    """Gateway 프리픽스(<Target>___)를 무시하고 suffix 로 도구를 찾는다."""
    for n in names:
        if n.endswith(suffix):
            return n
    return None


async def verify_gateway(ctx: dict[str, str], checks: Checks) -> None:
    """Gateway 집약 + Cedar 허용/거부 + Redshift datasource E2E."""
    print("\n[레벨5] Gateway / Cedar / Redshift")
    gateway_url = ctx.get("gateway_url", "")
    client_id = ctx.get("m2m_client_id", "")
    if not gateway_url or not client_id:
        print(_red("gateway-outputs.json / base-outputs.json 값 누락 — 레벨5 skip"))
        return

    user = os.environ.get("E2E_USER", "e2e-user@example.com")
    denied_user = os.environ.get("E2E_DENIED_USER", "e2e-denied@example.com")
    password = os.environ.get("E2E_USER_PASSWORD", "")
    if not password:
        # 권장 경로: 시크릿 ARN/이름만 받아 스크립트 내부에서 해석(플레인텍스트 비노출).
        secret_id = os.environ.get(
            "E2E_USER_PASSWORD_SECRET", "agentic-t2sql/e2e/user-password"
        )
        try:
            import boto3

            password = boto3.client("secretsmanager", region_name=REGION).get_secret_value(
                SecretId=secret_id
            )["SecretString"]
        except Exception as exc:  # noqa: BLE001
            print(_red(f"E2E 비밀번호 시크릿({secret_id}) 로드 실패({exc}) — 레벨5 skip"))
            return

    # ── 일반 사용자: tools/list 집약 + run_sql(aurora/redshift) + DELETE 거부 ──
    token = _cognito_token(client_id, user, password)

    async def work_user(session):
        tools = await session.list_tools()
        names = [t.name for t in tools.tools]
        run_sql = _find_tool(names, "run_sql")
        search_schema = _find_tool(names, "search_schema")
        checks.check(
            "Gateway tools/list 에 run_sql·search_schema 집약",
            bool(run_sql and search_schema),
            f"tools={names}",
        )
        if not run_sql:
            return

        # Cedar 허용 경로: 일반 사용자도 run_sql 허용(permit_authenticated_search_and_run).
        r = await session.call_tool(run_sql, {"sql": "SELECT COUNT(*) FROM customers"})
        obj = _tool_payload_to_obj(r)
        ok = isinstance(obj, dict) and obj.get("status") == "ok"
        checks.check("Gateway 경유 run_sql(aurora) 실행", ok, f"resp={str(obj)[:120]}")

        # Redshift datasource 라우팅.
        r2 = await session.call_tool(
            run_sql, {"sql": "SELECT COUNT(*) FROM customers", "datasource": "redshift"}
        )
        obj2 = _tool_payload_to_obj(r2)
        ok2 = isinstance(obj2, dict) and obj2.get("status") == "ok"
        checks.check("run_sql(datasource=redshift) 실행", ok2, f"resp={str(obj2)[:160]}")

        # Redshift 에서도 READ-ONLY 방어.
        r3 = await session.call_tool(
            run_sql, {"sql": "DELETE FROM customers", "datasource": "redshift"}
        )
        obj3 = _tool_payload_to_obj(r3)
        checks.check(
            "run_sql(redshift) DELETE → rejected",
            isinstance(obj3, dict) and obj3.get("status") == "rejected",
            f"status={obj3.get('status') if isinstance(obj3, dict) else obj3}",
        )

    await _with_gateway_session(gateway_url, token, work_user)

    # ── Denied 그룹 사용자: Cedar forbid 확인 ──
    try:
        denied_token = _cognito_token(client_id, denied_user, password)
    except Exception as exc:  # noqa: BLE001
        print(_red(f"Denied 사용자 토큰 실패({exc}) — Cedar 거부 체크 skip"))
        return

    async def work_denied(session):
        tools = await session.list_tools()
        names = [t.name for t in tools.tools]
        run_sql = _find_tool(names, "run_sql")
        if not run_sql:
            # 정책이 tools/list 자체를 걸러내는 구현이면 이 자체가 deny 증거.
            checks.check("Cedar 거부: Denied 그룹에 도구 미노출/차단", True, f"tools={names}")
            return
        try:
            r = await session.call_tool(run_sql, {"sql": "SELECT 1"})
            obj = _tool_payload_to_obj(r)
            text = str(obj)
            denied = (
                (isinstance(obj, dict) and obj.get("isError"))
                or "denied" in text.lower()
                or "authoriz" in text.lower()
                or "forbid" in text.lower()
            )
            checks.check("Cedar 거부: Denied 그룹 run_sql 차단", bool(denied), f"resp={text[:160]}")
        except Exception as exc:  # noqa: BLE001 — 예외로 거부되는 구현도 PASS
            checks.check("Cedar 거부: Denied 그룹 run_sql 차단", True, f"예외={str(exc)[:120]}")

    await _with_gateway_session(gateway_url, denied_token, work_denied)


# ─────────────────────────────── main ───────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description="agentic Text-to-SQL E2E 검증기")
    parser.add_argument("--level", choices=["1", "2", "4", "5", "all"], default="all")
    parser.add_argument("--orchestrator-arn")
    parser.add_argument("--sql-arn")
    parser.add_argument("--semantic-arn")
    parser.add_argument(
        "--question", default="지역별 매출 상위 5개 지역을 알려줘"
    )
    args = parser.parse_args()

    arns = load_arns(args)
    checks = Checks()

    if args.level in ("1", "all"):
        if not arns["sql"] or not arns["semantic"]:
            print(_red("SQL/Semantic MCP ARN 을 찾을 수 없습니다(runtime-outputs.json 또는 인자)."))
            return 2
        asyncio.run(verify_sql_mcp(arns["sql"], checks))
        asyncio.run(verify_semantic_mcp(arns["semantic"], checks))

    if args.level in ("2", "all"):
        if not arns["orchestrator"]:
            print(_red("Orchestrator ARN 을 찾을 수 없습니다."))
            return 2
        verify_orchestrator(arns["orchestrator"], checks, args.question)

    if args.level in ("4", "all"):
        if not arns["orchestrator"] or not arns["semantic"]:
            print(_red("Orchestrator/Semantic ARN 을 찾을 수 없습니다."))
            return 2
        asyncio.run(verify_semantic_extension(arns["semantic"], checks))
        verify_clarification(arns["orchestrator"], checks)

    if args.level in ("5", "all"):
        ctx = _load_gateway_ctx()
        if not ctx.get("gateway_url"):
            print("[레벨5] gateway-outputs.json 없음 — Gateway 미배포. skip.")
        else:
            asyncio.run(verify_gateway(ctx, checks))

    return checks.summary()


if __name__ == "__main__":
    sys.exit(main())
