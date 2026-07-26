"""
observability.py — AgentCore Observability를 보완하는 호출 단위 메트릭 기록.

AgentCore Runtime은 배포만 하면 ADOT(`opentelemetry-instrument`) 자동 계측으로
트레이스·스팬·로그를 CloudWatch GenAI Observability(Transaction Search)에 보냅니다.
다만 Claude Agent SDK는 모델 호출이 `claude` CLI 서브프로세스(Node) 안에서 일어나
토큰·비용 같은 LLM 수치는 자동 계측에 잡히지 않습니다.

이 모듈은 그 공백을 메웁니다:
  1) SDK가 스트림 마지막에 돌려주는 ResultMessage(토큰·비용·레이턴시·세션ID)를
     AgentCore 관측성 네임스페이스(`bedrock-agentcore`)의 CloudWatch 커스텀 메트릭
     `genai.*` 로 기록 — 이상탐지 알람(observability/setup_anomaly_alarms.py) 대상.
  2) 호출별 입력/출력 프롬프트 전문을 구조화 JSON 로그(GENAI_INVOCATION)로 기록 —
     런타임 로그 그룹에서 CloudWatch Logs Insights로 session_id별 조회 가능.
  3) OTel gen_ai 스팬 발행 — Strands 등이 자동으로 하는 것을 Claude Agent SDK에
     맞게 수동 발행. ADOT가 이미 설정한 전역 tracer를 재사용하므로 기존 트레이스
     (POST /invocations 하위)에 끼어 들어가고, GenAI Observability 콘솔의 트레이스
     상세에서 스팬을 클릭하면 프롬프트·응답·토큰이 속성으로 보입니다.

기록 실패는 에이전트 응답에 영향을 주지 않습니다 (로그만 남김).
"""
import json
import logging
import os
import time

log = logging.getLogger("observability")

# AgentCore Runtime 기본 실행 역할은 cloudwatch:PutMetricData 를
# `bedrock-agentcore` 네임스페이스로 제한하므로 기본값으로 그대로 사용합니다.
# 별도 네임스페이스를 쓰려면 실행 역할에 해당 네임스페이스 권한을 추가하세요.
METRICS_NAMESPACE = os.environ.get("GENAI_METRICS_NAMESPACE", "bedrock-agentcore")
AGENT_NAME = os.environ.get("GENAI_AGENT_NAME", "anycompany_ecommerce")
# 프롬프트/응답 원문 로깅 여부 — 기본 비활성(옵트인). 원문에는 고객 데이터·PII가
# 포함될 수 있으므로, 켤 때는(GENAI_LOG_PAYLOADS=1) 로그 보존 기간·접근 권한을
# 함께 관리하세요. 이 프로젝트의 deploy()는 실습을 위해 명시적으로 켭니다.
LOG_PAYLOADS = os.environ.get("GENAI_LOG_PAYLOADS", "0") == "1"

_cloudwatch = None


def _cw():
    global _cloudwatch
    if _cloudwatch is None:
        import boto3
        _cloudwatch = boto3.client(
            "cloudwatch", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return _cloudwatch


def observe_invocation(*, prompt: str, response: str, model: str,
                       latency_ms: int, usage: dict, cost_usd: float,
                       session_id: str | None = None, num_turns: int | None = None,
                       tools_used: list[str] | None = None, error: str | None = None):
    """호출 1건의 메트릭·gen_ai 스팬·페이로드 로그를 기록. 스트리밍 종료(finally) 시점에 호출."""
    tools_used = tools_used or []
    input_tokens = (usage.get("input_tokens", 0)
                    + usage.get("cache_creation_input_tokens", 0)
                    + usage.get("cache_read_input_tokens", 0))
    output_tokens = usage.get("output_tokens", 0)

    # GenAI Observability 콘솔의 트레이스 상세에서 입출력을 보여주는 gen_ai 스팬.
    try:
        from opentelemetry import trace as otel_trace
        end_ns = time.time_ns()
        span = otel_trace.get_tracer("observability").start_span(
            "claude_agent_sdk.invoke_agent",
            start_time=end_ns - int(latency_ms * 1_000_000),
            attributes={
                "gen_ai.system": "claude-agent-sdk",
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.name": AGENT_NAME,
                "gen_ai.request.model": model,
                "gen_ai.usage.input_tokens": input_tokens,
                "gen_ai.usage.output_tokens": output_tokens,
                "gen_ai.conversation.id": session_id or "",
                "gen_ai.tool.names": ",".join(tools_used),
                "cost_usd": cost_usd,
            })
        if LOG_PAYLOADS:
            # 컨테이너에 OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true 가
            # 있어야 ADOT가 content 속성을 제거하지 않고 내보냅니다 (deploy()가 주입).
            span.set_attribute("gen_ai.input.messages", json.dumps(
                [{"role": "user", "parts": [{"type": "text", "content": prompt}]}],
                ensure_ascii=False))
            span.set_attribute("gen_ai.output.messages", json.dumps(
                [{"role": "assistant", "parts": [{"type": "text", "content": response}]}],
                ensure_ascii=False))
        if error:
            span.set_status(otel_trace.StatusCode.ERROR, error)
        span.end(end_time=end_ns)
    except Exception as e:  # noqa: BLE001
        log.warning("gen_ai 스팬 발행 실패: %s", e)

    dims = [{"Name": "Agent", "Value": AGENT_NAME}]
    data = [
        {"MetricName": "genai.invocation.count", "Value": 1, "Unit": "Count"},
        {"MetricName": "genai.invocation.latency", "Value": latency_ms, "Unit": "Milliseconds"},
        {"MetricName": "genai.token.input", "Value": input_tokens, "Unit": "Count"},
        {"MetricName": "genai.token.output", "Value": output_tokens, "Unit": "Count"},
        {"MetricName": "genai.cost.usd", "Value": cost_usd, "Unit": "None"},
        {"MetricName": "genai.tool.calls", "Value": len(tools_used), "Unit": "Count"},
    ]
    if error:
        data.append({"MetricName": "genai.error.count", "Value": 1, "Unit": "Count"})
    for d in data:
        d["Dimensions"] = dims
    try:
        _cw().put_metric_data(Namespace=METRICS_NAMESPACE, MetricData=data)
    except Exception as e:  # noqa: BLE001
        log.warning("CloudWatch put_metric_data 실패: %s", e)

    # 호출별 입력/출력 전문 — 런타임 로그 그룹의 [runtime-logs] 스트림으로 수집됨.
    # Logs Insights 예: filter log_type = "GENAI_INVOCATION" | fields session_id,
    #                   request_payload, response_payload, cost_usd
    if LOG_PAYLOADS:
        try:
            print(json.dumps({
                "log_type": "GENAI_INVOCATION",
                "agent": AGENT_NAME,
                "session_id": session_id,
                "model": model,
                "latency_ms": latency_ms,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost_usd,
                "num_turns": num_turns,
                "tools_used": tools_used,
                "error": error,
                "request_payload": prompt,
                "response_payload": response,
            }, ensure_ascii=False), flush=True)
        except Exception as e:  # noqa: BLE001
            log.warning("페이로드 로그 기록 실패: %s", e)
