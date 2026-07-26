#!/usr/bin/env python3
"""E2E 검증기 — M1 완료 기준의 3레벨 스모크 테스트.

레벨 1 (MCP): SigV4 streamable-http 로 각 MCP Runtime 에 접속.
  - sql-execution-mcp: tools/list → run_sql("SELECT COUNT(*) FROM customers") == 1000,
                       run_sql("DELETE FROM customers") status == "rejected" (READ-ONLY 방어)
  - semantic-retrieval-mcp: search_schema("지역별 매출") → results 비어있지 않음
레벨 2 (에이전트): orchestrator Runtime 에 RunAgentInput POST(InvokeAgentRuntime) →
  SSE 이벤트에 RUN_STARTED → STEP/TOOL_CALL → TEXT_MESSAGE → RUN_FINISHED 확인.

실행:
  python scripts/e2e_verify.py --level 1        # MCP 레벨만
  python scripts/e2e_verify.py --level 2        # 에이전트 레벨만
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
    # AgentCore 세션 헤더(동일 microVM 라우팅). 33자 이상 권장.
    session_id = "e2e-session-000000000000000000000001"
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


# ─────────────────────────────── main ───────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description="agentic Text-to-SQL E2E 검증기")
    parser.add_argument("--level", choices=["1", "2", "all"], default="all")
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

    return checks.summary()


if __name__ == "__main__":
    sys.exit(main())
