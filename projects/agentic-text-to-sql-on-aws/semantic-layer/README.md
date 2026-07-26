# semantic-layer — semantic 지식 system-of-record & Neptune 동기화

agentic Text-to-SQL 솔루션의 **semantic layer**(비즈니스 용어·동의어·few-shot·
스키마 메타)를 관리하는 패키지입니다. 쓰기는 **DynamoDB 한 곳**에서만 하고
(dual-write 금지), 파생 저장소(Neptune 그래프)로는 **DynamoDB Streams** 를 통해
단방향 동기화합니다. (ARCHITECTURE §4.4 / §5.3)

> AWS에 실제 쓰기/동기화를 수행하는 실행(seed, Lambda 배포)은 배포 담당이 맡습니다.
> 이 패키지는 코드와 로컬 단위 테스트까지 제공합니다.

## 구성

```
semantic-layer/
├── src/semantic_layer/
│   ├── repository.py      # SemanticRepository: DynamoDB CRUD + 조건부 쓰기·버전 이력
│   ├── graph_sync.py      # record_to_cypher(순수) + NeptuneGraphClient(실행기)
│   ├── lambda_handler.py  # Streams 이벤트 핸들러(파샬 배치 응답, boto3 표준 런타임만)
│   └── seed_semantic.py   # 초기 seed 스크립트(sample-data schema 재사용)
└── tests/                 # fake 주입 단위 테스트(AWS 호출 없음)
```

## 데이터 모델

### DynamoDB (system-of-record) — 테이블 `agentic-t2sql-semantic`

| 키 | 의미 |
|---|---|
| `pk` = `{entity_type}#{entity_id}` | entity_type ∈ `term` \| `fewshot` \| `table` \| `column` \| `join` |
| `sk` = `v0` | 최신본 |
| `sk` = `v{n}` (n≥1) | 버전 이력 |

공통 속성: `entity_type`, `entity_id`, `status`(`candidate`\|`published`\|`rejected`),
`version`(N), `updated_at`(ISO8601), `updated_by`.

`rejected` 는 M5 additive(승인 큐 반려)입니다. `reject(entity_type, entity_id, reason)` 은
사유를 payload 의 `rejection_reason` 으로 남기고, `published` 가 아니므로 파생 저장소에는
노출되지 않습니다. 반려 후에도 `publish`(재승인)/`unpublish`(재검토 큐 복귀)가 가능합니다.

**쓰기 규칙**: `put_entity` 는 항상 `v0` 을 조건부로 갱신(version 증가)하고 직전 본을
`v{n}` 이력으로 복사합니다. 최초 생성은 `attribute_not_exists(pk)`, 갱신은
`version = <직전값>` 낙관적 잠금으로 동시성 충돌을 방어합니다. `status` 전환
(`publish`/`unpublish`/`reject`)도 `put_entity` 를 경유해 버전을 올립니다.

entity별 페이로드:

| entity_type | 페이로드 |
|---|---|
| `term` | `term`, `definition`, `synonyms`(L), `sql_fragment`, `maps_to`(L[{table,column}]), `embedding`(1024) |
| `fewshot` | `question`, `sql`, `embedding`(1024) |
| `table` | `table`, `description`, `ddl_snippet` |
| `column` | `table`, `column`, `data_type`, `description`, `ddl_snippet`, `references` |
| `join` | `left_table`, `right_table`, `join_on` |

`term`/`fewshot` 의 `embedding`(Titan Text Embeddings V2, 1024차원)은 **쓰기 시점**에
주입된 embedder 로 계산됩니다. `status` 전환 시에는 기존 임베딩을 보존해 재계산하지
않습니다.

### Neptune 그래프 (openCypher)

- 노드: `(:Table {name})`, `(:Column {name, table, key})`, `(:Term {name})`
- 엣지: `(Table)-[:HAS_COLUMN]->(Column)`, `(Table)-[:JOINS {on}]->(Table)`,
  `(Term)-[:MAPS_TO]->(Table|Column)`

**동기화 규칙** (`record_to_cypher`):

| Streams 레코드 | 동작 |
|---|---|
| `sk != v0` (이력) | 무시(빈 리스트) |
| INSERT/MODIFY, `status=published` | MERGE 멱등 upsert |
| INSERT/MODIFY, `status=candidate`\|`rejected` (강등 포함) | DETACH DELETE (에이전트 미노출) |
| REMOVE | DETACH DELETE(노드) / DELETE(join 엣지) |

`published` 이외(`candidate`/`rejected`)는 그래프에 반영하지 않는 것이 published/candidate
분리 방어선입니다.

## 실행 방법

### 1. 설치 및 테스트 (로컬)

```bash
cd semantic-layer
uv sync --extra dev
uv run pytest
uv run ruff check .
```

`--extra dev`(또는 `--extra seed`)는 `sample-data` 패키지를 path dependency 로
설치합니다(`schema.TABLES` 재사용). 런타임(Lambda) 의존성은 boto3 만 유지합니다.

### 2. 환경변수 준비

`.env.example` 를 참고해 값을 설정합니다(CDK semantic 스택 output 에서 획득).

```bash
export AWS_REGION=us-west-2
export SEMANTIC_TABLE_NAME=agentic-t2sql-semantic
export EMBEDDING_MODEL_ID=amazon.titan-embed-text-v2:0
```

### 3. 초기 seed (배포 담당자)

```bash
uv run seed-semantic
# 또는 인자로: uv run seed-semantic --table-name agentic-t2sql-semantic --region us-west-2
```

수행 내용(멱등, 재실행 안전 — 버전만 증가):
1. `sample_data.schema.TABLES` 에서 table/column/join 엔티티 파생 → `published`
2. 한국어 비즈니스 용어(term) 6개 — "최근 활성 고객", "매출", "VIP 고객", "수도권",
   "성수기"(published) + "이탈 위험 고객"(candidate, 분리 검증용)
3. few-shot(NLQ↔SELECT) 4개 — `published`
4. `term`/`fewshot` 은 쓰기 시점에 Titan v2 임베딩 계산 후 포함

### 4. Neptune 동기화 Lambda

`lambda_handler.handler` 를 DynamoDB Streams 트리거(ReportBatchItemFailures)에 연결합니다
(인프라는 CDK semantic 스택). 환경변수 `GRAPH_ENDPOINT` 로 Neptune 엔드포인트를 주입합니다.
실패한 레코드만 `batchItemFailures` 로 반환되어 재처리되며, MERGE 라 재처리 자체가 멱등입니다.

## 보안 참고

- **단일 쓰기 지점**: semantic 지식은 DynamoDB 만 씁니다(dual-write 금지 — 부분 실패 시
  영구 불일치 방지). 파생 인덱스는 원본에서 언제든 재구축(backfill) 가능합니다.
- **candidate/published 분리**: `candidate` 는 그래프에 동기화되지 않아 에이전트에
  노출되지 않습니다. 지식 오염(poisoning) 방어선(Well-Architected AGENTSEC01).
- **Lambda 최소 의존성**: 동기화 Lambda 는 boto3 표준 런타임만 사용합니다(외부 패키지 금지).
- 실물 시크릿/`.env` 는 커밋하지 않습니다(`.env.example` 만 커밋).

## 리소스 정리 (cleanup)

이 패키지는 리소스를 생성하지 않습니다(seed 실행 시 DynamoDB 항목·Neptune 노드 생성).
정리가 필요하면:

```bash
# DynamoDB 항목 삭제는 테이블 삭제로 일괄 처리(CDK 스택 cleanup).
# Neptune 그래프 초기화(openCypher):
#   MATCH (n) DETACH DELETE n
```

DynamoDB 테이블·Neptune 클러스터 자체는 CDK 스택(`infra/`) cleanup 으로 제거합니다.
