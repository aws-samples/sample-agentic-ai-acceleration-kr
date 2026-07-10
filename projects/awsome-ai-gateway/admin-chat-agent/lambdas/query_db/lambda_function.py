# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""query_db Lambda — admin-chat-agent's read-only SQL execution tool.

검증 단계 (Layer A — docs/admin-chat-agent-spec.md §2.5 / §3.1):
  1. sqlglot.parse(sql, dialect='postgres') — 파싱 실패 시 에러 피드백
  2. AST 검증 — DDL/DML 거부 (allowed: SELECT, WITH 만)
  3. 모든 참조 테이블 화이트리스트 확인
  4. EXPLAIN (FORMAT JSON) 으로 비용 추정 — total_cost > 50000 reject
  5. 실행 — read-only role 'gateway_chat_reader', statement_timeout=10s,
     LIMIT 1000 강제 wrap
  6. 결과 → S3 staging Parquet 업로드 (Code Specialist 가 read)
  7. sample 20행 + 메타만 LLM 에 반환 (INLINE_ROWS)

호출 입력:
  { "sql": str, "session_id": str, "step_id": str }
호출 출력:
  { "ok": True,
    "rows": [...최대 20행],
    "row_count": int,
    "columns": [{"name": str, "type": str}],
    "s3_uri": "s3://...",
    "explain_cost": float,
    "elapsed_ms": int }

  실패 시:
  { "ok": False, "error": str, "hint": str | None }
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any

import boto3
import psycopg2
import psycopg2.extras
import sqlglot
import sqlglot.expressions as exp
import yaml

try:
    import sql_guard  # L0/L1 결정적 정확도 안전망 (DEVLOG §58)
except ImportError:  # pragma: no cover — guard 부재 시 기존 동작 유지(비파괴)
    sql_guard = None

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# ─── Config ───
DB_HOST = os.environ["DB_HOST"]
DB_NAME = os.environ.get("DB_NAME", "gateway")
DB_USER = os.environ.get("DB_USER", "gateway_chat_reader")
DB_SECRET_ARN = os.environ["DB_SECRET_ARN"]
S3_STAGING_BUCKET = os.environ["S3_STAGING_BUCKET"]
WHITELIST_PATH = os.environ.get(
    "SCHEMA_WHITELIST_PATH",
    "/var/task/schema_whitelist.yaml",
)
EXPLAIN_COST_LIMIT = float(os.environ.get("EXPLAIN_COST_LIMIT", "50000"))
QUERY_LIMIT = int(os.environ.get("QUERY_LIMIT", "1000"))
# LLM 에 인라인 반환하는 sample 행수. "top 10/20" 류 질의가 잘리지 않게 20.
INLINE_ROWS = int(os.environ.get("INLINE_ROWS", "20"))
STATEMENT_TIMEOUT_MS = int(os.environ.get("STATEMENT_TIMEOUT_MS", "10000"))


secrets = boto3.client("secretsmanager")
s3 = boto3.client("s3")


# ─── Whitelist (lazy load, cached) ───
_whitelist_cache: dict[str, Any] | None = None


def get_whitelist() -> dict[str, Any]:
    global _whitelist_cache
    if _whitelist_cache is None:
        with open(WHITELIST_PATH, encoding="utf-8") as f:
            _whitelist_cache = yaml.safe_load(f)
    return _whitelist_cache


def allowed_table_set() -> set[tuple[str, str]]:
    """{(schema, table)} set."""
    wl = get_whitelist()
    return {(t["schema"], t["table"]) for t in wl.get("allowed_tables", [])}


def forbidden_columns() -> set[str]:
    """{'schema.table.column'} set."""
    return set(get_whitelist().get("forbidden_columns", []))


# ─── Validation ───
class ValidationError(Exception):
    """SQL 검증 실패. message 가 LLM 으로 전달되어 self-correction trigger."""


def validate_sql(sql: str) -> sqlglot.Expression:
    """1, 2, 3 단계 검증 — sqlglot AST + 화이트리스트."""
    # 1. parse
    try:
        ast = sqlglot.parse_one(sql, dialect="postgres")
    except Exception as e:
        raise ValidationError(f"SQL parse error: {e}") from e

    if ast is None:
        raise ValidationError("Empty SQL")

    # 2. 허용된 statement type 만 — SELECT 또는 WITH (CTE)
    if not isinstance(ast, (exp.Select, exp.With, exp.Subquery)):
        # WITH ... SELECT 도 sqlglot 에선 With 또는 Select 로 root
        if ast.find(exp.Insert) or ast.find(exp.Update) or ast.find(exp.Delete):
            raise ValidationError(
                "DDL/DML not allowed (INSERT/UPDATE/DELETE/DROP). "
                "Only SELECT statements are permitted."
            )

    # forbidden operation 추가 검사 (CREATE, DROP, GRANT 등)
    for op_name in ("Create", "Drop", "AlterTable", "Truncate", "Grant"):
        op_cls = getattr(exp, op_name, None)
        if op_cls and ast.find(op_cls):
            raise ValidationError(f"{op_name} statement not allowed")

    # 3. 모든 참조 테이블이 화이트리스트에 있는지
    allowed = allowed_table_set()
    for table in ast.find_all(exp.Table):
        # table.text("db") returns the schema as a plain string ("" if absent);
        # table.args.get("db") returns an Identifier object → no .lower(). Use text().
        schema = (table.text("db") or "public").lower()
        name = (table.name or "").lower()
        if (schema, name) not in {(s.lower(), t.lower()) for s, t in allowed}:
            raise ValidationError(
                f"Table '{schema}.{name}' is not in the whitelist. "
                f"Allowed tables: {sorted(allowed)}"
            )

    # forbidden column — 컬럼 이름이 forbidden 에 매치되면 reject
    forbidden = forbidden_columns()
    if forbidden:
        sql_lower = sql.lower()
        for fcol in forbidden:
            # 'auth.users.password_hash' — 대략적 substring 매치
            if fcol.lower() in sql_lower or fcol.split(".")[-1].lower() in sql_lower:
                # 컬럼 이름만 매치되는 false positive 방지: identifier 단위로 확인
                for col in ast.find_all(exp.Column):
                    full = ".".join(p for p in [col.text("db"), col.text("table"), col.name] if p).lower()
                    if full == fcol.lower():
                        raise ValidationError(f"Column '{fcol}' is forbidden")

    return ast


def force_limit(ast: sqlglot.Expression) -> str:
    """4단계 — LIMIT 강제. 이미 LIMIT 있으면 작은 값 채택."""
    select = ast if isinstance(ast, exp.Select) else ast.find(exp.Select)
    if select is None:
        return ast.sql(dialect="postgres")

    existing_limit = select.args.get("limit")
    if existing_limit:
        try:
            existing = int(existing_limit.expression.this)
            if existing > QUERY_LIMIT:
                select.set("limit", exp.Limit(expression=exp.Literal.number(QUERY_LIMIT)))
        except Exception:
            select.set("limit", exp.Limit(expression=exp.Literal.number(QUERY_LIMIT)))
    else:
        select.set("limit", exp.Limit(expression=exp.Literal.number(QUERY_LIMIT)))

    return ast.sql(dialect="postgres")


# ─── DB connection ───
def get_db_connection():
    secret = secrets.get_secret_value(SecretId=DB_SECRET_ARN)
    secret_data = json.loads(secret["SecretString"])
    conn = psycopg2.connect(
        host=DB_HOST,
        dbname=DB_NAME,
        user=DB_USER,
        password=secret_data["password"],
        connect_timeout=5,
        sslmode="require",
        # NOTE: do NOT pass libpq `options=-c ...` — RDS Proxy rejects command-line
        # options (it multiplexes connections). Set statement_timeout via SET below.
        # The gateway_chat_reader role also has statement_timeout=10s at role level.
    )
    with conn.cursor() as cur:
        cur.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
    return conn


def explain_cost(conn, sql: str) -> float:
    """5단계 — EXPLAIN 으로 비용 추정."""
    with conn.cursor() as cur:
        cur.execute(f"EXPLAIN (FORMAT JSON) {sql}")
        row = cur.fetchone()
        if not row:
            return 0.0
        plan = row[0][0]["Plan"]
        return float(plan.get("Total Cost", 0))


def _json_safe(value: Any) -> Any:
    """PG 값을 JSON 직렬화 가능한 형태로 정규화.

    Lambda 런타임은 핸들러 반환을 자체 json.dumps(default 없음)로 마샬링하므로
    datetime/date/Decimal/UUID/bytes 등이 남으면 Runtime.MarshalError 로 죽는다
    (시계열 use case 가 requested_at/day 컬럼 때문에 광범위하게 실패하던 원인).
      - Decimal → float (cost_usd 등 수치 의미 보존)
      - datetime/date/time → ISO 문자열
      - UUID → 문자열
      - bytes/memoryview → (노출 안 되게) repr 길이만
    """
    import datetime as _dt
    import decimal as _dec
    import uuid as _uuid

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, _dec.Decimal):
        return float(value)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, _uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary {len(bytes(value))}B>"
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return str(value)


def execute_and_collect(conn, sql: str) -> tuple[list[dict], list[dict]]:
    """6단계 — 실행 + 결과 + 컬럼 메타 반환. 행은 JSON-safe 로 정규화."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        rows = cur.fetchall()
        columns = [
            {"name": col.name, "type_oid": col.type_code}
            for col in (cur.description or [])
        ]
    return [_json_safe(dict(r)) for r in rows], columns


def compute_stats(rows: list[dict]) -> dict | None:
    """숫자 컬럼별 결정적 요약(min/max/mean/sum) + 단일 숫자컬럼이면 행별 비중%.

    LLM 이 인사이트 헤드라인("X 가 전체의 35%")을 지어내지 않고 **이 결정적
    필드를 인용**하게 한다(§55 insight-first — deep-insight _df_summary 패턴).
    전체 rows(LIMIT 적용분) 기준이라 인라인 샘플(INLINE_ROWS)보다 정확.
    LLM 비용 0 — 순수 Python.
    """
    if not rows:
        return None
    numeric_cols: dict[str, list[float]] = {}
    for col, val in rows[0].items():
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            numeric_cols[col] = []
    if not numeric_cols:
        return None
    for r in rows:
        for col in numeric_cols:
            v = r.get(col)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                numeric_cols[col].append(float(v))
    stats: dict[str, Any] = {}
    for col, vals in numeric_cols.items():
        if not vals:
            continue
        total = sum(vals)
        stats[col] = {
            "min": min(vals),
            "max": max(vals),
            "mean": round(total / len(vals), 6),
            "sum": round(total, 6),
            "count": len(vals),
        }
    # 숫자 컬럼이 1개고 행수가 적당하면(≤20) 행별 비중% — "top N 점유율" 인용용.
    if len(stats) == 1:
        col = next(iter(stats))
        total = stats[col]["sum"]
        if total and len(rows) <= 20:
            label_col = next(
                (c for c in rows[0] if c != col), None
            )
            if label_col:
                stats[col]["share_pct"] = [
                    {
                        "label": str(r.get(label_col)),
                        "pct": round(float(r.get(col, 0) or 0) / total * 100, 2),
                    }
                    for r in rows
                ]
    return stats or None


def assert_resultset(rows: list[dict], columns: list[dict], stats: dict | None) -> list[str]:
    """L1 — 결과셋 결정적 어서션(LLM 추가호출 0, DEVLOG §58).

    실행된 결과셋에 결정적 술어를 걸어 '조용히 틀린 숫자'의 신호를 잡는다.
    _reconcile_numbers 는 본문 숫자가 envelope 유래인지만 보지만(환각만 탐지),
    이 어서션은 '결과셋 자체가 말이 되는가'를 본다 — fan-out(②)·필터누락(⑧)·
    정렬깨짐 같은 의미오류의 실행기측 신호. 위반은 warnings 로 fail-soft.

    어서션은 결정적·검증가능한 술어만(부호/단조성/NULL/정렬) — LLM 환각 위험 0.
    """
    warnings: list[str] = []
    if not rows:
        return warnings

    numeric_names = set((stats or {}).keys())

    # 1) measure 음수 — 비용/토큰/지연은 음수 불가(데이터/집계 오류 신호)
    for col in numeric_names:
        s = stats[col]
        nonneg_hint = any(k in col.lower() for k in ("cost", "token", "calls", "count", "latency", "requests"))
        if nonneg_hint and s.get("min", 0) < 0:
            warnings.append(
                f"결과셋 이상: `{col}` 에 음수({s['min']}) — 비용/토큰/건수는 음수 불가. "
                f"집계나 조인이 잘못됐을 수 있음."
            )

    # 2) 정렬 단조성 — ORDER BY DESC 류 ranking 의도인데 첫 숫자 컬럼이 비단조면
    #    상위 N 순위가 깨진 신호(잘못된 GROUP BY/중복행).
    if numeric_names and len(rows) >= 3:
        first_num = next(iter(numeric_names))
        vals = [r.get(first_num) for r in rows if isinstance(r.get(first_num), (int, float))]
        if len(vals) >= 3:
            desc = all(vals[i] >= vals[i + 1] for i in range(len(vals) - 1))
            asc = all(vals[i] <= vals[i + 1] for i in range(len(vals) - 1))
            if not (desc or asc):
                # 정렬 자체가 없을 수도 있으니 약한 신호로만(WARN)
                warnings.append(
                    f"결과셋 주의: `{first_num}` 가 단조 정렬이 아님 — ranking 질의라면 "
                    f"ORDER BY 누락/중복행 가능성(결과 순서 확인)."
                )

    # 3) 단일 행 + ranking 의심: row_count==1 인데 여러 라벨 컬럼 → 과집계 신호는
    #    상위(sql_specialist note)에서 다루므로 여기선 생략(중복 경고 방지).
    return warnings


# ─── S3 staging ───
def upload_to_staging(rows: list[dict], session_id: str, step_id: str) -> str:
    """7단계 — Parquet 업로드. 큰 결과를 Code Specialist 에게 전달."""
    # MVP: 일단 JSON Lines 로 (parquet 은 pyarrow layer 필요)
    body = "\n".join(json.dumps(r, default=str) for r in rows).encode("utf-8")
    # staging/ prefix — 중간 데이터(1일 만료). reports/(7일)와 키 공간 분리해
    # lifecycle 을 prefix 별로 독립 제어(§49). 과거 평면 키({session}/...)는 broad
    # 규칙이 reports 까지 1일에 삭제하는 문제가 있었음.
    key = f"staging/{session_id}/{step_id}.jsonl"
    s3.put_object(
        Bucket=S3_STAGING_BUCKET,
        Key=key,
        Body=body,
        ContentType="application/x-ndjson",
        ServerSideEncryption="aws:kms",
    )
    return f"s3://{S3_STAGING_BUCKET}/{key}"


# ─── Lambda entrypoint ───
def lambda_handler(event: dict, context: Any) -> dict:
    sql = event.get("sql", "").strip()
    session_id = event.get("session_id") or str(uuid.uuid4())
    step_id = event.get("step_id") or str(uuid.uuid4())

    if not sql:
        return {"ok": False, "error": "SQL is required"}

    started = time.perf_counter()

    try:
        # 1, 2, 3 — AST validation
        ast = validate_sql(sql)

        # L0 — 결정적 정확도 가드(컬럼 해석/타임존/fan-out/대시보드 정합, §58).
        # errors 는 reject(self-correction 피드백), warnings 는 결과에 동봉.
        accuracy_warnings: list[str] = []
        if sql_guard is not None:
            try:
                g = sql_guard.guard(sql, get_whitelist())
                if g.get("errors"):
                    return {
                        "ok": False,
                        "error": "; ".join(g["errors"]),
                        "hint": "Fix the SQL per the validation message and retry.",
                    }
                accuracy_warnings.extend(g.get("warnings", []))
            except Exception as e:  # noqa: BLE001 — 가드 자체 오류가 본 경로 막지 않게
                logger.warning(f"sql_guard failed (non-fatal): {e}")

        # 4 — LIMIT 강제
        bounded_sql = force_limit(ast)

        with get_db_connection() as conn:
            # 5 — EXPLAIN
            cost = explain_cost(conn, bounded_sql)
            if cost > EXPLAIN_COST_LIMIT:
                return {
                    "ok": False,
                    "error": f"Estimated cost {cost:.0f} exceeds limit {EXPLAIN_COST_LIMIT:.0f}",
                    "hint": "Add WHERE clauses to narrow the query, or aggregate further.",
                }

            # 6 — 실행
            rows, columns = execute_and_collect(conn, bounded_sql)

        elapsed_ms = int((time.perf_counter() - started) * 1000)

        # L1 — 결과셋 결정적 어서션(§58). compute_stats 결과 위에 술어 평가.
        result_stats = compute_stats(rows)
        try:
            accuracy_warnings.extend(assert_resultset(rows, columns, result_stats))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"assert_resultset failed (non-fatal): {e}")

        # 7 — S3 staging (행이 많을 때만 가치, 인라인 한도 이하면 inline)
        s3_uri: str | None = None
        if len(rows) > INLINE_ROWS:
            try:
                s3_uri = upload_to_staging(rows, session_id, step_id)
            except Exception as e:
                logger.warning(f"S3 staging failed: {e}")
                # graceful — staging 실패해도 sample 결과는 반환

        return {
            "ok": True,
            "sql": bounded_sql,
            # 인라인 sample 행수. 5행이던 것을 20행으로 — "top 10" 류 질의가 5건만
            # 표시되던 문제(§52) 해소. 20행 초과분은 s3_uri 로(Code Specialist 용).
            "rows": rows[:INLINE_ROWS],
            "row_count": len(rows),
            "columns": columns,
            # 결정적 숫자 요약(§55 insight-first) — LLM 이 비중%/합계를 지어내지
            # 않고 이 필드를 인용. 전체 rows 기준이라 샘플 추론보다 정확.
            "stats": result_stats,
            # L0/L1 결정적 정확도 경고(§58) — fail-soft. orchestrator/validator 가
            # 이 신호를 보고 재생성/검증 강화, deep 모드는 스트리밍으로 노출.
            "accuracy_warnings": accuracy_warnings,
            "s3_uri": s3_uri,
            "explain_cost": cost,
            "elapsed_ms": elapsed_ms,
        }

    except ValidationError as e:
        return {"ok": False, "error": str(e), "hint": "Fix the SQL and retry."}
    except psycopg2.errors.QueryCanceled:
        return {
            "ok": False,
            "error": f"Query timed out after {STATEMENT_TIMEOUT_MS}ms",
            "hint": "Add WHERE filters to reduce scan size.",
        }
    except Exception as e:
        logger.exception("query_db unexpected error")
        return {"ok": False, "error": f"Unexpected error: {type(e).__name__}: {e}"}
