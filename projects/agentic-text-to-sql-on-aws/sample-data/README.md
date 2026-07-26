# sample-data — 샘플 데이터 & 스키마 인덱싱

agentic Text-to-SQL 솔루션의 **e-커머스 샘플 데이터** 생성기와 적재 스크립트입니다.
결정적(seed 고정) 데이터를 Aurora PostgreSQL(RDS Data API)에 적재하고,
스키마 메타데이터를 OpenSearch에 임베딩 인덱싱합니다.

> AWS에 실제 적재하는 실행은 배포 담당(Task #6)이 수행합니다. 이 패키지는 스크립트와 로컬 테스트까지 제공합니다.

## 구성

```
sample-data/
├── src/sample_data/
│   ├── schema.py            # e-커머스 스키마 정의(단일 진실 원천): DDL + COMMENT + 인덱스
│   ├── generator.py         # 결정적 데이터 생성기 (random.Random(42))
│   ├── dataapi.py           # RDS Data API 헬퍼(파라미터 변환, 트랜잭션/배치)
│   ├── seed_aurora.py       # Aurora 적재 스크립트 (+ read-only 사용자 agent_ro 생성)
│   ├── schema_docs.py       # 스키마 → semantic 검색 문서 빌더
│   └── index_schema_docs.py # OpenSearch 인덱싱(Titan v2 임베딩 + hybrid 파이프라인)
└── tests/                   # 결정성/DDL 문법/문서 빌더/AWS mock 테스트
```

## 스키마 (e-커머스)

| 테이블 | 설명 | 기본 행 수 |
|---|---|---|
| `categories` | 상품 카테고리 | 8 |
| `customers` | 고객(지역 분포, `last_login_at`으로 활성/비활성) | 1,000 |
| `products` | 상품(카테고리 FK, 가격) | 200 |
| `orders` | 주문(고객 FK, status, `ordered_at`, `total_amount`) | 10,000 |
| `order_items` | 주문 상세(order/product FK, quantity, unit_price) | ~21,100 |

모든 테이블·컬럼에 한국어 `COMMENT`를 부여했습니다(semantic layer의 원천).
데이터 분포는 자연어 질의 데모에 맞춰 설계했습니다.
- **지역별 매출**: 수도권(서울·경기) 집중 분포
- **최근 3개월 활성 고객**: 약 20%는 오래된 `last_login_at`(비활성)
- **카테고리별 인기 상품**: 카테고리·상품별 인기 가중으로 편차
- **기간별 매출**: 최근 24개월 시간 분포 + 연말 성수기 계절성

## 실행 방법

### 1. 설치 및 테스트 (로컬)

```bash
cd sample-data
uv sync --extra dev
uv run pytest
uv run ruff check .
```

### 2. 환경변수 준비

`.env.example`를 참고해 값을 설정합니다(CDK 스택 output에서 획득).

```bash
export AWS_REGION=us-west-2
export AURORA_CLUSTER_ARN=arn:aws:rds:us-west-2:...:cluster:...
export AURORA_SECRET_ARN=arn:aws:secretsmanager:us-west-2:...:secret:.../admin-...
export DB_NAME=ecommerce
export OPENSEARCH_ENDPOINT=https://search-...us-west-2.es.amazonaws.com
export OPENSEARCH_INDEX=t2sql-schema-docs
export EMBEDDING_MODEL_ID=amazon.titan-embed-text-v2:0
```

### 3. Aurora 적재 (배포 담당자)

```bash
uv run seed-aurora
# 또는 인자로 직접: uv run seed-aurora --cluster-arn ... --secret-arn ...
```

수행 순서(멱등, 재실행 안전):
1. DB `ecommerce` 생성(존재 시 skip)
2. 스키마 DDL 적용(`CREATE TABLE IF NOT EXISTS` + `COMMENT` + 인덱스)
3. 결정적 샘플 데이터 배치 적재(`BatchExecuteStatement`, 파라미터화, `ON CONFLICT DO NOTHING`)
4. read-only 사용자 `agent_ro` 생성 + **SELECT-only** grant
   - 비밀번호는 Secrets Manager 시크릿 `agentic-t2sql/aurora/agent-ro`에 저장(없으면 생성, 있으면 재사용)

### 4. OpenSearch 인덱싱 (배포 담당자)

```bash
uv run index-schema-docs
# 스키마만(예시 값 없이): uv run index-schema-docs --no-samples
```

- 인덱스 `t2sql-schema-docs`: `knn_vector`(1024차원, Titan v2) + text(BM25) 매핑
- hybrid 검색 파이프라인 `t2sql-hybrid-pipeline`(normalization-processor) 생성
- 테이블·컬럼 메타데이터(이름/타입/COMMENT/DDL 스니펫/예시 값)를 임베딩 후 upsert

## 보안 참고

- **read-only 강제**: `agent_ro`에는 `SELECT`만 grant(INSERT/UPDATE/DELETE 없음).
  `ALTER DEFAULT PRIVILEGES`로 향후 테이블에도 SELECT 자동 부여. READ-ONLY 4중 방어의 한 축입니다.
- **시크릿**: 비밀번호는 코드/로그에 노출하지 않고 Secrets Manager에서 생성·보관합니다.
  admin 자격증명은 스크립트에 전달되지 않고 RDS Data API가 `AURORA_SECRET_ARN`을 직접 참조합니다.
- 실물 시크릿/`.env`는 커밋하지 않습니다(`.env.example`만 커밋).

## 리소스 정리 (cleanup)

이 패키지는 리소스를 생성하지 않지만(스크립트 실행 시 DB 데이터/사용자/OpenSearch 문서 생성),
정리가 필요하면:

```bash
# OpenSearch 인덱스/파이프라인 삭제 (배포 담당 도구 또는 curl로)
#   DELETE /t2sql-schema-docs
#   DELETE /_search/pipeline/t2sql-hybrid-pipeline
# agent_ro 시크릿 삭제
aws secretsmanager delete-secret --secret-id agentic-t2sql/aurora/agent-ro \
  --force-delete-without-recovery --region us-west-2
```

Aurora 클러스터·OpenSearch 도메인 자체는 CDK 스택(`infra/`) cleanup으로 제거합니다.
