# sql-execution-mcp

AgentCore Runtime에 호스팅되는 MCP 서버. 자연어에서 생성된 SQL을 **검증**하고 **실행**한다.
READ-ONLY 4중 방어 중 **"LLM 밖 SQL AST validator(SQLGlot)"** 와 실행 계층을 담당한다.

## 도구

### `run_sql(sql: str) -> dict`

SELECT/WITH(CTE) 전용 SQLGlot AST allow-list를 통과한 read-only 쿼리만 Aurora PostgreSQL
(RDS Data API, read-only 자격증명)에서 실행한다.

- 성공: `{"status":"ok","columns":[...],"rows":[[...]],"row_count":N,"truncated":bool}`
- 거부: `{"status":"rejected","reason":"...","rule":"..."}`
- 실행오류: `{"status":"error","message":"..."}` (오케스트레이터 self-correction 루프가 `message` 사용)

## SQL 검증 규칙 (OOP: `SqlValidationRule` base + 구현체)

파이프라인이 아래 순서로 규칙을 적용한다. dialect는 `postgres` 고정, **문자열 매칭이 아닌 AST 타입 검사**.

| 순서 | 규칙 (`rule_id`) | 내용 |
|---|---|---|
| 1 | `single_statement` | 단일 statement만 허용(세미콜론 스태킹 차단) |
| 2 | `statement_type` | 최상위 노드가 `SELECT`/`UNION`/`WITH`(CTE) 계열(`exp.Query`)인지 검사 |
| 3 | `forbidden_node` | AST 전체 순회 — 쓰기/DDL(INSERT/UPDATE/DELETE/MERGE/DROP/CREATE/ALTER/TRUNCATE/COPY/SET/GRANT), `Command` 폴백(VACUUM 등), `SELECT ... INTO`, 위험 함수(`pg_sleep`, `pg_read_file`, `lo_export` 등) 차단. CTE/서브쿼리에 숨긴 우회 포함 |
| 4 | `system_catalog` | `pg_catalog`/`information_schema`/`pg_toast` 스키마 및 `pg_*` 테이블 차단 |
| 5 | `limit_injection` | LIMIT 없거나 상한(200) 초과 시 200으로 캡(AST 변형) |

거부 시 stdout에 structured JSON 감사 로그(`{event:"sql_rejected", rule, reason, sql_hash, timestamp}`)를
남긴다(SQL 원문은 sha256 해시만 — 시크릿·민감정보 노출 방지). CloudWatch가 수집.

## AgentCore Runtime MCP 호스팅 규격 (2026-07 검증)

`FastMCP(host="0.0.0.0", stateless_http=True)` + `mcp.run(transport="streamable-http")`.
컨테이너는 `0.0.0.0:8000`에서 `POST /mcp`를 노출한다(stateless streamable-HTTP).

## 로컬 실행

```bash
uv sync
# 검증만 로컬에서 테스트하려면 실행 없이 pytest 사용(Data API는 mock)
uv run pytest
uv run ruff check .

# 서버 기동(실 DB 실행에는 아래 env 필요 — .env.example 참고)
export AURORA_CLUSTER_ARN=... AURORA_SECRET_ARN=... DB_NAME=... AWS_REGION=us-west-2
uv run python -m sql_execution_mcp.server
# → http://0.0.0.0:8000/mcp
```

## 환경 변수

`.env.example` 참고. 반드시 **read-only(agent_ro) 시크릿**을 사용한다.

| 변수 | 설명 |
|---|---|
| `AURORA_CLUSTER_ARN` | Aurora 클러스터 ARN |
| `AURORA_SECRET_ARN` | Secrets Manager 시크릿 ARN (read-only `agent_ro`) |
| `DB_NAME` | 데이터베이스 이름 |
| `AWS_REGION` | 리전 (기본 `us-west-2`) |
