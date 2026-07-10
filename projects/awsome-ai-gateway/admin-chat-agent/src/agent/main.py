"""admin-chat-agent — 5-agent BI assistant.

docs/admin-chat-agent-spec.md §4.2 의 Pattern C+ (Orchestrator + 4
Specialists) 구현.

agents-as-tools 패턴: Orchestrator 가 ask_sql_specialist /
ask_code_specialist / ask_validator / ask_viz_specialist 를 tool 처럼
호출. 각 sub-agent 는 자신의 system prompt + tool 사용.

Phase 3 단계: 코드 구조 + system prompts + tool wiring 완성. 실제
deploy/검증은 image build → ECR push → CreateAgentRuntime 후.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import re
import time
from pathlib import Path

import structlog
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent, tool
from strands.models import BedrockModel, CacheConfig

from agent import candidate_select
from agent import fewshot
from agent import sql_struct
from agent.envelopes import ENVELOPE_MODELS

logger = structlog.get_logger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"

# 현재 invoke() 턴의 컨텍스트 stash (agents-as-tools @tool 은 blocking 이라
# stream 으로 결과가 안 나옴 → 여기 모아두고 generator 가 chart/tool_result
# 이벤트로 발행).
#  - _tool_results: sub-agent envelope (SQL/Code/Validator/Viz)
#  - _chart_specs : render_chart tool 이 만든 차트 spec (텍스트엔 안 나오므로
#                   별도 stash 해야 chart 이벤트로 발행됨 — 누락 방지)
_tool_results: contextvars.ContextVar[list] = contextvars.ContextVar(
    "_tool_results", default=None
)
_chart_specs: contextvars.ContextVar[list] = contextvars.ContextVar(
    "_chart_specs", default=None
)
#  - _verifications: L3 후보선택 검증 메타(§58) — deep 모드에서 verification
#    이벤트로 발행해 검증 타임라인을 노출(quick 은 stash 만, 발행 안 함).
#  - _mode / _session_id: L3 가 k(후보 수)·실행 라우팅에 쓰는 현재 invoke 컨텍스트.
_verifications: contextvars.ContextVar[list] = contextvars.ContextVar(
    "_verifications", default=None
)
_mode: contextvars.ContextVar[str] = contextvars.ContextVar("_mode", default="quick")
_session_id: contextvars.ContextVar[str] = contextvars.ContextVar("_session_id", default="")


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Models — global.* cross-region inference profile (ap-northeast-2)
#
# 5-agent 전체를 Opus 4.8 로 통일(text2sql·코드 생성 정확도 우선). per-tier env
# 로 개별 override 가능 → 정확도가 안 오르면 해당 tier 만 4.7/sonnet 으로 롤백
# (재빌드 없이 env 만, §32.4 주의: update-agent-runtime 은 env 보존 안 함).
# ─────────────────────────────────────────────────────────────────────────────
MODEL_OPUS = os.environ.get("MODEL_OPUS", "global.anthropic.claude-opus-4-8")
# SQL/Code/Viz specialist 모델 — 기본 Opus 4.8. 정확도 A/B 측정을 위해 tier 별
# 독립 override(MODEL_SQL/MODEL_CODE/MODEL_VIZ). 미설정 시 MODEL_OPUS 로 폴백.
MODEL_SQL = os.environ.get("MODEL_SQL", MODEL_OPUS)
MODEL_CODE = os.environ.get("MODEL_CODE", MODEL_OPUS)
MODEL_VIZ = os.environ.get("MODEL_VIZ", MODEL_OPUS)
# Report Specialist 모델 — 기본 MODEL_CODE(파일 생성 코드는 보일러플레이트라
# 지능 민감도 낮음). env 로 sonnet/haiku 다운시프트해 비용·latency 절감 가능.
MODEL_REPORT = os.environ.get("MODEL_REPORT", MODEL_CODE)
# 리포트 파일 S3 업로드 버킷(staging 재사용, reports/ prefix). execute_python
# 샌드박스가 boto3 로 직접 업로드하므로 코드에 버킷명을 주입해야 한다.
CHAT_STAGING_BUCKET = os.environ.get("CHAT_STAGING_BUCKET", "")
AWS_REGION = os.environ.get("AWS_REGION", "ap-northeast-2")

# orchestrator effort — 첫 침묵(tool-선택 추론 latency, §45.1) 직접 단축 레버.
# Opus 4.8 effort 레벨: low < medium < high(기본) < xhigh < max. high/xhigh/max 는
# 거의 항상 깊이 사고, low/medium 은 쉬운 문제서 사고를 건너뛰어 첫토큰이 빨라진다.
# 단 effort 는 tool 호출 수도 줄여(통합) 5-agent 위임 정확도에 영향 가능 → golden
# A/B 로만 낮춘다. env 로 재빌드 없이 A/B(§40 MODEL_SQL 패턴과 동일). 빈/"high"/
# 미지정 = 파라미터 생략(=high, 현행 동작 무변). specialist 는 영향 안 줌(orch 전용).
ORCH_EFFORT = os.environ.get("ORCH_EFFORT", "").strip().lower()
_VALID_EFFORTS = {"low", "medium", "high", "xhigh", "max"}

# L4 cross-family critic(§59) — diverse-lens 역번역 검증. 모델 무관 설계:
# MODEL_CRITIC 으로 Claude/GPT 스위칭(비교실험 후 결정). 기본 OFF(회귀 0).
#   CRITIC_ENABLED: "1" 이면 deep+고위험 질의에 역번역 critic 추가(약한 게이트).
#   MODEL_CRITIC: "claude"(Phase1 배선검증, 같은 패밀리) | "gpt"(GPT-5.5, 다른 패밀리).
#   MODEL_CRITIC_ID: GPT 모델 ID(Bedrock Mantle Responses API). 기본 openai.gpt-5.5.
#   MANTLE_REGION: GPT(Mantle) 호출 리전. GPT-5.5 는 us-east-2(오하이오) — 검증됨
#     (AgentCore_only §10b). 우리 홈리전(ap-northeast-2)엔 없어 cross-region 호출.
#   CRITIC_TIMEOUT_S: critic 호출 hard timeout(기본 8s — GPT 추론모델 TTFT 고려, 본경로보다 짧게).
CRITIC_ENABLED = os.environ.get("CRITIC_ENABLED", "").strip() == "1"
MODEL_CRITIC = os.environ.get("MODEL_CRITIC", "claude").strip().lower()
MODEL_CRITIC_ID = os.environ.get("MODEL_CRITIC_ID", "openai.gpt-5.5").strip()
MANTLE_REGION = os.environ.get("MANTLE_REGION", "us-east-2").strip()
CRITIC_TIMEOUT_S = float(os.environ.get("CRITIC_TIMEOUT_S", "8"))

# L5 answer auditor(§60) — 최종 답변 산문의 수치가 실행 결과(SQL/Code envelope)에서
# 유래했는지 보는 독립·회의적 감사. SQL 구조/의미 검증(L2)·역번역(L4) 끝난 뒤 FINALIZED
# PROSE 만 본다. 약한 게이트(차단 X, advisory 카드만), fail-soft(타임아웃/예외 무영향),
# deep+고위험 selective. 기본 OFF(회귀 0). MODEL_AUDITOR 로 claude/gpt 스위칭(critic 인프라 재사용).
AUDITOR_ENABLED = os.environ.get("AUDITOR_ENABLED", "").strip() == "1"
MODEL_AUDITOR = os.environ.get("MODEL_AUDITOR", "claude").strip().lower()
AUDITOR_TIMEOUT_S = float(os.environ.get("AUDITOR_TIMEOUT_S", "10"))

# L3 실행기반 후보선택(§58) — 후보 수 k. quick 은 즉답성 위해 작게, deep 은
# 정확도 우선 크게. env 로 재빌드 없이 A/B. k=1 이면 단일 생성(기존 동작과 동일).
SELFCONSISTENCY_K_QUICK = int(os.environ.get("SELFCONSISTENCY_K_QUICK", "3"))
SELFCONSISTENCY_K_DEEP = int(os.environ.get("SELFCONSISTENCY_K_DEEP", "5"))

# heartbeat 진행 라벨 — invoke() 의 heartbeat task 가 "지금 무엇 중"을 합성할 때
# in-flight tool 이름(current_tool_use)을 사람이 읽는 단계명으로 변환. 침묵 구간
# (sub-agent blocking)에 흐르는 생존신호의 detail. 미지의 tool 은 기본 라벨.
_HEARTBEAT_PHASE = {
    "ask_sql_specialist": ("sql", "데이터 조회·SQL 생성 중"),
    "ask_code_specialist": ("analyze", "Python 분석 실행 중"),
    "ask_validator": ("validate", "결과 검증 중"),
    "ask_viz_specialist": ("viz", "차트 구성 중"),
    "ask_report_specialist": ("report", "리포트 파일 생성 중"),
    "render_chart": ("viz", "차트 렌더링 중"),
    "get_schema": ("sql", "스키마 확인 중"),
}
# heartbeat 주기(초). 첫 tick 은 더 빨리(2s) 보내 "살아있음"을 빠르게 알리고,
# 이후 INTERVAL 간격. sub-agent 한 번이 20~60초라 5s 면 그 안에 4~12프레임.
_HEARTBEAT_FIRST = 2.0
_HEARTBEAT_INTERVAL = 5.0


def _bedrock(
    model_id: str, *, stream_thinking: bool = False, effort: str | None = None
) -> BedrockModel:
    """Opus 4.8/4.7 호환 BedrockModel.

    temperature/top_p/top_k 는 절대 넘기지 않는다 — Opus 4.8/4.7 에서 제거되어
    포함 시 Bedrock 400. thinking.type 은 "adaptive" 만 허용.

    stream_thinking=True 면 `display:"summarized"` 를 켜 추론 요약이 응답 텍스트
    델타로 실시간 흐른다(기본 omitted → 추론 텍스트 비어서 안 흐름). orchestrator
    에만 켜서 첫 위임 전 침묵 구간을 "지금 무엇을 분석 중" 으로 메운다.
    specialist 는 omitted 유지(내부 추론까지 흘리면 잡음).

    effort 를 주면 `output_config.effort`(thinking 과 **top-level 형제**, Bedrock
    실측 확정 — thinking.effort 는 ValidationException)로 추론 깊이/토큰을 조절한다.
    None/미지정 = 파라미터 생략 = 기본 high(현행 동작과 동일).

    프롬프트 캐싱(§54): cache_config="auto" + cache_tools 로 시스템프롬프트·tool
    정의·마지막 user 턴에 cachePoint 주입(Strands bedrock.py). orchestrator 는
    매 호출 system(4.8KB)+tools(5개 스키마)가 동일하므로 cache hit 시 TTFT 의
    prefill 비용이 절감된다(Bedrock Claude 프롬프트 캐시, 5m TTL).
    """
    thinking: dict = {"type": "adaptive"}
    if stream_thinking:
        thinking["display"] = "summarized"
    request_fields: dict = {"thinking": thinking}
    # high 는 기본값이라 생략(요청 바이트 최소화 = 프롬프트 캐시/현행 동작 보존).
    if effort and effort in _VALID_EFFORTS and effort != "high":
        request_fields["output_config"] = {"effort": effort}
    return BedrockModel(
        model_id=model_id,
        region_name=AWS_REGION,
        additional_request_fields=request_fields,
        cache_config=CacheConfig(strategy="auto"),
        cache_tools="default",
    )


def opus_model() -> BedrockModel:
    return _bedrock(MODEL_OPUS)


def orchestrator_model() -> BedrockModel:
    """orchestrator 전용 — 추론 요약 실시간 스트리밍 + effort 조절(레이턴시 레버)."""
    return _bedrock(MODEL_OPUS, stream_thinking=True, effort=ORCH_EFFORT or None)


def _gpt_critic_call(payload: str) -> str:
    """GPT-5.5 cross-family critic 호출 — Bedrock Mantle(us-east-2) Responses API.

    AgentCore_only(agents/gpt55_review.py)에서 검증된 경로: openai SDK 의
    BedrockOpenAI + aws_bedrock_token_generator.provide_token(SigV4 기반 단기 bearer
    자동발급). gpt-5.x 는 chat/completions 가 아니라 **Responses API**(responses.create).
    payload(JSON 문자열) 를 system 프롬프트와 합쳐 input 으로 보내고 output_text 반환.

    미설치(ImportError)/미인증/500/빈스트림 등은 raise — 상위(_cross_family_check)가
    try/except + timeout 으로 흡수(fail-soft, 본 검증 verdict 불변).
    """
    from openai import BedrockOpenAI
    from aws_bedrock_token_generator import provide_token

    client = BedrockOpenAI(
        aws_region=MANTLE_REGION,
        bedrock_token_provider=lambda: provide_token(region=MANTLE_REGION),
    )
    system = load_prompt("sql_critic")
    resp = client.responses.create(
        model=MODEL_CRITIC_ID,
        input=f"{system}\n\n[입력]\n{payload}",
        stream=False,
    )
    text = getattr(resp, "output_text", None)
    if not text:
        # output_text 비면 output 항목에서 text 수집(reasoning 제외 message 만).
        parts = []
        for item in getattr(resp, "output", None) or []:
            for c in getattr(item, "content", []) or []:
                t = getattr(c, "text", None)
                if t:
                    parts.append(t)
        text = "".join(parts)
    if not text:
        raise RuntimeError("critic 빈 응답(빈 스트림)")
    return text


def _claude_critic_call(payload: str) -> str:
    """Claude cross-family critic 호출 — Phase 1 배선/메커니즘 검증용(가족 다양성
    없음, 같은 Anthropic). sql_critic 프롬프트로 Opus 호출."""
    global _claude_critic_agent
    if _claude_critic_agent is None:
        _claude_critic_agent = Agent(
            model=_bedrock(MODEL_OPUS), tools=[], system_prompt=load_prompt("sql_critic"),
        )
    return str(_claude_critic_agent(payload))


_claude_critic_agent = None


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic tools — AgentCore Gateway → Lambda / admin-api
# ─────────────────────────────────────────────────────────────────────────────
@tool
def get_schema(table_name: str | None = None) -> dict:
    """Read the allowed schema metadata. Returns whitelist info.

    If table_name is None, returns list of all allowed tables.
    Otherwise returns columns of the specified table.
    """
    # Phase 3: AgentCore Gateway 로 Lambda 호출. MVP 는 직접 Lambda invoke.
    import boto3

    client = boto3.client("lambda")
    fn = os.environ.get("LAMBDA_GET_SCHEMA", "llm-gateway-prod-chat-agent-get-schema")
    response = client.invoke(
        FunctionName=fn,
        Payload=json.dumps({"table_name": table_name}).encode(),
    )
    return json.loads(response["Payload"].read())


@tool
def query_db(sql: str, session_id: str = "", step_id: str = "") -> dict:
    """Execute a read-only SELECT against the gateway DB.

    Validates with sqlglot AST + EXPLAIN + LIMIT, executes as
    gateway_chat_reader role with statement_timeout=10s. Stages large
    results to S3 for the Code Specialist.
    """
    import boto3

    client = boto3.client("lambda")
    fn = os.environ.get("LAMBDA_QUERY_DB", "llm-gateway-prod-chat-agent-query-db")
    response = client.invoke(
        FunctionName=fn,
        Payload=json.dumps(
            {"sql": sql, "session_id": session_id, "step_id": step_id}
        ).encode(),
    )
    return json.loads(response["Payload"].read())


@tool
def render_chart(
    kind: str,
    data: list[dict],
    x: str,
    y: str | list[str],
    color: str | None = None,
    title: str | None = None,
) -> dict:
    """Build a chart spec for admin-ui's recharts renderer.

    kind ∈ {bar, line, pie, area, table, kpi, image}.
    """
    encoding = {"x": x, "y": y}
    if color:
        encoding["color"] = color
    spec = {"kind": kind, "data": data, "encoding": encoding, "title": title}
    # invoke() 가 chart 이벤트로 발행하도록 stash. (orchestrator 가 render_chart
    # tool 을 부르면 spec 이 최종 텍스트에 안 나와 _extract_chart_specs 로는
    # 못 잡음 → 여기 stash 해야 차트가 누락되지 않는다.)
    bucket = _chart_specs.get()
    if bucket is not None:
        bucket.append(spec)
    return spec


@tool
def execute_python(code: str) -> dict:
    """Run Python in AgentCore Code Interpreter sandbox.

    pre-installed: pandas, numpy, scipy, sklearn, statsmodels, matplotlib,
    seaborn, reportlab, python-pptx, openpyxl, boto3.

    ⚠️ S3 쓰기는 **커스텀 인터프리터**(CODE_INTERPRETER_ID)에서만 가능(§49). 기본
    인터프리터는 boto3 자격증명이 없어 put_object 가 "Unable to locate credentials"
    로 실패한다. CODE_INTERPRETER_ID 가 설정되면 execution role 을 주입한 커스텀
    인터프리터를 써서 샌드박스가 직접 S3(staging/·reports/)에 쓸 수 있다. 미설정
    시 기본 인터프리터 폴백(S3 쓰기 불가 — report/PNG 업로드 안 됨).
    """
    from bedrock_agentcore.tools.code_interpreter_client import code_session

    region = os.environ.get("AWS_REGION", "ap-northeast-2")
    interp_id = os.environ.get("CODE_INTERPRETER_ID", "").strip()
    cm = (
        code_session(region, identifier=interp_id)
        if interp_id
        else code_session(region)
    )
    with cm as cc:
        result = cc.invoke(
            "executeCode",
            {"code": code, "language": "python", "clearContext": False},
        )
        # Strands 가 stream 형태로 반환. 마지막 result 만 추출
        for event in result.get("stream", []):
            if "result" in event:
                return event["result"]
        return {"stdout": "", "stderr": "no result"}


# ─────────────────────────────────────────────────────────────────────────────
# Sub-agents (Specialists)
# ─────────────────────────────────────────────────────────────────────────────
sql_specialist = Agent(
    model=_bedrock(MODEL_SQL),
    tools=[get_schema, query_db],
    system_prompt=load_prompt("sql_specialist"),
)

code_specialist = Agent(
    model=_bedrock(MODEL_CODE),
    tools=[execute_python],
    system_prompt=load_prompt("code_specialist"),
)

# Validator / Viz 는 tool 없이 LLM 만 (분석 결과 → JSON 응답)
sql_validator = Agent(
    model=opus_model(),
    tools=[],
    system_prompt=load_prompt("sql_validator"),
)

viz_specialist = Agent(
    model=_bedrock(MODEL_VIZ),
    tools=[],
    system_prompt=load_prompt("viz_specialist"),
)

# Report Specialist — 여러 SQL 결과를 모아 다운로드 가능한 리포트 파일(PDF/PPTX/
# XLSX)을 Code Interpreter 샌드박스에서 생성(reportlab/python-pptx/openpyxl 사전
# 설치 확인됨)하고 S3(reports/ prefix)에 업로드. get_schema/query_db 로 데이터
# 수집, execute_python 으로 파일 생성. 모델은 MODEL_REPORT(기본 MODEL_CODE) —
# 파일 생성 코드는 보일러플레이트라 지능 민감도 낮음(필요시 env 로 다운시프트).
report_specialist = Agent(
    model=_bedrock(MODEL_REPORT),
    tools=[get_schema, query_db, execute_python],
    system_prompt=load_prompt("report_specialist"),
)


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator's agents-as-tools wrappers
# ─────────────────────────────────────────────────────────────────────────────
@tool
def ask_sql_specialist(question: str, hints: dict | None = None) -> dict:
    """Delegate text-to-SQL generation + execution to the SQL Specialist.

    Returns the STRUCTURED envelope {ok, sql, rows, row_count, columns, s3_uri?,
    explain_cost?, note?} — NOT prose. orchestrator 는 이 envelope 의 필드를
    핸들로 참조해야 하며, 숫자를 산문에서 재작성/추론하면 안 된다(harness:
    deterministic-tool-first + structured output).

    DAIL-SQL few-shot: 질문과 유사한 검증된 (question, SQL) 예시를 payload 에
    주입해 스키마 사용법(KST/컬럼명/집계 idiom)을 in-context 로 가르친다. 뱅크는
    golden 평가 질문과 겹치지 않고, LOO 가드로 동일 질문은 제외(오염 방지).
    """
    fewshot_block = fewshot.build_fewshot_block(question)
    payload = json.dumps(
        {"question": question, "hints": hints or {}, "few_shot": fewshot_block}
    )
    env = _agent_call(sql_specialist, payload, parse_json=True, tool_name="ask_sql_specialist")
    # §58 결함⑨: SQL 이 나오면 **항상** validator 를 코드로 실행(비결정성 0).
    # orchestrator 가 ask_validator 를 부를지 말지에 의존하지 않는다.
    _auto_validate(question, env)
    # §59 L4: deep+고위험이면 cross-family 역번역 critic 추가(약한 게이트·fail-soft).
    _cross_family_check(question, env, _mode.get())
    return env


# 후보 다양화 hint(§58 L3) — CHASE-SQL '서로 다른 추론 경로'를 디코딩 제어 없이
# 프롬프트 변주로 흉내. 같은 질문을 다른 각도로 풀게 해 공통 실수 외의 오답을
# 흩뜨린다(정답은 어느 경로로도 같은 결과셋에 수렴).
_CANDIDATE_STRATEGIES = (
    {"strategy": "직접 작성 — 가장 단순하고 명확한 SQL."},
    {"strategy": "divide-and-conquer — 하위 조건(시간/필터/집계)을 단계로 분해 후 합성. 1:N JOIN 은 서브쿼리 선집계로 fan-out 회피."},
    {"strategy": "query-plan — 실행 순서(FROM·JOIN→WHERE→GROUP BY→집계)를 먼저 따져 조인 카디널리티와 집계 입도를 검증하며 작성."},
    {"strategy": "정합성 우선 — KST 앵커(AT TIME ZONE 'Asia/Seoul')와 status 의미를 명시적으로 적용하고, 팀 귀속은 usage_logs.team_id 직접 사용."},
    {"strategy": "보수적 — 모호하면 좁은 해석 대신 명시적 필터를 달고 COUNT(DISTINCT request_id)로 중복 카운트 방지."},
)


@tool
def ask_sql_verified(question: str, hints: dict | None = None) -> dict:
    """text-to-SQL with **execution-based self-consistency** (§58 L3, SOTA core).

    스칼라/집계 질의의 정확도를 높이기 위해 k 개 후보 SQL 을 서로 다른 전략으로
    생성→각각 실제 실행→결과셋이 같은 후보끼리 클러스터링→최대 다수파를 채택한다
    (CHASE-SQL/MBR-Exec). '틀린 SQL'(fan-out N배·필터누락 등)은 다른 후보와 결과가
    갈려 소수파로 탈락하므로, reconcile 이 못 잡던 의미 오류를 결과셋 합의로 거른다.

    반환: 채택된 후보의 SQL envelope + `verification` 메타(합의도·후보수·클러스터).
    오케스트레이터는 이 envelope 를 ask_sql_specialist 와 동일하게 쓰되, 합의가
    낮으면(여러 결과가 갈림) 신뢰도 주의로 다룬다. quick=k3 / deep=k5 (env 조절).

    단일 후보(k=1)면 ask_sql_specialist 와 동일 — 점진 도입/폴백 안전.
    """
    mode = _mode.get()
    k = SELFCONSISTENCY_K_DEEP if mode == "deep" else SELFCONSISTENCY_K_QUICK
    k = max(1, min(k, len(_CANDIDATE_STRATEGIES)))
    fewshot_block = fewshot.build_fewshot_block(question)

    candidates: list[candidate_select.Candidate] = []
    for i in range(k):
        strat = _CANDIDATE_STRATEGIES[i]
        merged_hints = {**(hints or {}), **strat}
        payload = json.dumps(
            {"question": question, "hints": merged_hints, "few_shot": fewshot_block}
        )
        # 후보는 내부 시도 — SQL 스키마로 파싱하되 stash 안 함(채택 후보만 stash).
        env = _agent_call(
            sql_specialist, payload, parse_json=True,
            schema_key="ask_sql_specialist", stash=False,
        )
        sql_text = str(env.get("sql", "")) if isinstance(env, dict) else ""
        candidates.append(candidate_select.Candidate(sql_text, env if isinstance(env, dict) else {}))

    sel = candidate_select.select_by_execution(candidates)

    # 채택 후보 결정 — winner 없으면(전부 실패) 첫 후보 envelope 반환(폴백).
    win_idx = sel.get("winner_index")
    chosen = candidates[win_idx] if win_idx is not None else (candidates[0] if candidates else None)
    result = dict(chosen.envelope) if chosen else {"ok": False, "error": "no candidates"}

    # 검증 메타 동봉 — verdict 도출(합의도 기반 결정적 신호)
    n_valid = sel.get("n_valid", 0)
    agreement = sel.get("agreement", 0.0)
    tie = sel.get("tie", False)
    if win_idx is None:
        v_verdict = "FAIL"  # 후보 전부 실행 실패
    elif tie or (n_valid >= 2 and agreement < 0.5):
        v_verdict = "WARN"  # 결과가 갈림 — 신뢰도 주의
    else:
        v_verdict = "PASS"
    verification = {
        "method": "execution_self_consistency",
        "k": k,
        "n_valid": n_valid,
        "agreement": agreement,
        "n_clusters": sel.get("n_clusters", 0),
        "tie": tie,
        "verdict": v_verdict,
        "chosen_sql": (chosen.sql if chosen else ""),
    }
    result["verification"] = verification

    # 단일 stash(채택 후보만) — tool_result 이벤트로 1회 발행(ask_sql_specialist 동일).
    bucket = _tool_results.get()
    if bucket is not None:
        bucket.append({"tool": "ask_sql_specialist", "result": result})
    # §58 결함⑨: 채택 후보도 **항상** validator 코드 실행(후보투표≠의미검증 — 별개).
    _auto_validate(question, result)
    # §59 L4: deep+고위험이면 다른 패밀리(역번역 렌즈) 2차 의견 추가(약한 게이트·fail-soft).
    _cross_family_check(question, result, mode)
    # deep 모드: 검증 타임라인 노출용 stash. quick 은 미발행(즉답성).
    vbucket = _verifications.get()
    if vbucket is not None and mode == "deep":
        vbucket.append(verification)
    return result


@tool
def ask_code_specialist(intent: str, data_ref: str, hints: dict | None = None) -> dict:
    """Delegate Python-based analysis (outliers/timeseries/ML) to the Code Specialist.

    data_ref: S3 URI from a previous SQL Specialist call.
    Returns the STRUCTURED envelope {result_summary, data?, chart_s3_url?,
    csv_s3_url?, code} — 실행된 Python 코드(code)와 결정적 결과(data)를 담는다.
    orchestrator 는 data 의 값을 핸들로 인용한다(산문 재계산 금지).
    """
    payload = json.dumps(
        {"intent": intent, "data_ref": data_ref, "hints": hints or {}}
    )
    return _agent_call(code_specialist, payload, parse_json=True, tool_name="ask_code_specialist")


def _run_validator(
    user_question: str,
    generated_sql: str,
    sample_rows: list[dict],
    schema_used: list[str],
    row_count: int,
    accuracy_warnings: list[str] | None = None,
    *,
    stash: bool = True,
) -> dict:
    """validator 호출 코어(§58 L2). ask_validator(tool)·자동검증이 공유.

    sqlglot 구조 사실(L2) + L0/L1 accuracy_warnings 를 주입해 PASS 편향 억제.
    stash=True 면 _tool_results 에 ask_validator 로 기록(경로/이벤트에 검증 노출).
    """
    facts = sql_struct.extract_facts(generated_sql)
    payload = json.dumps(
        {
            "user_question": user_question,
            "generated_sql": generated_sql,
            "sample_rows": sample_rows,
            "schema_used": schema_used,
            "row_count": row_count,
            "sql_structure": sql_struct.facts_to_prompt(facts),
            "accuracy_warnings": accuracy_warnings or [],
        }
    )
    return _agent_call(
        sql_validator, payload, parse_json=True,
        tool_name="ask_validator", stash=stash,
    )


def _auto_validate(question: str, env: dict) -> None:
    """SQL envelope 에 대해 validator 를 **코드로 항상** 실행하고 verdict 를
    envelope 에 박는다(§58 결함⑨ 근본 해소).

    orchestrator(LLM)가 ask_validator 를 부를지 말지에 의존하지 않는다 — SQL 이
    생성되면 무조건 검증. 비결정성 0. verdict 는 env['validation'] 에 담기고
    _tool_results 에 ask_validator 로 stash 돼 경로/스트리밍에 노출된다.
    실패해도 본 경로를 막지 않음(graceful — 검증 호출 자체 오류는 WARN 으로).

    ⚠️ `ok` 필드에 의존하지 않는다 — structured output(SqlEnvelope)엔 ok 가 없다.
    'sql 텍스트가 있고 명시적 에러가 아니면' 검증 대상(§58 결함⑨ 라이브 버그 수정).
    """
    if not isinstance(env, dict):
        return
    sql_text = str(env.get("sql") or "").strip()
    if not sql_text:  # SQL 미생성(파싱 실패/에러 envelope) → 검증 대상 아님
        return
    if env.get("ok") is False or env.get("error"):  # 명시적 실패
        return
    if env.get("validation") is not None:  # 이미 검증됨(중복 방지)
        return
    try:
        verdict = _run_validator(
            user_question=question,
            generated_sql=str(env.get("sql", "")),
            sample_rows=env.get("rows") or [],
            schema_used=[str(c.get("name", "")) for c in (env.get("columns") or []) if isinstance(c, dict)],
            row_count=int(env.get("row_count", 0) or 0),
            accuracy_warnings=env.get("accuracy_warnings") or [],
            stash=True,
        )
        env["validation"] = verdict
    except Exception as exc:  # noqa: BLE001 — 검증 호출 실패가 답변을 막지 않게
        logger.warning("auto_validate_failed", error=str(exc))
        env["validation"] = {
            "verdict": "WARN",
            "reason": "자동 검증 호출이 실패했습니다. 숫자 정확도를 재확인하세요.",
            "confidence": 0.5,
        }


def _get_critic_call():
    """현재 MODEL_CRITIC 에 맞는 critic 호출 함수(payload→text) 반환.
    gpt → _gpt_critic_call(BedrockOpenAI/Mantle), claude → _claude_critic_call.
    실제 모델/패키지 가용성은 호출 시점에 드러남(상위가 fail-soft 흡수)."""
    if MODEL_CRITIC in ("gpt", "gpt5", "gpt-5.5", "gpt55", "mantle", "openai"):
        return _gpt_critic_call
    return _claude_critic_call


def _is_high_risk(env: dict) -> bool:
    """L4 selective trigger — 고위험 질의만 cross-family critic 호출(비용·지연 보존).

    신호: validator WARN/FAIL, L3 후보 합의 낮음/tie, accuracy_warnings 존재.
    저위험(전부 깨끗)은 미호출 — crying-wolf·비용 회피.
    """
    val = (env.get("validation") or {}).get("verdict")
    if val in ("WARN", "FAIL"):
        return True
    ver = env.get("verification") or {}
    if ver.get("tie") or (ver.get("verdict") in ("WARN", "FAIL")):
        return True
    if env.get("accuracy_warnings"):
        return True
    return False


def _cross_family_check(question: str, env: dict, mode: str) -> None:
    """L4 diverse-lens 역번역 critic(§59) — 기존 L0~L3 무수정의 추가 레이어.

    다른 패밀리(MODEL_CRITIC)가 'SQL 의미가 질문과 맞는가'(역번역 렌즈)를 본다.
    제약(모두 충족해야 호출): CRITIC_ENABLED + deep 모드 + 고위험 질의.
    **약한 게이트**: critic 의견은 env['validation']['cross_family'] 에만 담고,
    Claude L2 verdict 와 갈리면 verdict 를 PASS→WARN 으로만 격상(차단·FAIL X).
    **fail-soft 3중**: 미활성/미인증/타임아웃/예외 모두 본 검증 verdict 불변
    (cross_family={available:false}). hard timeout 으로 walltime 강제.
    """
    if not CRITIC_ENABLED or mode != "deep":
        return
    sql_text = str(env.get("sql") or "").strip()
    if not sql_text or env.get("ok") is False or env.get("error"):
        return
    validation = env.get("validation")
    if not isinstance(validation, dict):
        return
    if not _is_high_risk(env):
        return  # 저위험 — 미호출(즉답·비용 보존)

    critic_call = _get_critic_call()  # payload→text (gpt: BedrockOpenAI / claude: Agent)
    facts = sql_struct.extract_facts(sql_text)
    payload = json.dumps({
        "user_question": question,
        "generated_sql": sql_text,
        "sample_rows": env.get("rows") or [],
        "sql_structure": sql_struct.facts_to_prompt(facts),
    })

    import concurrent.futures as _cf

    def _call() -> dict:
        # 패키지 미설치(ImportError)/미인증/빈응답은 여기서 raise → 아래 except 흡수.
        resp = critic_call(payload)
        parsed = _parse_agent_json(str(resp), expect_keys=("verdict", "restated_intent", "reason"))
        return parsed or {}

    try:
        with _cf.ThreadPoolExecutor(max_workers=1) as ex:
            cv = ex.submit(_call).result(timeout=CRITIC_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — 타임아웃/예외 → fail-open(verdict 불변)
        logger.warning("cross_family_check_failed", error=str(exc), model=MODEL_CRITIC)
        validation["cross_family"] = {"available": False, "reason": f"critic 호출 실패: {type(exc).__name__}"}
        return

    c_verdict = str(cv.get("verdict", "")).upper()
    validation["cross_family"] = {
        "available": True,
        "model": MODEL_CRITIC,
        "verdict": c_verdict if c_verdict in ("PASS", "WARN", "FAIL") else "PASS",
        "restated_intent": cv.get("restated_intent", ""),
        "reason": cv.get("reason", ""),
    }
    # 약한 게이트: 다른 패밀리가 의미 우려(WARN/FAIL) 제기 + Claude 는 PASS 였으면
    # 의견 불일치 → PASS→WARN 으로만 격상(차단 아님). 이미 WARN/FAIL 이면 유지.
    if c_verdict in ("WARN", "FAIL") and validation.get("verdict") == "PASS":
        validation["verdict"] = "WARN"
        validation["disagreement"] = True
        validation["reason"] = (
            f"{validation.get('reason', '')} | 교차검증({MODEL_CRITIC}) 의미 우려: "
            f"{cv.get('reason', '')}"
        ).strip(" |")


_answer_auditor_agent = None


def _auditor_call(payload: str) -> str:
    """Answer auditor 호출(§60 L5). Phase 1: Claude(Opus) — sql_auditor 프롬프트.
    MODEL_AUDITOR=gpt 면 추후 Mantle Responses API 로 교체(critic 경로 재사용) —
    지금은 Claude 로 배선 검증(GPT-5.5 us-east-2 안정 후 다른 패밀리로 diverse lens)."""
    global _answer_auditor_agent
    if _answer_auditor_agent is None:
        _answer_auditor_agent = Agent(
            model=_bedrock(MODEL_OPUS), tools=[], system_prompt=load_prompt("sql_auditor"),
        )
    return str(_answer_auditor_agent(payload))


def _run_answer_auditor(question: str, final_text: str, tool_results: list, mode: str) -> dict | None:
    """L5 독립 answer auditor(§60) — 최종 산문의 수치가 실행 결과에서 유래했는지 감사.

    구조·의미 검증(L2)·역번역(L4)이 끝난 뒤 **FINALIZED PROSE** 만 본다(참고
    deep-insight 의 독립 Auditor 패턴 — SKEPTICAL 기본). 약한 게이트·fail-soft.

    제약(모두 충족해야 호출): AUDITOR_ENABLED + deep 모드 + 고위험(답변에 큰 숫자
    포함 또는 validator WARN/FAIL). 저위험은 미호출(즉답·비용 보존).

    반환: {available, verdict(PASS|RETRY|NEEDS_REVIEW), defects, confidence, reason, model}
          또는 None(조건 미충족 — 미호출). RETRY/NEEDS_REVIEW 만 UI 카드로 발행한다.

    ⚠️ 답변은 이미 스트리밍 완료 → auditor 는 **수치를 고치지 않는다**(read-only,
    비파괴). RETRY = '재검증 권장' 신호일 뿐 자동 재실행 아님(crying-wolf 회피).
    """
    if not AUDITOR_ENABLED or mode != "deep":
        return None
    final_text = (final_text or "").strip()
    if not final_text:
        return None
    has_big_number = bool(re.search(r"\d{3,}", final_text))  # 3+ 자리(큰 수치)
    validator_risk = any(
        isinstance((tr.get("result") or {}).get("validation"), dict)
        and (tr["result"]["validation"].get("verdict") in ("WARN", "FAIL"))
        for tr in tool_results
    )
    if not has_big_number and not validator_risk:
        return None  # 저위험 — 미호출

    existing = {"validator_verdict": "PASS"}
    for tr in tool_results:
        val = (tr.get("result") or {}).get("validation")
        if isinstance(val, dict) and val.get("verdict"):
            existing["validator_verdict"] = val["verdict"]
    payload = json.dumps({
        "user_question": question,
        "final_answer_text": final_text,
        "tool_results": tool_results,
        "existing_validations": existing,
    }, ensure_ascii=False)

    import concurrent.futures as _cf

    def _call() -> dict:
        resp = _auditor_call(payload)
        parsed = _parse_agent_json(str(resp), expect_keys=("verdict", "defects", "confidence"))
        return parsed or {}

    try:
        with _cf.ThreadPoolExecutor(max_workers=1) as ex:
            au = ex.submit(_call).result(timeout=AUDITOR_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — 타임아웃/예외 → fail-soft(답변 불변)
        logger.warning("answer_auditor_failed", error=str(exc), model=MODEL_AUDITOR)
        return {"available": False, "reason": f"auditor 호출 실패: {type(exc).__name__}"}

    verdict = str(au.get("verdict", "")).upper()
    if verdict not in ("PASS", "RETRY", "NEEDS_REVIEW"):
        verdict = "PASS"  # 파싱 실패 → 관대 폴백(차단 아님)
    return {
        "available": True,
        "model": MODEL_AUDITOR,
        "verdict": verdict,
        "defects": au.get("defects", []) if isinstance(au.get("defects"), list) else [],
        "confidence": au.get("confidence", 0.5),
        "reason": str(au.get("reason", "")),
    }


@tool
def ask_validator(
    user_question: str,
    generated_sql: str,
    sample_rows: list[dict],
    schema_used: list[str],
    row_count: int,
    accuracy_warnings: list[str] | None = None,
) -> dict:
    """Validate SQL semantic correctness against user intent.

    Returns: {verdict: PASS|WARN|FAIL, reason, suggested_fix?, confidence}

    L2(§58): SQL 텍스트만 보던 PASS 편향을 억제하기 위해 sqlglot 으로 추출한
    결정적 구조 사실(테이블/JOIN키/집계·대상컬럼/GROUP BY/필터/COUNT DISTINCT/
    KST 앵커)과 query_db 의 결정적 정확도 경고(accuracy_warnings)를 함께 주입한다.

    ⚠️ 보통은 orchestrator 가 직접 부르지 않아도 된다 — ask_sql_specialist/
    ask_sql_verified 가 SQL 생성 시 **자동으로** validator 를 코드 실행한다(§58
    결함⑨ — 검증은 비결정적 LLM 선택이 아니라 보장된 단계). 이 tool 은 명시적
    재검증·추가 검증용.
    """
    return _run_validator(
        user_question, generated_sql, sample_rows, schema_used,
        row_count, accuracy_warnings,
    )


@tool
def ask_viz_specialist(data_shape: dict, user_intent: str) -> dict:
    """Decide chart kind/encoding from data shape + user intent.

    Returns: {kind, x, y, color?, title}
    """
    payload = json.dumps({"data_shape": data_shape, "intent": user_intent})
    return _agent_call(viz_specialist, payload, parse_json=True, tool_name="ask_viz_specialist")


@tool
def ask_report_specialist(
    request: str, period: str | None = None, fmt: str = "pdf"
) -> dict:
    """Generate a downloadable report file (PDF/PPTX/XLSX) and upload to S3.

    사용자가 "리포트/보고서/PPT/PDF로 만들어줘" 처럼 **다운로드 가능한 파일**을
    원할 때만 위임한다. 단순 화면 답변(표/차트)이면 ask_sql_specialist + render_chart
    로 충분하니 이 도구를 부르지 말 것(오분류 시 latency·비용 낭비).

    request: 무엇을 담을지 자연어("6월 비용 요약 — 월총비용/팀별/일별추이/top10").
    period: 기간 힌트(예 "2026-06") | None.
    fmt: pdf(기본) | pptx | xlsx.

    아키텍처(§49 최종): 커스텀 Code Interpreter(CODE_INTERPRETER_ID, execution role
    주입)에서 report_specialist 가 PDF 를 만들어 **샌드박스에서 직접 S3(reports/
    prefix)에 업로드**하고 report_s3_uri 를 반환한다. invoke() 가 report 이벤트로 발행
    → admin-ui 다운로드 카드. report_s3_uri 는 presign 대상(admin-api 검증 후 5분 만료).
    staging_bucket 을 payload 로 주입(샌드박스가 업로드 대상 버킷을 알아야 함).
    """
    fmt = (fmt or "pdf").lower()
    payload = json.dumps(
        {
            "request": request,
            "period": period,
            "format": fmt,
            "staging_bucket": CHAT_STAGING_BUCKET,
        },
        ensure_ascii=False,
    )
    return _agent_call(
        report_specialist, payload, parse_json=True, tool_name="ask_report_specialist"
    )


def _parse_agent_json(text: str, expect_keys: tuple[str, ...] = ()) -> dict | None:
    """sub-agent 응답에서 구조화 envelope(JSON) 추출.

    sub-agent 가 envelope 대신 마크다운 표/산문으로 답하거나, 산문에 여러 JSON
    조각을 섞는 경우가 있어 견고하게 후보를 모은다:
      1. ```json 펜스 블록 (여러 개)
      2. 균형 중괄호 스캔으로 top-level JSON object 들 (여러 개)
      3. outer brace slice (마지막 fallback)
    expect_keys 가 주어지면 그 키를 가진 후보를 우선 선택(예: sql_specialist 는
    "sql", code_specialist 는 "code") — 엉뚱한 JSON 조각(IAM statement 등) 회피.
    실패 시 None.
    """
    candidates: list[str] = []

    # 1) ```json ... ``` 펜스 (여러 개)
    for m in re.finditer(r"```(?:json)?\s*\n(\{.*?\})\s*\n```", text, re.DOTALL):
        candidates.append(m.group(1))

    # 2) 균형 중괄호 스캔 — top-level JSON object 후보 (여러 개)
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    candidates.append(text[start : i + 1])

    # 3) outer brace slice (최후)
    try:
        candidates.append(text[text.index("{") : text.rindex("}") + 1])
    except ValueError:
        pass

    parsed_objs: list[dict] = []
    for c in candidates:
        try:
            obj = json.loads(c)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            parsed_objs.append(obj)

    if not parsed_objs:
        return None
    # expect_keys 를 가진 후보 우선 (가장 많은 키 매치)
    if expect_keys:
        scored = sorted(
            parsed_objs,
            key=lambda o: sum(1 for k in expect_keys if k in o),
            reverse=True,
        )
        if any(k in scored[0] for k in expect_keys):
            return scored[0]
    return parsed_objs[0]


# tool 별 envelope 의 핵심 키 — _parse_agent_json 이 여러 JSON 후보 중 고를 때 쓴다.
_ENVELOPE_KEYS = {
    "ask_sql_specialist": ("sql", "rows", "row_count"),
    "ask_code_specialist": ("code", "result_summary", "data"),
    "ask_validator": ("verdict", "reason", "confidence"),
    "ask_viz_specialist": ("kind", "x", "y"),
}


def _structured_call(agent: Agent, payload: str, tool_name: str) -> dict | None:
    """Strands structured output 으로 envelope 를 강제 시도.

    tool 에 매핑된 Pydantic 스키마(ENVELOPE_MODELS)로 `agent(payload,
    structured_output_model=Model)` 호출 → result.structured_output.model_dump().
    이렇게 하면 sub-agent 가 답은 맞아도 필드(verdict/code)를 빠뜨리는 일이
    구조적으로 차단된다(golden case 06/09 형식 실패 타깃).

    SDK/모델/스키마 어느 단계든 실패하면 None 반환 → 호출부가 기존 텍스트
    파싱으로 graceful fallback(비파괴). 성공 단정은 하지 않는다.
    """
    model_cls = ENVELOPE_MODELS.get(tool_name)
    if model_cls is None:
        return None
    try:
        result = agent(payload, structured_output_model=model_cls)
        obj = getattr(result, "structured_output", None)
        if obj is not None:
            return obj.model_dump()
    except Exception as exc:  # noqa: BLE001 — 강제 실패는 fallback 으로 흡수
        logger.warning("structured_output_failed", tool=tool_name, error=str(exc))
    return None


def _agent_call(
    agent: Agent,
    payload: str,
    parse_json: bool = False,
    tool_name: str = "",
    *,
    schema_key: str = "",
    stash: bool = True,
) -> dict:
    """Invoke a sub-agent. parse_json 이면 구조화 dict 반환.

    우선순위: (1) Strands structured output 강제 → (2) 실패 시 자유 텍스트
    호출 + _parse_agent_json 견고 파싱(기존 동작). 구조화 결과는 _tool_results
    에 stash → 엔트리포인트가 tool_result 이벤트 + reconciliation 에 사용.

    schema_key: structured/parse 에 쓸 envelope 키(미지정 시 tool_name). L3 후보처럼
      tool_name 은 비우되(중복 stash 방지) SQL 스키마 파싱은 원할 때 사용.
    stash=False: _tool_results 에 추가하지 않음(내부 시도용 — L3 후보).
    """
    key = schema_key or tool_name
    result: dict | None = None
    if parse_json and key:
        result = _structured_call(agent, payload, key)

    if result is None:  # structured 미시도/실패 → 기존 자유 텍스트 경로
        response = agent(payload)
        text = str(response)
        if parse_json:
            parsed = _parse_agent_json(
                text, expect_keys=_ENVELOPE_KEYS.get(key, ())
            )
            result = (
                parsed if parsed is not None else {"response": text, "parse_error": True}
            )
        else:
            result = {"response": text}

    if tool_name and stash:
        bucket = _tool_results.get()
        if bucket is not None:
            bucket.append({"tool": tool_name, "result": result})
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator (top-level entry) — quick / deep 두 프로필 (§55)
#
# 퀵챗(drawer)=quick: 기존 그대로(즉답형). 사이드바 Chat(page)=deep: plan-first
# (다단계 질문은 분석 계획을 먼저 제시→사용자 승인 턴 후 실행) + 항상 검증 +
# insight-first 심화 규칙. 한 프롬프트에 if-deep 분기를 넣지 않고 **별도 Agent
# 인스턴스**(별도 system prompt)로 분리 — 프롬프트 캐시가 각각 안정되고, 퀵 경로는
# 바이트 단위 무변경(golden 회귀 0). 도구·모델은 동일(같은 5-specialist 재사용).
# deep effort 는 ORCH_EFFORT_DEEP env (기본: ORCH_EFFORT 동일) — 별도 A/B 레버.
# ─────────────────────────────────────────────────────────────────────────────
_ORCH_TOOLS = [
    ask_sql_specialist,
    ask_sql_verified,  # L3 실행기반 후보선택(§58) — 스칼라/집계 정확도 critical 질의용
    ask_code_specialist,
    ask_validator,
    ask_viz_specialist,
    ask_report_specialist,
    render_chart,
]

ORCH_EFFORT_DEEP = os.environ.get("ORCH_EFFORT_DEEP", ORCH_EFFORT).strip().lower()

orchestrator = Agent(
    model=orchestrator_model(),
    tools=_ORCH_TOOLS,
    system_prompt=load_prompt("orchestrator"),
)

orchestrator_deep = Agent(
    model=_bedrock(MODEL_OPUS, stream_thinking=True, effort=ORCH_EFFORT_DEEP or None),
    tools=_ORCH_TOOLS,
    system_prompt=load_prompt("orchestrator_deep"),
)


def _valid_chart_spec(obj: object) -> bool:
    """admin-ui ChartRenderer 계약: {kind, data, encoding(dict)} 충족 여부."""
    return (
        isinstance(obj, dict)
        and "kind" in obj
        and isinstance(obj.get("encoding"), dict)
        and "data" in obj
    )


def _extract_plan(text: str) -> tuple[dict, str] | None:
    """deep 모드 plan-first(```plan {json}``` 펜스) 추출 — §57 PlanCard.

    orchestrator_deep 이 D1 룰로 내놓는 분석 계획을 구조화 이벤트로 발행하기 위해
    최종 텍스트에서 떼어낸다. 반환: (plan_dict, 원문블록) — 원문블록은 UI 가 표시
    텍스트에서 제거(strip). {title, steps[{id,label,tool}]} 형태만 통과(관대 검증).
    """
    m = re.search(r"```plan\s*\n(.*?)\n```", text, re.DOTALL)
    if not m:
        return None
    try:
        plan = json.loads(m.group(1).strip())
    except (ValueError, TypeError):
        return None
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        return None
    return plan, m.group(0)


def _extract_chart_specs(text: str) -> list[tuple[dict, str]]:
    """완성된 reply 텍스트에서 차트 spec(JSON) 을 추출.

    인식 대상 (둘 다):
      1. ```chart ... ``` 펜스 블록
      2. bare JSON 객체 중 "kind" 와 "encoding" 키를 가진 것 (Opus 가 펜스 없이
         내놓는 경우)
    반환: [(spec_dict, 원문블록_문자열)]. 원문블록은 admin-ui 가 표시 텍스트에서
    제거(strip)하는 데 쓴다. ChartRenderer 계약({kind,data,encoding}) 에 맞는
    것만 통과.
    """
    results: list[tuple[dict, str]] = []
    fenced_spans: list[tuple[int, int]] = []  # 펜스가 차지한 (start,end) — bare 스캔서 제외
    _valid = _valid_chart_spec

    # 1) ```chart ... ``` 펜스
    for m in re.finditer(r"```chart\s*\n(.*?)\n```", text, re.DOTALL):
        block = m.group(0)
        body = m.group(1).strip()
        try:
            spec = json.loads(body)
        except (ValueError, TypeError):
            continue
        if _valid(spec):
            fenced_spans.append((m.start(), m.end()))
            results.append((spec, block))

    def _in_fence(idx: int) -> bool:
        return any(s <= idx < e for s, e in fenced_spans)

    # 2) bare JSON — 균형 중괄호 스캔. 펜스 안에 이미 잡힌 영역은 건너뜀.
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0 and not _in_fence(start):
                    candidate = text[start : i + 1]
                    if '"kind"' in candidate and '"encoding"' in candidate:
                        try:
                            spec = json.loads(candidate)
                        except (ValueError, TypeError):
                            spec = None
                        if spec is not None and _valid(spec):
                            results.append((spec, candidate))
    return results


def _collect_numbers(obj: object, out: set) -> None:
    """중첩 구조(envelope)에서 모든 수치를 평탄 수집 (reconciliation 기준값)."""
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        out.add(round(float(obj), 4))
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_numbers(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_numbers(v, out)
    elif isinstance(obj, str):
        # 문자열 안 숫자도(예: "$4.60") 후보로
        for m in re.findall(r"-?\d+(?:\.\d+)?", obj):
            try:
                out.add(round(float(m), 4))
            except ValueError:
                pass


# 시간 표현 단위 — 숫자 뒤에 이 단위가 붙으면 데이터 값이 아니라 기간/날짜
# 서술(예: "지난 30일", "최근 3개월", "24시간")이라 reconciliation 후보에서
# 제외한다. 뒤에 Hangul/영문 글자가 더 붙으면(예: "100초과", "30분석") 다른
# 단어이므로 negative lookahead 로 경계를 둬 매치하지 않는다. 시간 연속 접미
# (간/동안/째/내/전/후/여)는 단위의 일부로 허용("30일간", "3개월째").
# NOTE: 주/달/분/일/월/년 은 한국어 동음이의(주=주식 share, 달=moon, 분=명 등)라
# 절(clause) 끝에 붙은 일부 실제 데이터값이 시간으로 오인돼 제외될 수 있다.
# fail-soft WARN 게이트의 의도된 recall/precision trade-off (정밀도보다 오탐 억제).
_TIME_UNIT = (
    r"(?:일|주|개월|달|월|년|분|초|시간"
    r"|days?|weeks?|months?|years?|hours?|minutes?|mins?|seconds?|secs?)"
    r"(?:간|동안|째|내|전|후|여)?(?![가-힣A-Za-z])"
)
# 숫자 + 선택적 K/M 배수 + 선택적 % + 선택적 시간단위. (4) 그룹이 시간단위.
# K/M 배수((2))는 뒤에 글자가 더 오면 매치 금지 — 그래야 "30months"/"30minutes"
# 의 m 을 'million' 으로 먹지 않고 시간단위((4))가 months/minutes 를 잡는다.
_NUM_RE = re.compile(
    r"(-?\d[\d,]*(?:\.\d+)?)\s*([KkMm](?![A-Za-z]))?\s*(%)?\s*(" + _TIME_UNIT + r")?",
    re.IGNORECASE,
)


def _reconcile_numbers(text: str, tool_results: list) -> dict | None:
    """harness: deterministic numeric grounding gate.

    최종 답변 텍스트의 숫자 토큰이 실행된 결과(envelope)에서 유래했는지 검사.
    유래 안 한 '큰' 숫자가 있으면 WARN(fail-soft, 차단 안 함). 연/ID/소수
    rounding/퍼센트 등 false positive 를 줄이기 위해 보수적으로 필터.

    또한 trajectory check: 숫자 산문은 있는데 tool_result 가 0 이면 '계산 안 하고
    서술' 시그니처 → WARN.
    """
    ground: set = set()
    for tr in tool_results:
        _collect_numbers(tr.get("result"), ground)

    # 텍스트에서 통화/큰 수 위주 숫자 추출 (K/M 확장). 단 아래는 파생/서술
    # 표시값이라 raw 셀과 다를 수 있어 후보에서 제외(false positive 방지):
    #   - 퍼센트(뒤에 %)        : 점유율 등 파생값
    #   - 시간 표현(30일/3개월/24시간) : 데이터 값이 아니라 기간/날짜 서술
    candidates: set = set()
    for m in _NUM_RE.finditer(text):
        if m.group(3) == "%":
            continue  # 퍼센트는 reconciliation 대상에서 제외
        if m.group(4):
            continue  # 시간 단위가 붙은 숫자(기간/날짜)는 제외
        raw = m.group(1).replace(",", "")
        try:
            val = float(raw)
        except ValueError:
            continue
        suffix = (m.group(2) or "").lower()
        if suffix == "k":
            val *= 1_000
        elif suffix == "m":
            val *= 1_000_000
        candidates.add(round(val, 4))

    def _grounded(v: float) -> bool:
        # 정확 일치 또는 소수 rounding 허용(예: 4.5987 → 4.60), 또는 정수부 일치
        for g in ground:
            if abs(g - v) <= max(0.01, abs(g) * 0.01):
                return True
            if abs(round(g) - round(v)) < 0.5 and abs(g) >= 1:
                return True
        return False

    # 노이즈 필터: |v|<10 (연도 아닌 작은 수·퍼센트·rounding) 과 1900~2100(연도)는 제외
    suspicious = [
        v
        for v in candidates
        if abs(v) >= 10 and not (1900 <= v <= 2100) and not _grounded(v)
    ]

    has_numeric_prose = bool([v for v in candidates if abs(v) >= 10 and not (1900 <= v <= 2100)])
    if has_numeric_prose and not tool_results:
        return {
            "verdict": "WARN",
            "reason": "수치가 실행된 쿼리/코드 결과 없이 서술되었습니다. 정확도 확인 필요.",
            "confidence": 0.6,
        }
    if suspicious:
        sample = ", ".join(str(v) for v in sorted(suspicious)[:5])
        return {
            "verdict": "WARN",
            "reason": f"실행 결과에서 확인되지 않은 수치: {sample}. 정확도 재확인 권장.",
            "confidence": 0.55,
        }
    return None


app = BedrockAgentCoreApp()


@app.entrypoint
async def invoke(payload: dict):
    """Phase 4 — async-generator entrypoint으로 실시간 스트리밍.

    AgentCore 1.7.0 은 async-generator entrypoint 를 감지해 StreamingResponse
    (text/event-stream)로 노출하고, yield 한 dict 를 `data: <json>\\n\\n` 한 프레임
    으로 직렬화한다(우리가 data:/event: 를 붙이면 안 됨).

    orchestrator.stream_async(content) 의 이벤트:
      - event["data"]   : assistant 텍스트 델타(TextStreamEvent) → {type:text,chunk}
      - event["result"] : 최종 AgentResultEvent(= 옛 str(response))
      - (reasoning/tool_use/tool_result 등은 data 없음 → skip)

    reply 안의 ```chart {json} ``` 펜스 블록은 라인 상태머신으로 떼어내
    {type:chart, spec} 로 별도 발행 → admin-ui ChartRenderer 가 렌더.

    agents-as-tools 구조상 sub-agent(SQL/Code/Validator/Viz) 토큰은 스트리밍되지
    않고 orchestrator 최종 텍스트만 흐른다(@tool 래퍼가 동기 blocking).
    """
    content = (payload or {}).get("content", "")
    session_id = (payload or {}).get("session_id")
    screen_context = (payload or {}).get("screen_context")
    # mode(§55): "deep"(사이드바 Chat — plan-first 심층분석) | 그 외("quick" 기본).
    # 퀵 경로는 기존 orchestrator 그대로 — 바이트 무변경(golden 회귀 0).
    mode = (payload or {}).get("mode", "quick")
    active_orchestrator = orchestrator_deep if mode == "deep" else orchestrator
    logger.info(
        "agent_invoke", session_id=session_id, mode=mode, content_preview=content[:80]
    )

    # 화면 컨텍스트 주입 — admin-ui 가 "지금 보는 화면"({page, period?, data?})을
    # 동봉하면 user 턴 앞에 블록으로 붙여 orchestrator 가 맥락을 인지하게 한다.
    # system prompt 는 안 건드림(프롬프트 캐시 보존). data 는 admin-api 가 보낸
    # 집계 수치(PII 없음). agent 는 query_db 직결이라 필요하면 재조회 — 컨텍스트는
    # "사용자가 보는 것"을 알려줄 뿐 강제 데이터원이 아님.
    if isinstance(screen_context, dict) and screen_context.get("page"):
        try:
            ctx_block = json.dumps(screen_context, ensure_ascii=False)
        except (TypeError, ValueError):
            ctx_block = str(screen_context)
        content = (
            f"[사용자가 현재 보고 있는 화면]\n{ctx_block}\n\n"
            f"[사용자 질문]\n{content}"
        )

    # __ping__ 프리워밍 쇼트서킷(§54) — admin-api 가 세션 생성 시 fire-and-forget
    # 으로 호출해 AgentCore microVM 콜드스타트를 사용자의 첫 질문 **전에** 흡수한다.
    # LLM/도구를 일절 타지 않고 즉시 종료(비용 0, 수십 ms). 실제 질문의 cold 1.0s
    # → warm 0.15s (첫 프레임 실측).
    if content == "__ping__":
        yield {"type": "done", "session_id": session_id, "ping": True}
        return

    # sub-agent 결과 / 차트 spec stash 버킷 초기화 (이 턴 한정).
    _tool_results.set([])
    _chart_specs.set([])
    # L3 검증 메타 stash + 현재 invoke 컨텍스트(mode/session) — ask_sql_verified 가
    # k(후보 수) 결정과 검증 이벤트 발행에 사용.
    _verifications.set([])
    _mode.set(mode)
    _session_id.set(session_id)

    # 즉시 thinking 신호 — 첫 토큰까지 수십 초 걸릴 수 있어 사용자에게 "작업 중" 표시.
    yield {"type": "thinking", "text": "분석 중…"}

    full = ""
    seen_tool_uses: set = set()
    emitted_results = 0  # 이미 발행한 tool_result 개수 (stash 와 동기화)
    # tool 경계 직후 첫 text 델타 앞에 단락 구분 삽입 — orchestrator 가 tool 호출
    # 전후로 짧은 산문을 끊어 내보내는데, 그대로 이어붙이면 "조회하겠습니다.검증을
    # 진행하겠습니다." 처럼 문장이 붙는다(§52 UI 검증서 발견). 경계마다 \n\n.
    pending_break = False

    # ── 공백 없는 스트리밍: stream_async 를 별도 pump task 로 돌리고, heartbeat
    # task 와 함께 단일 asyncio.Queue 로 머지한다. sub-agent(@tool)는 Strands 가
    # asyncio.to_thread 로 실행하므로(decorator.py:633) blocking 중에도 이벤트
    # 루프는 살아있어 heartbeat task 가 그 틈에 프레임을 흘릴 수 있다 — 이게 20~60초
    # "공백"을 메우는 핵심. text 델타가 시작되면 heartbeat 를 즉시 정지(중복 표시
    # 방지)하고, 모든 종료 경로(정상/예외/조기 close)에서 두 task 를 정리한다.
    queue: asyncio.Queue = asyncio.Queue()
    _PUMP_DONE = object()
    text_started = asyncio.Event()
    # in-flight tool 추적 — heartbeat 가 "직전 끝난 tool"(stash)이 아니라 "지금
    # 실행 중인 tool"로 라벨링하도록 current_tool_use 관측을 공유. {toolUseId: (name, t0)}.
    in_flight: dict[str, tuple[str, float]] = {}

    async def _pump() -> None:
        try:
            async for ev in active_orchestrator.stream_async(content):
                queue.put_nowait(("stream", ev))
        except Exception as exc:  # 펌프 예외를 consumer 로 전달(삼키지 않음)
            queue.put_nowait(("error", exc))
        finally:
            queue.put_nowait(("stream", _PUMP_DONE))

    async def _heartbeat() -> None:
        t_start = time.monotonic()
        delay = _HEARTBEAT_FIRST
        try:
            while not text_started.is_set():
                await asyncio.sleep(delay)
                delay = _HEARTBEAT_INTERVAL
                if text_started.is_set():
                    break
                elapsed = time.monotonic() - t_start
                # 현재 in-flight tool 로 단계/라벨 합성. 없으면(첫 위임 전) 기본.
                live = sorted(in_flight.values(), key=lambda x: x[1])
                if live:
                    name, t0 = live[-1]
                    phase, label = _HEARTBEAT_PHASE.get(name, ("work", "처리 중"))
                else:
                    phase, label = ("think", "질문 분석 중")
                queue.put_nowait((
                    "hb",
                    {
                        "type": "heartbeat",
                        "phase": phase,
                        "label": label,
                        "elapsed_ms": int(elapsed * 1000),
                    },
                ))
        except asyncio.CancelledError:
            raise

    pump_task = asyncio.create_task(_pump())
    hb_task = asyncio.create_task(_heartbeat())

    try:
        while True:
            source, item = await queue.get()

            if source == "error":
                raise item

            if source == "hb":
                # 텍스트가 이미 시작했으면 늦게 도착한 hb 는 버린다(순서·중복 보호).
                if not text_started.is_set():
                    yield item
                continue

            # source == "stream"
            event = item
            if event is _PUMP_DONE:
                break

            # (a) 도구 호출 투명성 + in-flight 등록(heartbeat 라벨용).
            tu = event.get("current_tool_use") if isinstance(event, dict) else None
            if tu and tu.get("toolUseId"):
                tuid = tu["toolUseId"]
                if tuid not in seen_tool_uses:
                    seen_tool_uses.add(tuid)
                    in_flight[tuid] = (tu.get("name", "tool"), time.monotonic())
                    pending_break = True  # 다음 text 델타 앞에 단락 구분
                    yield {
                        "type": "tool_call",
                        "tool": tu.get("name", "tool"),
                        "args": tu.get("input", {}),
                    }

            # (b) stash 된 sub-agent 구조화 결과를 tool_result 로 발행 + in-flight 해제.
            bucket = _tool_results.get() or []
            while emitted_results < len(bucket):
                tr = bucket[emitted_results]
                emitted_results += 1
                # 완료된 tool 은 in-flight 에서 제거(heartbeat 라벨이 다음 단계로 이동).
                for tuid, (nm, _t) in list(in_flight.items()):
                    if nm == tr["tool"]:
                        in_flight.pop(tuid, None)
                        break
                yield {"type": "tool_result", "tool": tr["tool"], "result": tr["result"]}

            # (b2) 추론 요약 델타(orchestrator display:summarized) — "사고 과정"으로
            # 별도 렌더(본문 분리). heartbeat 와는 다른 레인(reasoning=의미텍스트).
            if event.get("reasoning") and event.get("reasoningText"):
                yield {"type": "reasoning", "chunk": event["reasoningText"]}

            # (c) 텍스트 델타 — 첫 델타에 heartbeat 정지 신호. tool 경계 직후면
            # 단락 구분(\n\n)을 앞에 붙여 문장 붙음 방지(기존 본문이 있을 때만).
            if "data" in event:
                delta = event["data"]
                if not text_started.is_set():
                    text_started.set()
                if pending_break:
                    pending_break = False
                    if full and not full.endswith("\n"):
                        full += "\n\n"
                        yield {"type": "text", "chunk": "\n\n"}
                full += delta
                yield {"type": "text", "chunk": delta}
    finally:
        # 모든 종료 경로에서 task 정리. heartbeat 는 즉시 취소. pump 는 cancel 해도
        # to_thread 의 OS 스레드(boto3 Bedrock 호출)는 끝까지 돌 수 있으나(스레드는
        # 강제종료 불가), 정상 흐름에선 _PUMP_DONE 이후라 이미 완료 상태.
        text_started.set()
        hb_task.cancel()
        if not pump_task.done():
            pump_task.cancel()
        await asyncio.gather(hb_task, pump_task, return_exceptions=True)

    # 루프 종료 후 남은 결과 flush
    bucket = _tool_results.get() or []
    while emitted_results < len(bucket):
        tr = bucket[emitted_results]
        emitted_results += 1
        yield {"type": "tool_result", "tool": tr["tool"], "result": tr["result"]}

    # plan 발행(§57 PlanCard) — deep 모드 plan-first 의 ```plan 펜스를 구조화
    # 이벤트로. strip 으로 본문에서 raw JSON 을 제거(차트 strip 과 동일 패턴).
    plan_found = _extract_plan(full)
    if plan_found:
        plan_obj, plan_block = plan_found
        yield {"type": "plan", "plan": plan_obj, "strip": plan_block}

    # 차트 발행 — 두 경로:
    #  1) render_chart tool 이 stash 한 spec (orchestrator 가 도구로 차트 생성;
    #     spec 이 텍스트에 안 나오므로 stash 로만 잡힘 — 주 경로).
    #  2) 최종 텍스트 내 ```chart 펜스/bare JSON (하위호환 fallback).
    emitted_charts: list[dict] = []
    for spec in _chart_specs.get() or []:
        if _valid_chart_spec(spec):
            emitted_charts.append(spec)
            yield {"type": "chart", "spec": spec}
    for spec, raw_block in _extract_chart_specs(full):
        if spec not in emitted_charts:  # tool 로 이미 발행한 것과 중복 방지
            yield {"type": "chart", "spec": spec, "strip": raw_block}

    # 리포트 발행 — ask_report_specialist 결과(이미 tool_result 로 발행됨)에서
    # report_s3_uri 가 있는 것만 골라 별도 `report` 이벤트로 발행한다. UI 다운로드
    # 카드용 타입 신호이며, tool_result 와 **중복 발행 아님**(서로 다른 type, UI 가
    # 각각 다르게 렌더). admin-api 가 s3_uri 를 presign 검증 후 다운로드 링크화.
    for tr in bucket:
        if tr.get("tool") != "ask_report_specialist":
            continue
        res = tr.get("result") or {}
        uri = res.get("report_s3_uri")
        if uri and isinstance(uri, str) and uri.startswith("s3://"):
            yield {
                "type": "report",
                "s3_uri": uri,
                "file_name": res.get("file_name", "report"),
                "format": res.get("format", "pdf"),
                "summary": res.get("summary", ""),
                "page_count": res.get("page_count"),
            }

    # L3 검증 타임라인 발행(§58) — deep 모드만 stash 채워짐(quick 은 즉답성 위해
    # 미발행, 결과만). 실행기반 후보선택의 합의도·후보수를 카드로 노출해 "이 숫자를
    # 어떻게 검증했나"를 운영자가 볼 수 있게(설명가능성 = 신뢰).
    for v in _verifications.get() or []:
        yield {"type": "verification", "result": v}

    # 결정적 validator-skip 게이트(§58 결함⑨): SQL 은 돌았는데 ask_validator 가
    # 호출되지 않았으면 — 프롬프트가 "반드시 검증"이라 해도 LLM 이 생략할 수 있다
    # (게이트 비강제). 검증 누락을 **코드로 가시화**(fail-soft WARN). report 흐름은
    # validator 선행 금지(§49)라 제외.
    tool_names = {tr.get("tool") for tr in bucket}
    sql_ran = "ask_sql_specialist" in tool_names
    validated = "ask_validator" in tool_names
    reported = "ask_report_specialist" in tool_names
    if sql_ran and not validated and not reported:
        yield {
            "type": "validator",
            "result": {
                "verdict": "WARN",
                "reason": "SQL 결과가 의미 검증(validator)을 거치지 않았습니다. 숫자 정확도를 재확인하세요.",
                "confidence": 0.5,
            },
        }

    # 결정적 reconciliation gate (harness): 최종 텍스트의 숫자가 실행 결과(envelope)
    # 에서 유래했는지 Python 으로 검사. 유래 안 한 숫자가 있으면 WARN (fail-soft).
    warn = _reconcile_numbers(full, bucket)
    if warn:
        yield {"type": "validator", "result": warn}

    # L5 독립 answer auditor(§60) — deep+고위험만, 최종 산문의 수치 cite 무결성을
    # 회의적으로 재검(결정적 reconcile 의 LLM 보강 렌즈). 약한 게이트·fail-soft:
    # PASS 면 미발행(노이즈 회피), RETRY/NEEDS_REVIEW 만 advisory 카드로 발행(비파괴).
    try:
        audit = _run_answer_auditor(content, full, bucket, mode)
        if audit and audit.get("available") and audit.get("verdict") in ("RETRY", "NEEDS_REVIEW"):
            yield {"type": "audit", "result": audit}
    except Exception as exc:  # noqa: BLE001 — auditor 가 done 을 막지 않게(최종 안전망)
        logger.warning("answer_auditor_emit_failed", error=str(exc))

    yield {
        "type": "done",
        "session_id": session_id,
        "agent": "admin-chat-agent",
        "phase": 4,
    }


if __name__ == "__main__":
    app.run()
