# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Structured-output envelopes for the sub-agents (forced schema).

`main.py` 의 ask_* @tool 래퍼는 그동안 "JSON 만 반환해줘" 라는 *프롬프트 부탁* +
`_parse_agent_json` 견고 파싱에 의존했다. 그 결과 sub-agent 가 답은 맞아도
필드(verdict/code 등)를 빠뜨리면 채점이 실패했다(golden case 06 validator_verdict,
case 09 code envelope).

이 모듈은 Strands 1.40+ 의 structured output(`agent(payload,
structured_output_model=Model)` → `result.structured_output`)으로 envelope 를
*강제*하기 위한 Pydantic 스키마를 정의한다. 강제가 실패하면 main 이 기존
텍스트 파싱으로 graceful fallback 하므로 비파괴(additive)다.

필드명은 기존 이벤트 계약(scoring.py 의 extract_sql/extract_code/
extract_validator_verdict 등)과 **정확히** 일치해야 한다 — model_dump() 결과가
그대로 tool_result 이벤트의 result 로 흐른다.

pydantic 만 의존(strands/boto3 무관) → 단위 테스트 가능.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SqlEnvelope(BaseModel):
    """SQL Specialist 의 구조화 반환. `sql` 필수 — 빠지면 채점 불가."""

    model_config = ConfigDict(extra="allow")

    sql: str = Field(description="실제 실행한 SELECT 문 전체 (생략·요약 금지)")
    rows: list[dict] = Field(default_factory=list, description="최대 5행 sample")
    row_count: int = Field(default=0, description="전체 결과 행 수")
    columns: list[dict] = Field(
        default_factory=list, description="컬럼 메타 [{name, type_oid}]"
    )
    s3_uri: str | None = Field(default=None, description="대용량 결과 staging S3 URI")
    note: str | None = Field(default=None, description="자체 sanity 메모 | null")


class CodeEnvelope(BaseModel):
    """Code Specialist 의 구조화 반환. `code`·`result_summary` 필수."""

    model_config = ConfigDict(extra="allow")

    result_summary: str = Field(description="분석 결과 한국어 요약 1-2문장")
    code: str = Field(description="execute_python 으로 실행한 Python 코드 전체 (audit)")
    data: dict | list | None = Field(default=None, description="작은 결과 inline | null")
    chart_s3_url: str | None = Field(default=None, description="matplotlib PNG S3 | null")
    csv_s3_url: str | None = Field(default=None, description="row-level 결과 CSV S3 | null")


class ValidatorEnvelope(BaseModel):
    """SQL Validator 의 구조화 반환. `verdict`·`reason` 필수."""

    model_config = ConfigDict(extra="allow")

    verdict: str = Field(description="PASS | WARN | FAIL")
    reason: str = Field(description="한 문장 — 무엇을 봤고 왜 그 verdict 인지")
    suggested_fix: str | None = Field(default=None, description="수정 hint | null")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0, description="0.0~1.0")

    @field_validator("verdict", mode="before")
    @classmethod
    def _normalize_verdict(cls, v: object) -> str:
        """'PASS - looks good' / 'pass' 같은 변형을 표준 토큰으로 정규화.

        scoring.py 가 verdict == 'PASS' 로 정확 비교하므로, 모델이 산문을
        섞어도 PASS/WARN/FAIL 토큰을 추출해 채점 실패(case 06)를 막는다.
        """
        s = str(v).upper()
        for token in ("FAIL", "WARN", "PASS"):
            if token in s:
                return token
        return s.strip()


class VizEnvelope(BaseModel):
    """Viz Specialist 의 구조화 반환. `kind` 필수."""

    model_config = ConfigDict(extra="allow")

    kind: str = Field(description="bar | line | area | pie | table | kpi | image")
    x: str | None = Field(default=None, description="x축 컬럼명 | null")
    y: str | list[str] | None = Field(default=None, description="y축 컬럼명 | 리스트 | null")
    color: str | None = Field(default=None, description="series 분리 컬럼 | null")
    title: str | None = Field(default=None, description="한국어 제목")


class ReportEnvelope(BaseModel):
    """Report Specialist 의 구조화 반환. 다운로드 파일(S3)을 가리킨다.

    아키텍처(§49 최종): 커스텀 Code Interpreter(CODE_INTERPRETER_ID, execution role
    주입)에서 샌드박스가 **직접 S3(reports/ prefix)에 업로드**하고 그 URI 를 반환한다.
    (초기 base64-경유 방식은 LLM 이 base64 를 토큰 재생성해 느리고 깨져서 폐기 — 커스텀
    인터프리터로 샌드박스 자격증명 문제를 해결해 직접 쓰기로 전환.)

    `report_s3_uri`·`file_name`·`format`·`summary` 필수. invoke() 가 report 이벤트로
    발행 → admin-ui 다운로드 카드. report_s3_uri 는 presign 대상(admin-api 가 reports/
    prefix 검증 후 5분 만료 URL 발급). 실패 시 report_s3_uri 빈 문자열 + summary 에 원인.
    """

    model_config = ConfigDict(extra="allow")

    report_s3_uri: str = Field(description="샌드박스가 업로드한 리포트 s3:// URI (reports/ prefix). 실패 시 빈 문자열")
    file_name: str = Field(description="다운로드 표시용 파일명 (예: cost-report-2026-06.pdf)")
    format: str = Field(description="pdf | pptx | xlsx")
    summary: str = Field(description="리포트 핵심 요약 1-2문장 (채팅 본문에 표시)")
    page_count: int | None = Field(default=None, description="페이지/슬라이드 수 | null")


# tool 이름 → 강제 스키마 매핑. main._agent_call 이 참조.
ENVELOPE_MODELS = {
    "ask_sql_specialist": SqlEnvelope,
    "ask_code_specialist": CodeEnvelope,
    "ask_validator": ValidatorEnvelope,
    "ask_viz_specialist": VizEnvelope,
    "ask_report_specialist": ReportEnvelope,
}
