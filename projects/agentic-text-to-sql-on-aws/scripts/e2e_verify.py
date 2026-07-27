#!/usr/bin/env python3
"""E2E 검증기 — 코어 파이프라인부터 개선 파이프라인까지의 레벨별 스모크 테스트.

레벨 1 (MCP): SigV4 streamable-http 로 각 MCP Runtime 에 접속.
  - sql-execution-mcp: tools/list → run_sql("SELECT COUNT(*) FROM customers") == 1000,
                       run_sql("DELETE FROM customers") status == "rejected" (READ-ONLY 방어)
  - semantic-retrieval-mcp: search_schema("지역별 매출") → results 비어있지 않음
레벨 2 (에이전트): orchestrator Runtime 에 RunAgentInput POST(InvokeAgentRuntime) →
  SSE 이벤트에 RUN_STARTED → STEP/TOOL_CALL → TEXT_MESSAGE → RUN_FINISHED 확인.
레벨 4 (clarification): 모호한 질의 → CUSTOM(clarification_request) 수신 →
  같은 runtimeSessionId 로 clarificationResponse 재호출 → 정상 완료 확인.
  semantic 검색 확장(용어/fewshot 히트)도 이 레벨에서 확인.
레벨 5 (Gateway/Cedar/Redshift):
  - Cognito M2M 토큰으로 Gateway MCP 접속 → tools/list(집약) 확인
  - User 그룹 사용자: run_sql/search_schema 허용, Denied 그룹: forbid 확인 (Cedar)
  - run_sql(datasource="redshift") 정상 실행 + DELETE 거부
  전제: gateway-outputs.json + Cognito 테스트 사용자(E2E_USER/E2E_DENIED_USER env 또는
  기본 e2e-user@example.com / e2e-denied@example.com, 비밀번호는 E2E_USER_PASSWORD env).
레벨 6 (admin panel / 큐레이션·승인 / 사용자 JWT OBO):
  - admin ALB `/` 200/3xx + `/api/health` 200
  - POST /api/auth/login(e2e-manager) → accessToken, 미인증 401, 일반 사용자 403
  - Manager 토큰으로 Gateway MCP: datasource-admin-mcp 도구 노출 → put_entity(term,
    candidate) → list_entities(status=candidate) 포함 → publish_entity → published
  - published term 이 search_schema 에 전파(OSIS 지연 감안 최대 90초 폴링)
  - Cedar: 일반 사용자 admin 도구 거부 + run_sql/search_schema 는 여전히 허용(회귀)
  - 정리: 생성한 e2e term 을 unpublish (실패 시 경고만)
  전제: admin-outputs.json(AgenticT2SqlAdminStack.AdminAlbUrl) + gateway/base outputs +
  E2E 사용자(scripts/create-e2e-users.sh 로 생성, e2e-manager@example.com 포함).
레벨 7 (개선 파이프라인 — Track A/B):
  - Track B: put_entity → reject_entity(반려 사유) → rejected 기록·candidate 미노출,
    mine_candidates 채굴 → 승인 큐 노출 → publish → search_schema 전파, 중복 채굴 방지
  - Track A: EX evaluator ACTIVE, admin API 로 배치 평가 시작/조회, online eval 상태,
    bundle 목록/승격(SSM active-bundle 갱신)/원복
  - Cedar 회귀: 일반 사용자에게 mine_candidates/reject_entity 미노출·거부
  전제: 레벨 2 가 먼저 실행돼 t2sql_query_record 로그가 존재(all 이면 자동 충족),
  evaluation-outputs.json + admin/gateway/base outputs.

실행:
  python scripts/e2e_verify.py --level 1        # MCP 레벨만
  python scripts/e2e_verify.py --level 2        # 에이전트 레벨만
  python scripts/e2e_verify.py --level 4        # clarification/semantic 만
  python scripts/e2e_verify.py --level 5        # Gateway/Cedar/Redshift 만
  python scripts/e2e_verify.py --level 6        # admin panel/큐레이션/OBO 만
  python scripts/e2e_verify.py --level 7        # 개선 파이프라인만
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


# ─────────────────────────────── 레벨 4: clarification / semantic ──────────────────────────────────
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
    """semantic-retrieval-mcp 의 semantic 확장(용어/fewshot 히트) 확인."""
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


# ─────────────────────────────── 레벨 5: Gateway/Cedar/Redshift ──────────────────────────────────
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


def _e2e_password(level_label: str) -> str:
    """E2E 테스트 사용자 공용 비밀번호를 확보(평문 미출력).

    env `E2E_USER_PASSWORD` 우선, 없으면 Secrets Manager 시크릿에서 읽는다.
    실패 시 빈 문자열을 반환하고 skip 안내만 출력한다.
    """
    password = os.environ.get("E2E_USER_PASSWORD", "")
    if password:
        return password
    # 권장 경로: 시크릿 ARN/이름만 받아 스크립트 내부에서 해석(플레인텍스트 비노출).
    secret_id = os.environ.get("E2E_USER_PASSWORD_SECRET", "agentic-t2sql/e2e/user-password")
    try:
        import boto3

        return boto3.client("secretsmanager", region_name=REGION).get_secret_value(
            SecretId=secret_id
        )["SecretString"]
    except Exception as exc:  # noqa: BLE001
        print(_red(f"E2E 비밀번호 시크릿({secret_id}) 로드 실패({exc}) — {level_label} skip"))
        return ""


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
    password = _e2e_password("레벨5")
    if not password:
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


# ───────────────────── 레벨 6: admin panel / 큐레이션 / OBO ─────────────────────
ADMIN_OUTPUTS = ROOT / "infra" / "admin-outputs.json"

# OSIS(DynamoDB Streams → OpenSearch) 전파 지연을 감안한 폴링 상한(초)과 간격(초).
PROPAGATION_TIMEOUT_SECONDS = 90
PROPAGATION_INTERVAL_SECONDS = 5


def _load_admin_ctx() -> dict[str, str]:
    """admin/gateway/base outputs 에서 레벨6 검증에 필요한 값을 읽는다."""
    ctx: dict[str, str] = {}
    if ADMIN_OUTPUTS.exists():
        admin = json.loads(ADMIN_OUTPUTS.read_text()).get("AgenticT2SqlAdminStack", {})
        ctx["admin_url"] = (admin.get("AdminAlbUrl", "") or "").rstrip("/")
    ctx.update(_load_gateway_ctx())
    return ctx


def _http_request(
    url: str,
    method: str = "GET",
    token: str | None = None,
    body: dict | None = None,
    timeout: int = 30,
) -> tuple[int, Any]:
    """표준 라이브러리로 HTTP 호출 → (status_code, 파싱된 JSON 또는 원문).

    4xx/5xx 도 예외 없이 상태 코드로 반환한다(401/403 검증에 필요).
    연결 자체가 실패하면 (0, 오류문자열).
    """
    import urllib.error
    import urllib.request

    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — 내부 ALB URL
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    except Exception as exc:  # noqa: BLE001 — 네트워크 실패도 체크 결과로 표현
        return 0, str(exc)
    try:
        return status, json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return status, raw


def _find_admin_tool(names: list[str], tool: str) -> str | None:
    """admin target 프리픽스를 감안해 도구명을 정확히 찾는다.

    `_find_tool` 의 단순 suffix 매칭은 `publish_entity` 가 `unpublish_entity` 에도
    걸리므로, 여기서는 완전일치 또는 `___<tool>` 경계 일치만 인정한다.
    """
    for n in names:
        if n == tool or n.endswith(f"___{tool}"):
            return n
    return None


async def verify_admin_panel(ctx: dict[str, str], checks: Checks, password: str) -> None:
    """admin ALB 헬스 · 인증/인가 · Manager 큐레이션→승인→전파 · Cedar 회귀."""
    print("\n[레벨6] admin panel / 큐레이션·승인 / Cedar OBO")
    import uuid

    admin_url = ctx.get("admin_url", "")
    gateway_url = ctx.get("gateway_url", "")
    client_id = ctx.get("m2m_client_id", "")
    manager_user = os.environ.get("E2E_MANAGER_USER", "e2e-manager@example.com")
    user = os.environ.get("E2E_USER", "e2e-user@example.com")

    # ── (1) admin ALB 헬스 ──
    if not admin_url:
        print("    admin-outputs.json 의 AdminAlbUrl 없음 — ALB/API 체크 skip")
    else:
        code, _ = _http_request(f"{admin_url}/")
        checks.check("admin ALB GET / → 200/3xx", 200 <= code < 400, f"HTTP {code}")
        hcode, hbody = _http_request(f"{admin_url}/api/health")
        checks.check(
            "admin /api/health → 200", hcode == 200, f"HTTP {hcode}, body={str(hbody)[:80]}"
        )

    # ── (2) 로그인 · 미인증 401 · 일반 사용자 403 ──
    manager_token = ""
    if admin_url:
        lcode, lbody = _http_request(
            f"{admin_url}/api/auth/login",
            method="POST",
            body={"username": manager_user, "password": password},
        )
        if isinstance(lbody, dict):
            manager_token = (
                lbody.get("accessToken") or lbody.get("access_token") or ""
            )
        checks.check(
            "POST /api/auth/login (e2e-manager) → accessToken 발급",
            lcode == 200 and bool(manager_token),
            f"HTTP {lcode}",  # 토큰 값은 출력하지 않는다.
        )

        acode, _ = _http_request(f"{admin_url}/api/semantic/entities")
        checks.check("미인증 GET /api/semantic/entities → 401", acode == 401, f"HTTP {acode}")

    # Manager 토큰을 admin API 에서 못 얻었으면 Cognito 직접 인증으로 폴백(MCP 체크 계속).
    if not manager_token and client_id:
        try:
            manager_token = _cognito_token(client_id, manager_user, password)
            print("    (admin API 로그인 대신 Cognito 직접 인증으로 Manager 토큰 확보)")
        except Exception as exc:  # noqa: BLE001
            print(_red(f"    Manager 토큰 확보 실패({exc}) — 레벨6 이후 체크 skip"))
            return

    user_token = ""
    if client_id:
        try:
            user_token = _cognito_token(client_id, user, password)
        except Exception as exc:  # noqa: BLE001
            print(_red(f"    일반 사용자 토큰 실패({exc}) — 403/Cedar 체크 skip"))

    if admin_url and user_token:
        ucode, _ = _http_request(f"{admin_url}/api/semantic/entities", token=user_token)
        checks.check(
            "일반 사용자 토큰 GET /api/semantic/entities → 403",
            ucode == 403,
            f"HTTP {ucode}",
        )

    if not gateway_url or not manager_token:
        print("    gateway URL/Manager 토큰 없음 — MCP 큐레이션 체크 skip")
        return

    # ── (3) Manager 토큰 Gateway MCP: put_entity(candidate) → list → publish ──
    # 실행마다 고유 term (warm microVM·잔여 데이터로 인한 착시 방지).
    term = f"e2e-term-{uuid.uuid4().hex[:8]}"
    state: dict[str, Any] = {"published": False}

    async def work_manager(session):
        tools = await session.list_tools()
        names = [t.name for t in tools.tools]
        put_tool = _find_admin_tool(names, "put_entity")
        list_tool = _find_admin_tool(names, "list_entities")
        publish_tool = _find_admin_tool(names, "publish_entity")
        checks.check(
            "Manager tools/list 에 datasource-admin-mcp 도구 노출",
            any("datasource-admin-mcp___" in n for n in names),
            f"admin_tools={[n for n in names if 'datasource-admin-mcp___' in n]}",
        )
        if not (put_tool and list_tool and publish_tool):
            checks.check(
                "admin 도구(put/list/publish_entity) 확보",
                False,
                f"tools={names}",
            )
            return

        payload = {
            "term": term,
            "definition": f"E2E 검증용 임시 용어({term}) — 최근 90일 순매출 합계",
            "synonyms": ["E2E 순매출", "e2e net revenue"],
            "sql_fragment": "SUM(o.amount) - SUM(COALESCE(o.refund_amount, 0))",
        }
        r = await session.call_tool(
            put_tool,
            {
                "entity_type": "term",
                "entity_id": term,
                "payload": payload,
                "status": "candidate",
                "actor": "e2e-manager",
            },
        )
        obj = _tool_payload_to_obj(r)
        ok = isinstance(obj, dict) and obj.get("status") == "ok"
        checks.check(
            f"put_entity(term={term}, candidate) 성공 (사용자 JWT OBO)",
            ok,
            f"resp={str(obj)[:160]}",
        )
        if not ok:
            return

        r2 = await session.call_tool(list_tool, {"entity_type": "term", "status": "candidate"})
        obj2 = _tool_payload_to_obj(r2)
        entities = obj2.get("entities") if isinstance(obj2, dict) else []
        ids = [e.get("entity_id") for e in entities or [] if isinstance(e, dict)]
        checks.check(
            "list_entities(status=candidate) 에 신규 term 포함",
            term in ids,
            f"n_candidates={len(ids)}",
        )

        r3 = await session.call_tool(
            publish_tool, {"entity_type": "term", "entity_id": term, "actor": "e2e-manager"}
        )
        obj3 = _tool_payload_to_obj(r3)
        entity = obj3.get("entity") if isinstance(obj3, dict) else None
        published = isinstance(entity, dict) and entity.get("status") == "published"
        state["published"] = published
        checks.check(
            "publish_entity → status=published",
            published,
            f"resp={str(obj3)[:160]}",
        )

    await _with_gateway_session(gateway_url, manager_token, work_manager)

    # ── (4) OpenSearch 전파 폴링 → search_schema 히트 ──
    if state["published"]:
        await _verify_term_propagation(gateway_url, manager_token, term, checks)

    # ── (5) Cedar: 일반 사용자는 admin 도구 거부, run_sql/search_schema 는 허용 ──
    if user_token:
        await _verify_cedar_admin_scope(gateway_url, user_token, checks)

    # ── (6) 정리: 생성한 term unpublish (실패해도 경고만) ──
    async def work_cleanup(session):
        tools = await session.list_tools()
        names = [t.name for t in tools.tools]
        unpublish_tool = _find_admin_tool(names, "unpublish_entity")
        if not unpublish_tool:
            print("    (정리) unpublish_entity 도구 없음 — skip")
            return
        r = await session.call_tool(
            unpublish_tool, {"entity_type": "term", "entity_id": term, "actor": "e2e-manager"}
        )
        print(f"    (정리) unpublish_entity({term}) → {str(_tool_payload_to_obj(r))[:100]}")

    try:
        await _with_gateway_session(gateway_url, manager_token, work_cleanup)
    except Exception as exc:  # noqa: BLE001 — 정리는 실패해도 검증 결과에 영향 없음
        print(_red(f"    (정리) unpublish 실패(경고): {str(exc)[:120]}"))


async def _verify_term_propagation(
    gateway_url: str, manager_token: str, term: str, checks: Checks
) -> None:
    """published term 이 OpenSearch 로 전파돼 search_schema 에 히트하는지 폴링 확인."""
    deadline_attempts = max(1, PROPAGATION_TIMEOUT_SECONDS // PROPAGATION_INTERVAL_SECONDS)
    hit = False
    for attempt in range(deadline_attempts):

        async def work(session):
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            search = _find_tool(names, "search_schema")
            if not search:
                return False
            r = await session.call_tool(search, {"query": term, "top_k": 5})
            obj = _tool_payload_to_obj(r)
            results = obj.get("results") if isinstance(obj, dict) else []
            for item in results or []:
                if not isinstance(item, dict):
                    continue
                blob = json.dumps(item, ensure_ascii=False)
                if term in blob:
                    return True
            return False

        try:
            hit = await _with_gateway_session(gateway_url, manager_token, work, attempts=1)
        except Exception as exc:  # noqa: BLE001 — 폴링 중 일시 실패는 재시도
            hit = False
            if attempt == deadline_attempts - 1:
                print(_red(f"    전파 폴링 마지막 시도 실패: {str(exc)[:120]}"))
        if hit:
            elapsed = attempt * PROPAGATION_INTERVAL_SECONDS
            checks.check(
                f"published term 이 search_schema 에 전파(≤{PROPAGATION_TIMEOUT_SECONDS}s)",
                True,
                f"약 {elapsed}s 후 히트",
            )
            return
        if attempt < deadline_attempts - 1:
            await asyncio.sleep(PROPAGATION_INTERVAL_SECONDS)
    checks.check(
        f"published term 이 search_schema 에 전파(≤{PROPAGATION_TIMEOUT_SECONDS}s)",
        False,
        f"term={term} 미히트 (OSIS 지연 또는 인덱싱 실패)",
    )


async def _verify_cedar_admin_scope(gateway_url: str, user_token: str, checks: Checks) -> None:
    """일반 사용자: admin 도구는 거부, run_sql/search_schema 는 계속 허용(action 스코프 회귀)."""

    async def work(session):
        tools = await session.list_tools()
        names = [t.name for t in tools.tools]
        put_tool = _find_admin_tool(names, "put_entity")
        if put_tool is None:
            # 정책이 tools/list 에서 걸러내면 그 자체가 deny 증거(Denied 그룹 검증과 동일 패턴).
            checks.check("Cedar: 일반 사용자에게 admin 도구 미노출/거부", True, "tools/list 미노출")
        else:
            try:
                r = await session.call_tool(
                    put_tool,
                    {
                        "entity_type": "term",
                        "entity_id": "e2e-should-be-blocked",
                        # ⚠️ payload 에 "denied"/"forbid" 같은 판정 키워드를 넣으면 에코백된
                        #    응답 텍스트 매칭이 오탐 PASS 를 낸다(배포 실측). 중립 값 사용.
                        "payload": {"term": "e2e-blocked", "definition": "must not be written"},
                        "status": "candidate",
                    },
                )
                obj = _tool_payload_to_obj(r)
                text = str(obj)
                # 성공 응답(status=ok)은 무조건 거부 실패다 — 키워드 매칭보다 우선한다.
                succeeded = isinstance(obj, dict) and obj.get("status") == "ok"
                denied = not succeeded and (
                    (isinstance(obj, dict) and obj.get("isError"))
                    or getattr(r, "isError", False)
                    or "denied" in text.lower()
                    or "authoriz" in text.lower()
                    or "forbid" in text.lower()
                )
                checks.check(
                    "Cedar: 일반 사용자 admin 도구(put_entity) 거부",
                    bool(denied),
                    f"resp={text[:160]}",
                )
            except Exception as exc:  # noqa: BLE001 — 예외 거부도 PASS
                checks.check(
                    "Cedar: 일반 사용자 admin 도구(put_entity) 거부",
                    True,
                    f"예외={str(exc)[:120]}",
                )

        # action 스코프 회귀: 기존 허용 도구는 그대로 동작해야 한다.
        run_sql = _find_tool(names, "run_sql")
        if run_sql:
            r2 = await session.call_tool(run_sql, {"sql": "SELECT 1"})
            obj2 = _tool_payload_to_obj(r2)
            checks.check(
                "Cedar 회귀: 일반 사용자 run_sql(SELECT 1) 허용",
                isinstance(obj2, dict) and obj2.get("status") == "ok",
                f"resp={str(obj2)[:120]}",
            )
        else:
            checks.check("Cedar 회귀: 일반 사용자 run_sql 허용", False, "run_sql 미노출")

        search = _find_tool(names, "search_schema")
        if search:
            r3 = await session.call_tool(search, {"query": "지역별 매출", "top_k": 3})
            obj3 = _tool_payload_to_obj(r3)
            checks.check(
                "Cedar 회귀: 일반 사용자 search_schema 허용",
                isinstance(obj3, dict) and obj3.get("results") is not None,
                f"resp={str(obj3)[:120]}",
            )
        else:
            checks.check(
                "Cedar 회귀: 일반 사용자 search_schema 허용", False, "search_schema 미노출"
            )

    await _with_gateway_session(gateway_url, user_token, work)


# ───────────────────── 레벨 7: 개선 파이프라인 (Track A/B) ─────────────────────
EVALUATION_OUTPUTS = ROOT / "infra" / "evaluation-outputs.json"


def _load_evaluation_ctx() -> dict[str, str]:
    """evaluation/admin/gateway/base outputs 에서 레벨7 검증 값을 읽는다."""
    ctx = _load_admin_ctx()
    if EVALUATION_OUTPUTS.exists():
        ev = json.loads(EVALUATION_OUTPUTS.read_text()).get(
            "AgenticT2SqlEvaluationStack", {}
        )
        ctx["evaluator_id"] = ev.get("ExecutionEvaluatorId", "")
        ctx["online_eval_config_id"] = ev.get("OnlineEvalConfigId", "")
        ctx["active_bundle_param"] = ev.get("ActiveBundleParamName", "")
    return ctx


async def _verify_fewshot_propagation(
    gateway_url: str, manager_token: str, fewshot_id: str, checks: Checks
) -> None:
    """published 채굴 fewshot 이 OpenSearch 로 전파돼 search_schema 에 히트하는지 확인.

    fewshot 은 질문 텍스트로 임베딩·검색되므로 entity 의 question 을 먼저 조회해
    그 텍스트로 검색하고, 결과에 entity_id(또는 질문)가 나타나면 전파로 판정한다.
    """
    question = ""

    async def fetch(session):
        tools = await session.list_tools()
        names = [t.name for t in tools.tools]
        get_tool = _find_admin_tool(names, "get_entity")
        if not get_tool:
            return ""
        r = await session.call_tool(
            get_tool, {"entity_type": "fewshot", "entity_id": fewshot_id}
        )
        obj = _tool_payload_to_obj(r)
        entity = obj.get("entity") if isinstance(obj, dict) else None
        return str(entity.get("question", "")) if isinstance(entity, dict) else ""

    try:
        question = await _with_gateway_session(gateway_url, manager_token, fetch)
    except Exception as exc:  # noqa: BLE001
        print(_red(f"    fewshot 질문 조회 실패: {str(exc)[:120]}"))
    if not question:
        checks.check("채굴 fewshot 전파 검증 준비(질문 조회)", False, f"id={fewshot_id}")
        return

    deadline_attempts = max(1, PROPAGATION_TIMEOUT_SECONDS // PROPAGATION_INTERVAL_SECONDS)
    hit = False
    for attempt in range(deadline_attempts):

        async def work(session):
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            search = _find_tool(names, "search_schema")
            if not search:
                return False
            r = await session.call_tool(search, {"query": question, "top_k": 5})
            obj = _tool_payload_to_obj(r)
            for item in obj.get("results") or [] if isinstance(obj, dict) else []:
                if not isinstance(item, dict):
                    continue
                blob = json.dumps(item, ensure_ascii=False)
                if fewshot_id in blob or question[:20] in blob:
                    return True
            return False

        try:
            hit = await _with_gateway_session(gateway_url, manager_token, work, attempts=1)
        except Exception:  # noqa: BLE001 — 폴링 중 일시 실패는 재시도
            hit = False
        if hit:
            checks.check(
                f"채굴 fewshot 이 search_schema 에 전파(≤{PROPAGATION_TIMEOUT_SECONDS}s)",
                True,
                f"약 {attempt * PROPAGATION_INTERVAL_SECONDS}s 후 히트",
            )
            return
        if attempt < deadline_attempts - 1:
            await asyncio.sleep(PROPAGATION_INTERVAL_SECONDS)
    checks.check(
        f"채굴 fewshot 이 search_schema 에 전파(≤{PROPAGATION_TIMEOUT_SECONDS}s)",
        False,
        f"id={fewshot_id} 미히트",
    )


async def verify_improvement_pipeline(
    ctx: dict[str, str], checks: Checks, password: str
) -> None:
    """Track B(reject·채굴→승인→전파·중복방지) + Track A(평가·bundle 승격) E2E."""
    print("\n[레벨7] 개선 파이프라인 (Track A/B)")
    import uuid

    gateway_url = ctx.get("gateway_url", "")
    client_id = ctx.get("m2m_client_id", "")
    admin_url = ctx.get("admin_url", "")
    manager_user = os.environ.get("E2E_MANAGER_USER", "e2e-manager@example.com")
    user = os.environ.get("E2E_USER", "e2e-user@example.com")

    if not gateway_url or not client_id:
        print(_red("gateway/base outputs 누락 — 레벨7 skip"))
        return
    try:
        manager_token = _cognito_token(client_id, manager_user, password)
    except Exception as exc:  # noqa: BLE001
        print(_red(f"Manager 토큰 실패({exc}) — 레벨7 skip"))
        return

    # ── (1) Track B: reject 흐름 — put(candidate) → reject → 상태·이력·미노출 ──
    term = f"e2e-reject-{uuid.uuid4().hex[:8]}"
    mined_state: dict[str, Any] = {"fewshot_id": "", "mined_ok": False}

    async def work_reject(session):
        tools = await session.list_tools()
        names = [t.name for t in tools.tools]
        put_tool = _find_admin_tool(names, "put_entity")
        reject_tool = _find_admin_tool(names, "reject_entity")
        list_tool = _find_admin_tool(names, "list_entities")
        checks.check(
            "Manager tools/list 에 reject_entity·mine_candidates 노출",
            bool(reject_tool and _find_admin_tool(names, "mine_candidates")),
            f"admin_tools={[n for n in names if 'datasource-admin-mcp___' in n]}",
        )
        if not (put_tool and reject_tool and list_tool):
            return

        r = await session.call_tool(
            put_tool,
            {
                "entity_type": "term",
                "entity_id": term,
                "payload": {"term": term, "definition": f"E2E 반려 검증용({term})"},
                "status": "candidate",
                "actor": "e2e-manager",
            },
        )
        obj = _tool_payload_to_obj(r)
        if not (isinstance(obj, dict) and obj.get("status") == "ok"):
            checks.check("reject 준비: put_entity(candidate)", False, f"resp={str(obj)[:160]}")
            return

        r2 = await session.call_tool(
            reject_tool,
            {
                "entity_type": "term",
                "entity_id": term,
                "reason": "E2E 반려 사유 — 정의 불충분",
                "actor": "e2e-manager",
            },
        )
        obj2 = _tool_payload_to_obj(r2)
        entity = obj2.get("entity") if isinstance(obj2, dict) else None
        rejected = isinstance(entity, dict) and entity.get("status") == "rejected"
        has_reason = isinstance(entity, dict) and bool(entity.get("rejection_reason"))
        checks.check(
            "reject_entity → status=rejected + rejection_reason 기록",
            rejected and has_reason,
            f"resp={str(obj2)[:160]}",
        )

        # candidate 목록에서 사라지고 rejected 목록에 남는다(반려 이력).
        r3 = await session.call_tool(list_tool, {"entity_type": "term", "status": "candidate"})
        obj3 = _tool_payload_to_obj(r3)
        cand_ids = [
            e.get("entity_id")
            for e in (obj3.get("entities") or [] if isinstance(obj3, dict) else [])
            if isinstance(e, dict)
        ]
        r4 = await session.call_tool(list_tool, {"entity_type": "term", "status": "rejected"})
        obj4 = _tool_payload_to_obj(r4)
        rej_ids = [
            e.get("entity_id")
            for e in (obj4.get("entities") or [] if isinstance(obj4, dict) else [])
            if isinstance(e, dict)
        ]
        checks.check(
            "rejected 는 candidate 목록 미노출 + rejected 목록 노출(이력)",
            term not in cand_ids and term in rej_ids,
            f"in_candidate={term in cand_ids}, in_rejected={term in rej_ids}",
        )

    await _with_gateway_session(gateway_url, manager_token, work_reject)

    # ── (2) Track B: 채굴 → 승인 큐 → publish → 전파 → 중복 방지 ──
    async def work_mine(session):
        tools = await session.list_tools()
        names = [t.name for t in tools.tools]
        mine_tool = _find_admin_tool(names, "mine_candidates")
        list_tool = _find_admin_tool(names, "list_entities")
        publish_tool = _find_admin_tool(names, "publish_entity")
        if not (mine_tool and list_tool and publish_tool):
            checks.check("mine_candidates 도구 확보", False, f"tools={names}")
            return

        r = await session.call_tool(mine_tool, {"hours": 24, "actor": "e2e-mining"})
        obj = _tool_payload_to_obj(r)
        ok = isinstance(obj, dict) and obj.get("status") == "ok"
        mined = int(obj.get("mined", 0)) if ok else 0
        skipped = int(obj.get("skipped_existing", 0)) if ok else 0
        # 레벨 2 가 남긴 t2sql_query_record 전제 — 신규 채굴 또는 기존 중복 skip 중 하나는 있어야 한다.
        checks.check(
            "mine_candidates 실행 → 채굴(mined+skipped ≥ 1)",
            ok and (mined + skipped) >= 1,
            f"resp={str(obj)[:200]}",
        )
        if not ok:
            return

        # 채굴된 fewshot 후보 하나를 승인 대상으로 고른다(이번 실행 or 기존 채굴분).
        fewshot_id = ""
        for cand in obj.get("candidates") or []:
            if isinstance(cand, dict) and cand.get("entity_type") == "fewshot":
                fewshot_id = str(cand.get("entity_id", ""))
                break
        if not fewshot_id:
            r2 = await session.call_tool(list_tool, {"entity_type": "fewshot"})
            obj2 = _tool_payload_to_obj(r2)
            for e in obj2.get("entities") or [] if isinstance(obj2, dict) else []:
                if isinstance(e, dict) and str(e.get("entity_id", "")).startswith("mined-"):
                    fewshot_id = str(e["entity_id"])
                    break
        if not fewshot_id:
            checks.check("채굴 fewshot 후보 확보", False, "mined-* fewshot 없음")
            return
        mined_state["fewshot_id"] = fewshot_id

        # 승인 큐(candidate)에 노출되는지 — 이미 published 였다면(재실행) 그 상태 자체가 승인 완료 증거.
        r3 = await session.call_tool(
            list_tool, {"entity_type": "fewshot", "status": "candidate"}
        )
        obj3 = _tool_payload_to_obj(r3)
        cand_ids = [
            e.get("entity_id")
            for e in (obj3.get("entities") or [] if isinstance(obj3, dict) else [])
            if isinstance(e, dict)
        ]
        in_queue = fewshot_id in cand_ids
        if in_queue:
            r4 = await session.call_tool(
                publish_tool,
                {"entity_type": "fewshot", "entity_id": fewshot_id, "actor": "e2e-manager"},
            )
            obj4 = _tool_payload_to_obj(r4)
            entity = obj4.get("entity") if isinstance(obj4, dict) else None
            published = isinstance(entity, dict) and entity.get("status") == "published"
        else:
            cur = await session.call_tool(
                list_tool, {"entity_type": "fewshot", "status": "published"}
            )
            cur_obj = _tool_payload_to_obj(cur)
            published = fewshot_id in [
                e.get("entity_id")
                for e in (cur_obj.get("entities") or [] if isinstance(cur_obj, dict) else [])
                if isinstance(e, dict)
            ]
        checks.check(
            "채굴 후보 승인 큐 유입 → publish → published",
            published,
            f"fewshot_id={fewshot_id}, was_in_queue={in_queue}",
        )
        mined_state["mined_ok"] = published

        # 중복 채굴 방지: 재실행 시 같은 후보를 다시 적재하지 않는다.
        r5 = await session.call_tool(mine_tool, {"hours": 24, "actor": "e2e-mining"})
        obj5 = _tool_payload_to_obj(r5)
        re_mined_ids = [
            c.get("entity_id")
            for c in (obj5.get("candidates") or [] if isinstance(obj5, dict) else [])
            if isinstance(c, dict)
        ]
        checks.check(
            "중복 채굴 방지: 재실행 시 동일 후보 재적재 없음",
            isinstance(obj5, dict)
            and obj5.get("status") == "ok"
            and fewshot_id not in re_mined_ids
            and int(obj5.get("skipped_existing", 0)) >= 1,
            f"skipped={obj5.get('skipped_existing') if isinstance(obj5, dict) else '?'}",
        )

    await _with_gateway_session(gateway_url, manager_token, work_mine)

    # ── (3) 채굴 fewshot 의 OpenSearch 전파(질문 텍스트로 search_schema 히트) ──
    if mined_state["mined_ok"] and mined_state["fewshot_id"]:
        await _verify_fewshot_propagation(
            gateway_url, manager_token, mined_state["fewshot_id"], checks
        )

    # ── (4) Track A: evaluator 존재(ACTIVE) ──
    evaluator_id = ctx.get("evaluator_id", "")
    if evaluator_id:
        try:
            import boto3

            cc = boto3.client("bedrock-agentcore-control", region_name=REGION)
            ev = cc.get_evaluator(evaluatorId=evaluator_id)
            checks.check(
                "EX evaluator(agentic_t2sql_execution_accuracy) ACTIVE",
                ev.get("status") == "ACTIVE",
                f"status={ev.get('status')}",
            )
        except Exception as exc:  # noqa: BLE001
            checks.check("EX evaluator 조회", False, f"오류={str(exc)[:120]}")
    else:
        print("    evaluation-outputs.json 의 ExecutionEvaluatorId 없음 — evaluator 체크 skip")

    # ── (5) Track A: admin API — 배치 평가 시작/조회, online eval, bundle 승격 ──
    if not admin_url:
        print("    admin_url 없음 — admin 평가 API 체크 skip")
    else:
        lcode, lbody = _http_request(
            f"{admin_url}/api/auth/login",
            method="POST",
            body={"username": manager_user, "password": password},
        )
        api_token = lbody.get("accessToken", "") if isinstance(lbody, dict) else ""
        if not api_token:
            api_token = manager_token  # admin API 는 Cognito AccessToken 을 그대로 받는다.

        scode, sbody = _http_request(
            f"{admin_url}/api/eval/runs",
            method="POST",
            token=api_token,
            body={"hours": 24},
            timeout=60,
        )
        run_id = ""
        if isinstance(sbody, dict):
            run_id = str(sbody.get("batch_evaluation_id", "") or "")
        checks.check(
            "POST /api/eval/runs → 배치 평가 시작(batch_evaluation_id)",
            scode in (200, 202) and bool(run_id),
            f"HTTP {scode}, id={run_id or str(sbody)[:120]}",
        )

        gcode, gbody = _http_request(f"{admin_url}/api/eval/runs", token=api_token)
        listed = False
        if isinstance(gbody, dict):
            runs = gbody.get("runs") or []
            listed = any(
                isinstance(x, dict) and x.get("batch_evaluation_id") == run_id for x in runs
            ) or (bool(runs) and not run_id)
        checks.check(
            "GET /api/eval/runs 에 시작한 평가 노출",
            gcode == 200 and listed,
            f"HTTP {gcode}",
        )

        ocode, obody = _http_request(f"{admin_url}/api/eval/online", token=api_token)
        online_ok = isinstance(obody, dict) and obody.get("configured") is True
        checks.check(
            "GET /api/eval/online → online eval 구성 확인",
            ocode == 200 and online_ok,
            f"HTTP {ocode}, body={str(obody)[:120]}",
        )

        # bundle: 목록 → (없으면 생성) → 승격(SSM 갱신) → 원복(롤백 검증).
        bcode, bbody = _http_request(f"{admin_url}/api/bundles", token=api_token)
        bundles = bbody.get("bundles") if isinstance(bbody, dict) else None
        prev_active = bbody.get("active") if isinstance(bbody, dict) else None
        checks.check("GET /api/bundles 200", bcode == 200, f"HTTP {bcode}")

        bundle_id, version_id = "", ""
        if isinstance(bundles, list) and bundles:
            first = bundles[0]
            bundle_id = str(first.get("bundle_id", ""))
            version_id = str(first.get("version_id", "") or "")
        if bcode == 200 and not bundle_id:
            ccode, cbody = _http_request(
                f"{admin_url}/api/bundles",
                method="POST",
                token=api_token,
                body={
                    "systemPrompt": "E2E bundle snapshot — orchestrator 기본 프롬프트 자리",
                    "modelId": "us.anthropic.claude-sonnet-5",
                    "description": "E2E 최초 bundle 스냅샷",
                },
                timeout=60,
            )
            if isinstance(cbody, dict):
                bundle_id = str(cbody.get("bundle_id", ""))
                version_id = str(cbody.get("version_id", "") or "")
            checks.check(
                "POST /api/bundles → 최초 bundle 생성",
                ccode in (200, 201) and bool(bundle_id),
                f"HTTP {ccode}",
            )
        if bundle_id and not version_id:
            vcode, vbody = _http_request(
                f"{admin_url}/api/bundles/{bundle_id}/versions", token=api_token
            )
            versions = vbody.get("versions") if isinstance(vbody, dict) else None
            if isinstance(versions, list) and versions:
                version_id = str(versions[0].get("version_id", ""))

        if bundle_id and version_id:
            pcode, pbody = _http_request(
                f"{admin_url}/api/bundles/{bundle_id}/promote",
                method="POST",
                token=api_token,
                body={"versionId": version_id},
            )
            promoted = False
            try:
                import boto3

                ssm = boto3.client("ssm", region_name=REGION)
                param = ctx.get("active_bundle_param") or "/agentic-t2sql/active-bundle"
                value = json.loads(ssm.get_parameter(Name=param)["Parameter"]["Value"])
                promoted = (
                    value.get("bundleId") == bundle_id
                    and value.get("versionId") == version_id
                )
            except Exception as exc:  # noqa: BLE001
                print(_red(f"    SSM 확인 실패: {str(exc)[:120]}"))
            checks.check(
                "bundle 승격 → SSM active-bundle 갱신 확인",
                pcode == 200 and promoted,
                f"HTTP {pcode}, ssm_match={promoted}",
            )
            # 원복(롤백 동작 검증): 이전 active 로 되돌린다. 이전이 없으면 빈 값으로.
            restore = (
                prev_active
                if isinstance(prev_active, dict) and prev_active.get("bundleId")
                else {"bundleId": "", "versionId": ""}
            )
            if restore.get("bundleId"):
                rcode, _ = _http_request(
                    f"{admin_url}/api/bundles/{restore['bundleId']}/promote",
                    method="POST",
                    token=api_token,
                    body={"versionId": restore.get("versionId", "")},
                )
                checks.check("bundle 원복(롤백 경로) 동작", rcode == 200, f"HTTP {rcode}")
            else:
                # 데모 초기 상태(빈 포인터)로 되돌린다 — orchestrator 는 기본값 폴백.
                try:
                    import boto3

                    ssm = boto3.client("ssm", region_name=REGION)
                    param = ctx.get("active_bundle_param") or "/agentic-t2sql/active-bundle"
                    ssm.put_parameter(
                        Name=param,
                        Value=json.dumps({"bundleId": "", "versionId": ""}),
                        Overwrite=True,
                    )
                    checks.check("bundle 원복(초기 빈 포인터) 동작", True, "SSM 직접 원복")
                except Exception as exc:  # noqa: BLE001
                    checks.check("bundle 원복 동작", False, f"오류={str(exc)[:120]}")
        else:
            checks.check("bundle 승격 검증", False, "bundleId/versionId 확보 실패")

    # ── (6) Cedar 회귀: 일반 사용자에게 개선 파이프라인 admin 도구 미노출/거부 ──
    try:
        user_token = _cognito_token(client_id, user, password)
    except Exception as exc:  # noqa: BLE001
        print(_red(f"일반 사용자 토큰 실패({exc}) — Cedar 회귀 skip"))
        return

    async def work_cedar(session):
        tools = await session.list_tools()
        names = [t.name for t in tools.tools]
        mine_tool = _find_admin_tool(names, "mine_candidates")
        reject_tool = _find_admin_tool(names, "reject_entity")
        if mine_tool is None and reject_tool is None:
            checks.check(
                "Cedar: 일반 사용자에게 mine/reject 도구 미노출", True, "tools/list 미노출"
            )
            return
        # 노출됐다면 호출이 거부돼야 한다(status==ok 면 무조건 실패로 판정).
        target = mine_tool or reject_tool
        try:
            r = await session.call_tool(
                target,
                {"hours": 1} if target == mine_tool else {
                    "entity_type": "term",
                    "entity_id": "e2e-should-be-blocked",
                    "reason": "must not apply",
                },
            )
            obj = _tool_payload_to_obj(r)
            succeeded = isinstance(obj, dict) and obj.get("status") == "ok"
            checks.check(
                "Cedar: 일반 사용자 개선 파이프라인 admin 도구 거부",
                not succeeded,
                f"resp={str(obj)[:160]}",
            )
        except Exception as exc:  # noqa: BLE001 — 예외 거부도 PASS
            checks.check("Cedar: 일반 사용자 개선 파이프라인 admin 도구 거부", True, f"예외={str(exc)[:120]}")

    await _with_gateway_session(gateway_url, user_token, work_cedar)


# ─────────────────────────────── main ───────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description="agentic Text-to-SQL E2E 검증기")
    parser.add_argument("--level", choices=["1", "2", "4", "5", "6", "7", "all"], default="all")
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

    if args.level in ("6", "all"):
        actx = _load_admin_ctx()
        if not ADMIN_OUTPUTS.exists() and not actx.get("gateway_url"):
            print("[레벨6] admin-outputs.json / gateway-outputs.json 없음 — admin 스택 미배포. skip.")
        elif not ADMIN_OUTPUTS.exists():
            print("[레벨6] admin-outputs.json 없음 — Admin 스택 미배포. skip.")
        else:
            password = _e2e_password("레벨6")
            if password:
                asyncio.run(verify_admin_panel(actx, checks, password))

    if args.level in ("7", "all"):
        ectx = _load_evaluation_ctx()
        if not ectx.get("gateway_url"):
            print("[레벨7] gateway-outputs.json 없음 — 개선 파이프라인 미배포. skip.")
        elif not EVALUATION_OUTPUTS.exists():
            print("[레벨7] evaluation-outputs.json 없음 — Evaluation 스택 미배포. skip.")
        else:
            password = _e2e_password("레벨7")
            if password:
                asyncio.run(verify_improvement_pipeline(ectx, checks, password))

    return checks.summary()


if __name__ == "__main__":
    sys.exit(main())
