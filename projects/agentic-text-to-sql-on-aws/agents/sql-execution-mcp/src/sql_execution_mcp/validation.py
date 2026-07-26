"""SQL 검증기 — SQLGlot AST allow-list (READ-ONLY 4중 방어의 "LLM 밖 SQL AST validator").

문자열 매칭이 아니라 SQLGlot이 파싱한 AST 노드 타입을 검사한다. dialect는 파이프라인
생성 시 지정(기본 ``postgres``, Redshift 소스는 ``redshift``). 규칙 자체는 AST 노드 타입만
검사하므로 dialect 비의존적이며, dialect는 파싱·직렬화 단계에만 쓰인다.
추상 base class ``SqlValidationRule`` + 구현체들을 ``SqlValidationPipeline``이 순차 적용한다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

# 기본 dialect. Aurora PostgreSQL 및 Redshift(포스트그레 계열) 모두 파서를 공유하되,
# Redshift 전용 구문(UNLOAD 등)을 정확히 파싱하려면 파이프라인에 dialect="redshift"를 준다.
DIALECT = "postgres"

# 기본 LIMIT / 상한. 결과 폭주·과다 스캔 방어(§4.5 다층 안전장치 2).
DEFAULT_ROW_LIMIT = 200
MAX_ROW_LIMIT = 200

# 시스템 카탈로그 / 메타데이터 스키마 차단 (정보 노출·스키마 정찰 방지).
BLOCKED_SCHEMAS = frozenset({"pg_catalog", "information_schema", "pg_toast"})

# 접두사로 차단되는 시스템 테이블 (pg_tables, pg_stat_*, pg_shadow 등).
BLOCKED_TABLE_PREFIXES = ("pg_",)

# 위험한 함수(파일·네트워크·시스템 접근, 세션 조작). read-only여도 정보 노출/DoS 소지.
BLOCKED_FUNCTIONS = frozenset(
    {
        "pg_sleep",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
        "lo_import",
        "lo_export",
        "dblink",
        "dblink_exec",
        "copy_from_program",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
        "set_config",
        "current_setting",  # 세션·시크릿 설정 노출 가능
    }
)

# allow-list를 벗어난 최상위 statement 노드(쓰기·DDL·세션 제어 등).
_FORBIDDEN_STATEMENT_TYPES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.TruncateTable,
    exp.Copy,
    exp.Set,
    exp.Grant,
    exp.Command,  # VACUUM, EXPLAIN 등 sqlglot이 Command로 폴백하는 문장
)


class SqlRejected(Exception):
    """검증 규칙 위반. ``rule``(규칙 식별자)과 ``reason``(사람이 읽는 사유)을 담는다."""

    def __init__(self, rule: str, reason: str) -> None:
        super().__init__(reason)
        self.rule = rule
        self.reason = reason


@dataclass
class ValidationContext:
    """규칙 파이프라인이 공유하는 상태.

    ``statements``는 파싱된 모든 최상위 문장, ``root``는 검증 대상 단일 문장(가변 — LIMIT 주입).
    """

    raw_sql: str
    statements: list[exp.Expression] = field(default_factory=list)

    @property
    def root(self) -> exp.Expression | None:
        return self.statements[0] if self.statements else None

    def set_root(self, expression: exp.Expression) -> None:
        self.statements[0] = expression


class SqlValidationRule(ABC):
    """검증 규칙 추상 base class. 위반 시 ``SqlRejected``를 발생시키거나 AST를 변형한다."""

    #: 감사 로그·거부 응답에 쓰이는 안정적 규칙 식별자.
    rule_id: str = "unknown"

    @abstractmethod
    def apply(self, context: ValidationContext) -> None:
        """규칙을 적용한다. 위반이면 :class:`SqlRejected`를 raise."""
        raise NotImplementedError


class SingleStatementRule(SqlValidationRule):
    """단일 statement만 허용. 세미콜론으로 이은 다중 문장(스태킹) 차단."""

    rule_id = "single_statement"

    def apply(self, context: ValidationContext) -> None:
        count = len(context.statements)
        if count == 0:
            raise SqlRejected(self.rule_id, "빈 SQL이거나 파싱 가능한 문장이 없습니다.")
        if count > 1:
            raise SqlRejected(
                self.rule_id,
                f"다중 statement는 허용되지 않습니다(발견 {count}개). 단일 SELECT/WITH만 실행합니다.",  # noqa: E501
            )


class StatementTypeRule(SqlValidationRule):
    """최상위 노드가 SELECT / UNION / WITH(CTE) 계열인지 AST 타입으로 검사(allow-list)."""

    rule_id = "statement_type"

    def apply(self, context: ValidationContext) -> None:
        root = context.root
        assert root is not None  # SingleStatementRule 이후 보장

        # WITH는 sqlglot에서 최상위 Select/Union의 자식(args["with"])으로 파싱된다.
        # 따라서 최상위가 Query(Select/Union/Intersect/Except)이면 CTE 포함해 허용.
        if not isinstance(root, exp.Query):
            raise SqlRejected(
                self.rule_id,
                f"SELECT/WITH 문만 허용됩니다. 발견된 문장 유형: {type(root).__name__}.",
            )


class ForbiddenNodeRule(SqlValidationRule):
    """AST 전체를 순회하며 금지 노드(쓰기·DDL·SELECT INTO·위험 함수)를 탐지.

    ``WITH x AS (DELETE ...)`` 처럼 CTE/서브쿼리에 숨긴 우회를 잡는다.
    """

    rule_id = "forbidden_node"

    def apply(self, context: ValidationContext) -> None:
        root = context.root
        assert root is not None

        # 쓰기/DDL/세션 제어 노드가 어디(서브쿼리·CTE 포함)에든 있으면 거부.
        for node in root.walk():
            if isinstance(node, _FORBIDDEN_STATEMENT_TYPES) and node is not root:
                raise SqlRejected(
                    self.rule_id,
                    f"금지된 구문이 포함되어 있습니다: {type(node).__name__} "
                    "(CTE/서브쿼리에 숨긴 쓰기·DDL 시도 포함).",
                )

        # 최상위 노드 자체가 금지 유형인 경우(StatementTypeRule과 이중 방어).
        if isinstance(root, _FORBIDDEN_STATEMENT_TYPES):
            raise SqlRejected(
                self.rule_id,
                f"금지된 구문입니다: {type(root).__name__}.",
            )

        # SELECT ... INTO (테이블 생성/쓰기) 차단.
        if list(root.find_all(exp.Into)):
            raise SqlRejected(
                self.rule_id,
                "SELECT ... INTO (테이블 생성/쓰기)는 허용되지 않습니다.",
            )

        # 위험 함수 호출(Anonymous 노드로 파싱됨) 차단.
        for func in root.find_all(exp.Anonymous):
            name = (func.this or "").lower() if isinstance(func.this, str) else ""
            if name in BLOCKED_FUNCTIONS:
                raise SqlRejected(
                    self.rule_id,
                    f"허용되지 않는 함수 호출입니다: {name}().",
                )


class SystemCatalogRule(SqlValidationRule):
    """시스템 카탈로그/메타데이터 스키마·테이블 접근 차단(스키마 정찰·정보 노출 방지)."""

    rule_id = "system_catalog"

    def apply(self, context: ValidationContext) -> None:
        root = context.root
        assert root is not None

        for table in root.find_all(exp.Table):
            schema = (table.db or "").lower()
            name = (table.name or "").lower()

            if schema in BLOCKED_SCHEMAS:
                raise SqlRejected(
                    self.rule_id,
                    f"시스템 스키마 접근은 차단됩니다: {schema}.",
                )
            # 스키마 미지정이거나 명시된 경우 모두 테이블 접두사 검사.
            if any(name.startswith(prefix) for prefix in BLOCKED_TABLE_PREFIXES):
                raise SqlRejected(
                    self.rule_id,
                    f"시스템 카탈로그 테이블 접근은 차단됩니다: {name}.",
                )


class LimitInjectionRule(SqlValidationRule):
    """LIMIT이 없거나 상한을 초과하면 기본값으로 캡. AST를 변형(마지막에 적용)."""

    rule_id = "limit_injection"

    def __init__(
        self, default_limit: int = DEFAULT_ROW_LIMIT, max_limit: int = MAX_ROW_LIMIT
    ) -> None:
        self.default_limit = default_limit
        self.max_limit = max_limit

    def apply(self, context: ValidationContext) -> None:
        root = context.root
        assert root is not None

        # Query 계열이 아니면(도달하지 않아야 함) 건드리지 않는다.
        if not isinstance(root, exp.Query):
            return

        limit_node = root.args.get("limit")
        needs_cap = True
        if isinstance(limit_node, exp.Limit):
            expr = limit_node.expression
            # 리터럴 정수 LIMIT이고 상한 이하이면 그대로 둔다.
            if isinstance(expr, exp.Literal) and expr.is_number:
                try:
                    current = int(expr.name)
                except ValueError:
                    current = None
                if current is not None and current <= self.max_limit:
                    needs_cap = False

        if needs_cap:
            capped = root.limit(self.max_limit, copy=False)
            context.set_root(capped)


@dataclass
class ValidationResult:
    """검증 결과. ``ok=True``면 ``sql``이 실행 대상(LIMIT 주입 반영)."""

    ok: bool
    sql: str | None = None
    rule: str | None = None
    reason: str | None = None


class SqlValidationPipeline:
    """규칙들을 순차 적용하는 검증 파이프라인. 첫 위반에서 거부한다.

    ``dialect``는 SQLGlot 파싱·직렬화에 쓰인다(기본 ``postgres``). datasource별로
    별도 인스턴스를 두어 Aurora=postgres, Redshift=redshift dialect로 파싱한다.
    """

    def __init__(
        self,
        rules: list[SqlValidationRule] | None = None,
        dialect: str = DIALECT,
    ) -> None:
        self.dialect = dialect
        self.rules: list[SqlValidationRule] = rules if rules is not None else default_rules()

    def validate(self, sql: str) -> ValidationResult:
        try:
            parsed = [s for s in sqlglot.parse(sql, dialect=self.dialect) if s is not None]
        except Exception as exc:  # noqa: BLE001 — 파싱 실패는 거부로 정규화
            return ValidationResult(
                ok=False,
                rule="parse_error",
                reason=f"SQL 파싱 실패: {exc}",
            )

        context = ValidationContext(raw_sql=sql, statements=parsed)

        for rule in self.rules:
            try:
                rule.apply(context)
            except SqlRejected as rejection:
                return ValidationResult(ok=False, rule=rejection.rule, reason=rejection.reason)

        root = context.root
        assert root is not None
        return ValidationResult(ok=True, sql=root.sql(dialect=self.dialect))


def default_rules(
    default_limit: int = DEFAULT_ROW_LIMIT, max_limit: int = MAX_ROW_LIMIT
) -> list[SqlValidationRule]:
    """기본 규칙 순서: 단일문장 → 유형 → 금지노드 → 시스템카탈로그 → LIMIT 주입(마지막)."""
    return [
        SingleStatementRule(),
        StatementTypeRule(),
        ForbiddenNodeRule(),
        SystemCatalogRule(),
        LimitInjectionRule(default_limit=default_limit, max_limit=max_limit),
    ]
