# datasource-admin-mcp

AgentCore Runtime에 호스팅되는 **관리 평면(Admin/Manager) MCP 서버**. admin panel이
semantic 큐레이션·승인과 데이터소스 등록/점검/스키마 크롤을 이 서버를 통해서만 수행한다.

## 왜 MCP 서버인가 (쓰기 경로 단일화)

admin web API는 DynamoDB를 **직접 쓰지 않는다**. 모든 큐레이션·승인·데이터소스 작업은
`사용자 JWT Bearer → Gateway MCP → datasource-admin-mcp → SemanticRepository` 경로다.

1. DynamoDB **단일 쓰기 지점** 유지 (dual-write 금지 — docs/architecture.md §4.4)
2. **Cedar**가 Manager/Admin 인가를 도구 평면에서 강제
3. **사용자별 JWT On-Behalf-Of** 전파를 admin 경로에서 실현

## 도구 (10종)

모든 도구는 JSON dict를 반환하고, 실패는 `{"status":"error","message":"타입: 메시지"}`로
정규화한다. `actor`는 감사 기록용(admin web이 JWT username을 전달).

| 도구 | 시그니처 | 반환 |
|---|---|---|
| `list_entities` | `(entity_type=None, status=None)` | `{"status":"ok","entities":[...]}` |
| `get_entity` | `(entity_type, entity_id)` | `{"status":"ok","entity":{...}\|None}` |
| `put_entity` | `(entity_type, entity_id, payload, status="candidate", actor="admin-panel")` | `{"status":"ok","entity":{...}}` |
| `publish_entity` | `(entity_type, entity_id, actor="admin-panel")` | `{"status":"ok","entity":{...}}` |
| `unpublish_entity` | `(entity_type, entity_id, actor="admin-panel")` | `{"status":"ok","entity":{...}}` |
| `reject_entity` | `(entity_type, entity_id, reason="", actor="admin-panel")` | `{"status":"ok","entity":{...}}` |
| `mine_candidates` | `(hours=24, actor="mining-batch")` | `{"status":"ok","scanned":N,"mined":N,"skipped_existing":N,"candidates":[...]}` |
| `register_datasource` | `(datasource_id, engine, config, actor="admin-panel")` | `{"status":"ok","secret_arn":"..."}` |
| `test_datasource` | `(datasource_id)` | `{"status":"ok","ok":bool,"detail":"..."}` |
| `crawl_schema` | `(datasource_id, actor="admin-panel")` | `{"status":"ok","tables":N,"columns":N,"joins":N}` |

> **하위호환 규칙**: 위 도구명·인자명과 응답 필드는 **제거·개명하지 않는다**. 확장은 신규
> 도구/신규 필드 추가(additive only)로만 하고, 신규 인자는 기본값을 반드시 둔다 — 기존
> 소비자(admin web·배치)가 무시해도 안전해야 한다. 도구를 추가하면 Gateway target 을
> 재동기화해야 `tools/list` 에 노출된다(CFN 변경이 없어 자동 동기화되지 않는다).

- `entity_type` 유효값: `term | fewshot | table | column | join | datasource`
  (`datasource`는 `SemanticRepository.VALID_ENTITY_TYPES` 에 정의된다).
- `list_entities`/`get_entity`/`put_entity` 응답에서 **`embedding` 필드는 제거**된다(payload 경량화).
- `term`/`fewshot` 쓰기는 Titan Text Embeddings V2(1024차원)로 임베딩을 계산해 저장한다 —
  파생 OpenSearch 인덱스의 `knn_vector` 매핑과 차원이 일치해야 kNN 질의가 성립한다.
- 크롤 산출물은 항상 **candidate**다. Manager 승인(`publish_entity`) 후에야 Streams →
  OpenSearch/Neptune으로 전파되어 검색에 반영된다.
- `status` 유효값: `candidate | published | rejected` (`rejected`는
  `reject_entity`가 사유를 payload `rejection_reason`으로 남긴다. `published`가 아니므로
  파생 저장소에 노출되지 않고, 이후 `publish_entity`로 재승인 / `unpublish_entity`로
  재검토 큐 복귀가 가능하다).

## 후보 채굴 (개선 파이프라인 Track B)

`mine_candidates`는 orchestrator가 실행 종료 시 남기는 구조화 로그
`t2sql_query_record {JSON}`(`{question, sql, status, session_id, version}`)을 CloudWatch
Logs에서 읽어 semantic 후보를 **candidate**로 적재한다(승인 게이트 유지).

| 후보 | 원천 | entity_id | payload |
|---|---|---|---|
| `fewshot` | `status="ok"` + SQL 존재 | `mined-<sha256(정규화 질문)[:12]>` | `question`, `sql`, `source="mined"`, `mined_from_session` |
| `term` | `status="error"\|"clarification"` 질문에서 2회 이상 등장한 비스톱워드 토큰 | `mined-term-<sha256(토큰)[:12]>` | `term`, `definition`(정의 필요 플레이스홀더), `synonyms=[]`, `source="mined"` |

- 로그 그룹은 env `ORCHESTRATOR_LOG_GROUP_PREFIX`(기본 `/aws/bedrock-agentcore/runtimes/`)
  하위에서 이름에 `orchestrator`가 포함된 그룹만 스캔한다(`DescribeLogGroups` +
  `FilterLogEvents(filterPattern="t2sql_query_record")`).
- **중복 채굴 방지**: put 전에 `get_entity`로 동일 entity_id 존재를 확인해 **status 무관
  (`rejected` 포함)** 이면 skip하고 `skipped_existing`을 올린다 — 반려한 후보가 배치
  재실행으로 되살아나지 않는다.
- 응답의 `candidates` 목록은 최대 50건까지만 실린다(카운트는 전체값).
- 한 그룹의 `FilterLogEvents` 실패(권한 등)는 warning만 남기고 다른 그룹을 계속 스캔한다.

## 데이터소스 관리

| 구분 | 저장 위치 |
|---|---|
| 자격증명 포함 연결 설정 | Secrets Manager `agentic-t2sql/datasource/<datasource_id>` |
| 연결 메타(자격증명 제외) | DynamoDB `entity_type="datasource"` (candidate) |

`register_datasource`는 시크릿을 신규 생성(`create_secret`)하거나 이미 있으면 새 버전을
기록(`put_secret_value`)한다 — 재실행 안전. `password`/`secret`/`token`/`private_key`/
`credentials` 키는 DynamoDB 메타에서 제거된다. 시크릿 **값은 로깅·응답에 절대 실지 않는다.**

`test_datasource`:
- 내장 소스(`aurora`/`redshift`) → Data API로 `SELECT 1` 실제 실행
- 등록된 커스텀 소스 → 시크릿 존재 + 필수 키(`host`/`database`/`username`/`password`) 검증까지만.
  이 runtime은 `PUBLIC` 네트워크 모드라 임의 DB로 직접 연결할 수 없다(키 이름만 노출).

## 커넥터 설계 (OOP)

복잡한 부분은 추상 base + 구현체로 분리한다(레포 원칙).

| 클래스 | 역할 |
|---|---|
| `DatasourceConnector` | 추상 base — `run_query()`만 구현하면 `test_connection()`/`crawl()`을 공유 |
| `AuroraDataApiConnector` | rds-data `ExecuteStatement`(동기), Aurora PostgreSQL |
| `RedshiftDataApiConnector` | redshift-data(비동기: execute → describe 폴링 → get_result) |
| `SchemaCrawler` | 크롤 결과를 `put_entity(status="candidate")`로 적재 |
| `DatasourceRegistry` | Secrets Manager 기반 등록 저장소 |
| `CandidateMiner` | CloudWatch Logs 레코드 → fewshot/term 후보를 `put_entity(status="candidate")`로 적재 |

- 크롤 질의는 `information_schema.tables/columns` + FK(`table_constraints` 조인) 3개.
  Redshift는 PostgreSQL 8.0.2 기반이라 동일 질의를 쓴다. FK 질의 실패는 warning만 남기고
  `joins`를 비운 채 크롤을 계속한다(엔진별 제약 메타 차이 방어).
- **Redshift Data API는 `SecretArn`이 필수**다. 생략하면 IAM 매핑 사용자로 실행돼
  `information_schema` 조회조차 권한 부족으로 실패한다(배포 실측).
- Data API 호출 패턴은 `sql-execution-mcp/executor.py`와 동형이지만 **의도적으로 복사**했다 —
  두 런타임 이미지는 서로 독립이어야 하므로 패키지 간 의존을 만들지 않는다.

## AgentCore Runtime MCP 호스팅 규격 (2026-07 검증)

`FastMCP(host="0.0.0.0", stateless_http=True)` + `mcp.run(transport="streamable-http")`.
컨테이너는 `0.0.0.0:8000`에서 `POST /mcp`를 노출한다(stateless streamable-HTTP).

## 로컬 실행

```bash
cd agents/datasource-admin-mcp
uv sync --extra dev
uv run pytest
uv run ruff check .

# 실 실행은 AWS 자격증명 + env 필요 (.env.example 참고)
uv run python -m datasource_admin_mcp.server
# → http://0.0.0.0:8000/mcp
```

## 컨테이너 빌드 (⚠️ 컨텍스트 = 레포 루트)

`semantic-layer`를 로컬 경로 의존성으로 포함하므로 빌드 컨텍스트가 **레포 루트**여야 한다.

```bash
scripts/build-and-push.sh datasource-admin-mcp
# 내부적으로: <engine> build --platform linux/arm64 \
#   -f agents/datasource-admin-mcp/Dockerfile -t <ecr>/agentic-t2sql/datasource-admin-mcp:latest .
```

## 환경 변수

`.env.example` 참고.

| 변수 | 설명 |
|---|---|
| `SEMANTIC_TABLE_NAME` | semantic DynamoDB 테이블명 (필수) |
| `EMBEDDING_MODEL_ID` | 임베딩 모델 (기본 `amazon.titan-embed-text-v2:0`) |
| `AURORA_CLUSTER_ARN` / `AURORA_SECRET_ARN` / `DB_NAME` | Aurora 크롤·점검 (read-only 시크릿) |
| `REDSHIFT_WORKGROUP` / `REDSHIFT_DB` / `REDSHIFT_SECRET_ARN` | Redshift 크롤·점검 |
| `DATASOURCE_SECRET_PREFIX` | 등록 시크릿 prefix (기본 `agentic-t2sql/datasource/`) |
| `ORCHESTRATOR_LOG_GROUP_PREFIX` | 후보 채굴이 스캔할 로그 그룹 prefix (기본 `/aws/bedrock-agentcore/runtimes/`) |
| `AWS_REGION` | 리전 (기본 `us-west-2`) |

## IAM 최소 권한 (`agentic-t2sql-admin-mcp-role`)

- DynamoDB: semantic 테이블 `GetItem`/`PutItem`/`Scan`
- Bedrock: `InvokeModel` (Titan embed 모델만)
- Secrets Manager: `agentic-t2sql/datasource/*` 에 `CreateSecret`/`PutSecretValue`/
  `DescribeSecret`/`GetSecretValue` + Aurora/Redshift read-only 시크릿 `GetSecretValue`
- rds-data `ExecuteStatement` (Aurora 클러스터 한정), redshift-data
  `ExecuteStatement`/`DescribeStatement`/`GetStatementResult`/`CancelStatement`
- CloudWatch Logs: `DescribeLogGroups` + `FilterLogEvents`
  (`/aws/bedrock-agentcore/runtimes/*` 한정 — 후보 채굴)
