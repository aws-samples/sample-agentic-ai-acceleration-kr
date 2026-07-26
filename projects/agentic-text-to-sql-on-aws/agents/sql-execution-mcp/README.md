# sql-execution-mcp

AgentCore Runtime에 호스팅되는 MCP 서버. 자연어에서 생성된 SQL을 **검증**하고 **실행**한다.
READ-ONLY 4중 방어 중 **"LLM 밖 SQL AST validator(SQLGlot)"** 와 실행 계층을 담당한다.

## 도구

### `run_sql(sql: str, datasource: str = "aurora") -> dict`

SELECT/WITH(CTE) 전용 SQLGlot AST allow-list를 통과한 read-only 쿼리만 선택된 데이터소스
(read-only 자격증명)에서 실행한다.

- 성공: `{"status":"ok","columns":[...],"rows":[[...]],"row_count":N,"truncated":bool}`
- 거부: `{"status":"rejected","reason":"...","rule":"..."}`
- 실행오류: `{"status":"error","message":"..."}` (오케스트레이터 self-correction 루프가 `message` 사용)

#### datasource 라우팅 (M3)

`datasource`로 실행 대상을 선택한다. 기본값 `"aurora"`(기존 호출 하위호환).

| datasource | 대상 | 실행기 | 파싱 dialect |
|---|---|---|---|
| `aurora`(기본) | 운영 e-커머스 DB (Aurora PostgreSQL) | `AuroraDataApiExecutor` (RDS Data API, 동기) | `postgres` |
| `redshift` | 분석 웨어하우스 (Redshift Serverless, 동일 스키마) | `RedshiftDataApiExecutor` (Redshift Data API, 비동기 폴링) | `redshift` |

- datasource별 `(검증 파이프라인, 실행기)` 레지스트리로 라우팅한다. 응답 스키마는 동일.
- 알 수 없는 datasource → `{"status":"rejected","rule":"unknown_datasource","reason":...}`.
- Redshift env 미설정 시 `datasource="redshift"` 요청은 `{"status":"error","message":"Redshift 미구성: ..."}` (graceful — 크래시 없음).
- Redshift Data API는 **비동기**: `execute_statement`→ `describe_statement` 폴링(0.5s 간격,
  60s 타임아웃, 초과 시 `cancel_statement` 후 error)→ `get_statement_result`.
- 감사 로그에 `datasource` 필드가 추가된다.

#### 실행기 계층 (OOP: `BaseExecutor` base + 구현체)

추상 `BaseExecutor(execute(sql) -> ExecutionResult)` + 공통 `_truncate`(max_rows 계약).
구현체: `AuroraDataApiExecutor`(하위호환 alias `SqlExecutor`), `RedshiftDataApiExecutor`.
boto3 클라이언트·`sleep`은 주입 가능(AWS 호출 없는 fake 테스트).

## SQL 검증 규칙 (OOP: `SqlValidationRule` base + 구현체)

파이프라인이 아래 순서로 규칙을 적용한다. dialect는 datasource별 지정(aurora=`postgres`,
redshift=`redshift`), **문자열 매칭이 아닌 AST 타입 검사**. 규칙 자체는 AST 노드 타입만 보므로
dialect 비의존적이며, Redshift 전용 `UNLOAD`(→ `Command`로 폴백)·`COPY`(→ `exp.Copy`)도
`statement_type`/`forbidden_node`에서 거부된다.

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
| `DB_NAME` | Aurora 데이터베이스 이름 |
| `AWS_REGION` | 리전 (기본 `us-west-2`) |
| `REDSHIFT_WORKGROUP` | Redshift Serverless workgroup 이름 (`datasource="redshift"` 시 필요) |
| `REDSHIFT_DB` | Redshift 데이터베이스 이름 |
| `REDSHIFT_SECRET_ARN` | Redshift read-only 시크릿 ARN |

Redshift 3개 변수가 모두 설정돼야 `datasource="redshift"` 가 활성화된다(미설정 시 graceful error).
