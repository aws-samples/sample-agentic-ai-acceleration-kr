# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Live invoke client — 배포된 AgentCore runtime 을 호출해 이벤트 스트림 수집.

admin-api 의 chat_agent.py `_agentcore_stream` 과 동일한 계약으로 호출한다:
  boto3 bedrock-agentcore.invoke_agent_runtime
    → response['response'] (StreamingBody)
    → iter_lines() 로 `data: <json>` 프레임 파싱
    → 각 프레임을 dict 이벤트로 yield

이 함수가 반환하는 event dict 리스트를 tests/eval/scoring.py 가 채점한다.

활성화 (둘 다 필요):
  GOLDEN_LIVE=1
  AGENTCORE_RUNTIME_ARN=arn:aws:bedrock-agentcore:...:runtime/llm_gateway_dev_admin_chat_agent-...
선택:
  AGENTCORE_REGION  (default ap-northeast-2)
  GOLDEN_TIMEOUT    (case 당 최대 초, default 180)

라이브 호출은 실제 Bedrock 비용을 발생시킨다(case 당 ~$0.2 수준). CI 의 기본
경로(static)는 이 모듈을 부르지 않는다.
"""

from __future__ import annotations

import json
import os
import time

DEFAULT_REGION = os.environ.get("AGENTCORE_REGION", "ap-northeast-2")
DEFAULT_TIMEOUT = float(os.environ.get("GOLDEN_TIMEOUT", "180"))

_AGENTCORE_SESSION_MIN = 33


def is_live_enabled() -> bool:
    return os.environ.get("GOLDEN_LIVE") == "1" and bool(
        os.environ.get("AGENTCORE_RUNTIME_ARN")
    )


def _session_id(raw: str) -> str:
    """AgentCore runtimeSessionId 최소 33자 요구 — chat_agent.py 와 동일 패딩."""
    if len(raw) >= _AGENTCORE_SESSION_MIN:
        return raw
    return (raw + "-" + "0" * _AGENTCORE_SESSION_MIN)[:_AGENTCORE_SESSION_MIN]


def invoke_agent(
    question: str,
    *,
    runtime_arn: str | None = None,
    region: str | None = None,
    session_id: str = "golden-eval-session-000000000000",
    timeout: float | None = None,
    mode: str = "quick",
) -> list[dict]:
    """배포된 agent 를 1회 호출하고 모든 SSE 이벤트를 list[dict] 로 수집.

    chat_agent.py `_agentcore_stream` 의 라인 파싱과 동일하게 `data:` 프레임만
    추출해 json.loads 한다. agent 가 yield 하는 raw 이벤트(type: thinking/
    tool_call/tool_result/text/chart/validator/done)를 그대로 담는다.
    """
    import boto3
    from botocore.config import Config

    arn = runtime_arn or os.environ["AGENTCORE_RUNTIME_ARN"]
    region = region or DEFAULT_REGION
    timeout = timeout or DEFAULT_TIMEOUT

    # botocore 의 소켓 read timeout 은 GOLDEN_TIMEOUT 과 별개(기본 60s) — Code
    # Specialist sandbox 분석이 길면 heartbeat(10s) 사이 침묵이 60s 를 넘을 수
    # 있어 끊긴다. read_timeout 을 timeout 에 맞추고 자동 retry 비활성(스트림 중복 방지).
    client = boto3.client(
        "bedrock-agentcore",
        region_name=region,
        config=Config(read_timeout=timeout, connect_timeout=15, retries={"max_attempts": 0}),
    )
    payload = {"content": question, "session_id": session_id, "mode": mode}

    response = client.invoke_agent_runtime(
        agentRuntimeArn=arn,
        runtimeSessionId=_session_id(session_id),
        payload=json.dumps(payload).encode(),
    )
    body = response.get("response") or response.get("payload")

    events: list[dict] = []
    deadline = time.monotonic() + timeout

    if hasattr(body, "iter_lines"):
        for line in body.iter_lines():
            if time.monotonic() > deadline:
                events.append(
                    {"type": "error", "error": f"timeout after {timeout}s"}
                )
                break
            if not line:
                continue
            text_line = (
                line.decode() if isinstance(line, (bytes, bytearray)) else line
            )
            if not text_line.startswith("data:"):
                continue
            data_str = text_line[len("data:") :].strip()
            if data_str in ("", "[DONE]"):
                continue
            try:
                evt = json.loads(data_str)
            except (ValueError, TypeError):
                evt = {"type": "text", "chunk": data_str}
            events.append(evt)
            if isinstance(evt, dict) and evt.get("type") == "done":
                break
    else:
        # 비스트리밍 단일 JSON fallback
        raw = body.read().decode() if hasattr(body, "read") else json.dumps(
            response, default=str
        )
        try:
            obj = json.loads(raw)
            reply = obj.get("reply", raw) if isinstance(obj, dict) else raw
        except (ValueError, TypeError):
            reply = raw
        events.append({"type": "text", "chunk": reply})

    return events
