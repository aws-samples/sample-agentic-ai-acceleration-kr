"""Strands Agent / Graph 조립 (통합 계층).

두 가지 실행 형태를 제공한다. 둘 다 동일한 시스템 프롬프트/도구/모델을 공유하며,
stream_async 로 스트리밍하므로 stream_translator 가 그대로 처리한다.

- build_graph(): ARCHITECTURE §4.2 확정 골격. 결정적 노드 전이 + self-correction 조건부 엣지.
- build_single_agent(): 단일 Agent + 도구 2개 폴백. 시스템 프롬프트로 파이프라인 유도.

MODE 는 config.Settings.mode 로 선택한다(기본 "graph").
SDK 의존성은 지연 임포트하여 순수 로직 테스트와 분리한다.

M5 additive: `bundle_override` 를 주입하면 활성 Configuration Bundle 의
`system_prompt` / `model_id` 로 코드 기본값을 오버라이드한다(§9.5). 미주입(None)이면
기존 동작 그대로다.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .bundle_config import DEFAULT_BUNDLE_LABEL, BundleOverride
from .config import Settings
from .prompts import SYSTEM_PROMPT


class OrchestratorBuilder:
    """모델·MCP 도구·메모리를 묶어 Agent 또는 Graph 를 생성하는 빌더.

    tools_by_server: {"sql": [run_sql tools], "semantic": [search_schema tools]}
    session_manager: AgentCoreMemorySessionManager | None (STM)
    bundle_override: BundleOverride | None (M5 additive) — system_prompt/model_id 오버라이드.
      None 이면 코드 기본값(SYSTEM_PROMPT, Settings.model_id)을 사용한다.
    """

    def __init__(
        self,
        settings: Settings,
        sql_tools: list[Any],
        semantic_tools: list[Any],
        session_manager: Any | None = None,
        clarification_tool: Any | None = None,
        bundle_override: BundleOverride | None = None,
    ) -> None:
        self._settings = settings
        self._sql_tools = sql_tools
        self._semantic_tools = semantic_tools
        self._session_manager = session_manager
        # clarification(재요청) 도구. 테스트에서 주입 가능. None 이면 실제 도구를 지연 생성.
        self._clarification_tool = clarification_tool
        self._bundle_override = bundle_override

    @property
    def system_prompt(self) -> str:
        """실제 사용할 시스템 프롬프트(bundle 오버라이드 우선)."""
        override = self._bundle_override
        if override is not None and override.system_prompt:
            return override.system_prompt
        return SYSTEM_PROMPT

    @property
    def model_id(self) -> str:
        """실제 사용할 모델 ID(bundle 오버라이드 우선)."""
        override = self._bundle_override
        if override is not None and override.model_id:
            return override.model_id
        return self._settings.model_id

    @property
    def bundle_label(self) -> str:
        """version vector 스탬프용 bundle 라벨(미적용 시 "default")."""
        override = self._bundle_override
        return override.bundle_label if override is not None else DEFAULT_BUNDLE_LABEL

    def _get_clarification_tool(self) -> Any:
        """clarification 도구를 반환(주입값 우선, 없으면 지연 생성)."""
        if self._clarification_tool is None:
            from .clarification import create_clarification_tool

            self._clarification_tool = create_clarification_tool()
        return self._clarification_tool

    def _model(self) -> Any:
        from strands.models import BedrockModel

        return BedrockModel(
            model_id=self.model_id,
            region_name=self._settings.region,
            streaming=True,
            temperature=0.0,
        )

    def build_single_agent(self) -> Any:
        """단일 Agent + 도구 3종 (clarification + 폴백/기본 신뢰 경로)."""
        from strands import Agent

        return Agent(
            model=self._model(),
            system_prompt=self.system_prompt,
            tools=[self._get_clarification_tool(), *self._semantic_tools, *self._sql_tools],
            session_manager=self._session_manager,
        )

    def build_graph(self) -> Any:
        """Strands Graph — 결정적 골격 (ARCHITECTURE §4.2).

        노드:
          intent          질의 해석 (도구 없음)
          schema_linking   search_schema 호출 (semantic 도구)
          sql_generation   SELECT 생성 (도구 없음)
          execution        run_sql 호출 (sql 도구)
          synthesis        한국어 결과 요약 (도구 없음)
        엣지:
          intent → schema_linking → sql_generation → execution → synthesis
          execution → sql_generation (조건부: self-correction, 최대 N회)
        """
        from strands import Agent
        from strands.multiagent import GraphBuilder

        model = self._model()
        base_prompt = self.system_prompt

        intent = Agent(
            name="intent",
            model=model,
            system_prompt=(
                base_prompt
                + "\n\n## 현재 단계: intent 분석\n"
                "사용자 질문의 의도(집계/조회 대상, 필터, 기간 등)를 한국어로 간결히 정리하세요. "
                "다음 중 하나라도 해당하면 **반드시** `request_clarification` 도구를 한 번 호출해 "
                "사용자에게 되물어야 합니다(임의 가정 금지):\n"
                "- 기간이 필요한 질의인데 기간이 없거나 '최근'/'요즘'처럼 범위가 불명확함 "
                "(예: '최근 매출 알려줘' → 기간 선택지를 제시하며 되묻기)\n"
                "- 지표 정의가 여러 해석 가능(예: '실적'이 매출인지 주문 수인지)\n"
                "- 조회 대상 엔티티가 특정되지 않음\n"
                "되물을 때 fields 는 가능하면 select(선택지 제시) 또는 date_range 로 구성하세요. "
                "응답을 받으면 그 해석으로 진행합니다. 위에 해당하지 않는 명확한 질의만 도구 없이 "
                "합리적 해석을 명시하고 진행하세요. SQL 은 아직 작성 안 함."
            ),
            tools=[self._get_clarification_tool()],
        )
        schema_linking = Agent(
            name="schema_linking",
            model=model,
            system_prompt=(
                base_prompt
                + "\n\n## 현재 단계: schema linking\n"
                "`search_schema` 도구를 호출해 질문과 관련된 테이블/컬럼/용어를 조회하고, "
                "다음 단계의 SQL 작성에 필요한 스키마 컨텍스트를 정리해 전달하세요."
            ),
            tools=list(self._semantic_tools),
        )
        sql_generation = Agent(
            name="sql_generation",
            model=model,
            system_prompt=(
                base_prompt
                + "\n\n## 현재 단계: SQL 생성\n"
                "앞 단계의 스키마 컨텍스트만 사용해 단일 PostgreSQL SELECT 문을 작성하세요. "
                "직전 실행이 실패했다면 오류 메시지를 반영해 수정하세요. SQL 만 출력합니다."
            ),
        )
        execution = Agent(
            name="execution",
            model=model,
            system_prompt=(
                base_prompt
                + "\n\n## 현재 단계: 실행\n"
                "앞 단계의 SQL 을 `run_sql` 도구로 실행하세요. "
                "결과 상태(ok/rejected/error)와 결과 표(또는 오류 메시지)를 그대로 전달하세요."
            ),
            tools=list(self._sql_tools),
        )
        synthesis = Agent(
            name="synthesis",
            model=model,
            system_prompt=(
                base_prompt
                + "\n\n## 현재 단계: 결과 요약\n"
                "실행 결과 표를 바탕으로 사용자 질문에 대한 답을 자연어로 요약하세요. "
                "질의 언어에 맞추되 불명확하면 한국어로 답합니다."
            ),
        )

        builder = GraphBuilder()
        builder.add_node(intent, "intent")
        builder.add_node(schema_linking, "schema_linking")
        builder.add_node(sql_generation, "sql_generation")
        builder.add_node(execution, "execution")
        builder.add_node(synthesis, "synthesis")

        builder.set_entry_point("intent")
        builder.add_edge("intent", "schema_linking")
        builder.add_edge("schema_linking", "sql_generation")
        builder.add_edge("sql_generation", "execution")
        # 정상 실행이면 요약으로, 실패면 SQL 재생성으로 (설정된 재시도 예산까지).
        max_corr = self._settings.max_sql_corrections
        builder.add_edge("execution", "synthesis", condition=_execution_succeeded)
        builder.add_edge(
            "execution",
            "sql_generation",
            condition=make_retry_condition(max_corr),
        )

        # 루프 안전장치: self-correction 최대 N회 + 전체 노드 실행 상한.
        # 정상 경로 5노드 + 재시도당 (sql_generation, execution) 2노드.
        max_nodes = 5 + 2 * self._settings.max_sql_corrections
        builder.set_max_node_executions(max_nodes)
        builder.reset_on_revisit(True)

        return builder.build()


def _count_executions(state: Any, node_id: str) -> int:
    """그래프 상태에서 특정 노드가 실행된 횟수를 센다(방어적 추출)."""
    history = getattr(state, "execution_order", None) or getattr(state, "node_history", None)
    if not history:
        return 0
    count = 0
    for entry in history:
        entry_id = getattr(entry, "node_id", None) or getattr(entry, "id", None) or entry
        if str(entry_id) == node_id:
            count += 1
    return count


def _last_execution_failed(state: Any) -> bool:
    """execution 노드의 마지막 결과가 rejected/error 인지 판단."""
    from .mcp_parsing import parse_sql_result

    results = getattr(state, "results", None)
    if not results:
        return False
    node_result = results.get("execution") if isinstance(results, dict) else None
    if node_result is None:
        return False
    text = _result_text(node_result)
    if not text:
        return False
    lowered = text.lower()
    # 실행 노드가 남긴 텍스트에서 상태 신호를 탐지.
    if '"status"' in lowered or "status" in lowered:
        try:
            result = parse_sql_result(_extract_json(text))
            return result.needs_correction
        except (ValueError, KeyError):
            pass
    return ("rejected" in lowered) or ("error" in lowered)


def _execution_succeeded(state: Any) -> bool:
    return not _last_execution_failed(state)


def execution_needs_retry(state: Any, max_corrections: int) -> bool:
    """execution 실패 후 재시도해야 하는지(예산 내인지) 판단."""
    if not _last_execution_failed(state):
        return False
    # 실패는 이미 확인됨 → 재시도 여부는 예산 문제뿐.
    # execution 실행 횟수 == 시도 번호(1부터). 다음 시도가 예산 내인지 확인.
    attempts = _count_executions(state, "execution")
    return attempts <= max_corrections


def make_retry_condition(max_corrections: int) -> Callable[[Any], bool]:
    """설정된 재시도 예산을 캡처한 Graph 조건 함수(state -> bool)를 생성."""

    def _condition(state: Any) -> bool:
        return execution_needs_retry(state, max_corrections)

    return _condition


def _result_text(node_result: Any) -> str:
    result = getattr(node_result, "result", node_result)
    if result is None:
        return ""
    return str(result)


def _extract_json(text: str) -> str:
    """텍스트에서 가장 바깥 JSON 객체를 추출(없으면 원본 반환)."""
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text
