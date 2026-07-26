"""semantic layer 초기 seed 스크립트.

sample-data 패키지의 ``schema.TABLES`` (단일 진실 원천)에서 table/column/join
엔티티를 파생하고, 한국어 비즈니스 용어(term)와 few-shot(NLQ↔SQL) 예시를
DynamoDB 에 적재한다. 멱등(재실행 안전) — ``put_entity`` 가 버전만 올린다.

table/column/join 은 published 로, term/fewshot 은 대부분 published 로 넣되
published/candidate 분리 검증을 위해 candidate 예시 1개를 포함한다.

환경변수: SEMANTIC_TABLE_NAME, AWS_REGION(기본 us-west-2),
  EMBEDDING_MODEL_ID(기본 amazon.titan-embed-text-v2:0).

주의: 이 스크립트는 실제 DynamoDB 쓰기와 Bedrock 임베딩 호출을 수행한다.
실행은 배포 담당이 맡는다(로컬 단위 테스트는 fake 를 주입한다).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from typing import Any

from .repository import SemanticRepository

DEFAULT_REGION = "us-west-2"
DEFAULT_EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"
EMBEDDING_DIM = 1024


# --- 스키마 파생 엔티티 (sample-data schema.TABLES 재사용) --------------------


def _load_tables():
    """sample_data.schema.TABLES 를 임포트(seed extra 필요)."""
    try:
        from sample_data import schema
    except ModuleNotFoundError as exc:  # pragma: no cover - 의존성 누락 안내
        raise RuntimeError(
            "sample-data 패키지가 필요하다. `uv sync --extra seed`(또는 --extra dev) 로 설치하라."
        ) from exc
    return schema.TABLES


def derive_schema_entities(tables) -> list[dict[str, Any]]:
    """schema.TABLES → table/column/join 엔티티 정의 목록(status=published).

    반환 항목: {"entity_type", "entity_id", "payload"}. put 순서는
    table → column → join (그래프 MERGE 의존성상 안전한 순서).
    """
    tables_out: list[dict[str, Any]] = []
    columns_out: list[dict[str, Any]] = []
    joins_out: list[dict[str, Any]] = []

    for table in tables:
        tables_out.append(
            {
                "entity_type": "table",
                "entity_id": table.name,
                "payload": {
                    "table": table.name,
                    "description": table.comment,
                    "ddl_snippet": table.create_ddl(),
                },
            }
        )
        for col in table.columns:
            columns_out.append(
                {
                    "entity_type": "column",
                    "entity_id": f"{table.name}.{col.name}",
                    "payload": {
                        "table": table.name,
                        "column": col.name,
                        "data_type": col.type,
                        "description": col.comment,
                        "ddl_snippet": col.ddl(),
                        "references": col.references,
                    },
                }
            )
            if col.references:
                # "categories(id)" → right_table=categories, right_col=id
                right_table, right_col = _parse_reference(col.references)
                joins_out.append(
                    {
                        "entity_type": "join",
                        "entity_id": f"{table.name}.{col.name}->{right_table}.{right_col}",
                        "payload": {
                            "left_table": table.name,
                            "right_table": right_table,
                            "join_on": f"{table.name}.{col.name} = {right_table}.{right_col}",
                        },
                    }
                )

    return tables_out + columns_out + joins_out


def _parse_reference(reference: str) -> tuple[str, str]:
    """"table(column)" → (table, column)."""
    table, _, rest = reference.partition("(")
    column = rest.rstrip(")")
    return table.strip(), column.strip()


# --- 비즈니스 용어(term) -----------------------------------------------------
# schema.py 실제 컬럼 기준: 매출=orders.total_amount, 활성=customers.last_login_at.

TERMS: list[dict[str, Any]] = [
    {
        "entity_id": "recent_active_customer",
        "status": "published",
        "payload": {
            "term": "최근 활성 고객",
            "definition": "최근 3개월 이내에 로그인한 고객.",
            "synonyms": ["액티브 유저", "요즘 들어온 유저", "최근 사용자"],
            "sql_fragment": "last_login_at >= CURRENT_DATE - INTERVAL '3 months'",
            "maps_to": [{"table": "customers", "column": "last_login_at"}],
        },
    },
    {
        "entity_id": "revenue",
        "status": "published",
        "payload": {
            "term": "매출",
            "definition": "주문 총액의 합계. orders.total_amount 를 집계한다.",
            "synonyms": ["revenue", "판매액", "매출액"],
            "sql_fragment": "SUM(orders.total_amount)",
            "maps_to": [{"table": "orders", "column": "total_amount"}],
        },
    },
    {
        "entity_id": "vip_customer",
        "status": "published",
        "payload": {
            "term": "VIP 고객",
            "definition": "누적 구매액 상위 고객. 고객별 orders.total_amount 합계 기준 상위 그룹.",
            "synonyms": ["우수 고객", "단골", "큰손"],
            "sql_fragment": (
                "customer_id IN (SELECT customer_id FROM orders "
                "GROUP BY customer_id ORDER BY SUM(total_amount) DESC LIMIT 10)"
            ),
            "maps_to": [
                {"table": "orders", "column": "total_amount"},
                {"table": "customers", "column": "id"},
            ],
        },
    },
    {
        "entity_id": "metro_area",
        "status": "published",
        "payload": {
            "term": "수도권",
            "definition": "서울·경기·인천 지역. customers.region 기준.",
            "synonyms": ["수도권 지역", "서울경기"],
            "sql_fragment": "region IN ('서울', '경기', '인천')",
            "maps_to": [{"table": "customers", "column": "region"}],
        },
    },
    {
        "entity_id": "peak_season",
        "status": "published",
        "payload": {
            "term": "성수기",
            "definition": "연말 쇼핑 성수기(11~12월). orders.ordered_at 월 기준.",
            "synonyms": ["연말 성수기", "피크 시즌"],
            "sql_fragment": "EXTRACT(MONTH FROM ordered_at) IN (11, 12)",
            "maps_to": [{"table": "orders", "column": "ordered_at"}],
        },
    },
    {
        # published/candidate 분리 검증용 candidate 예시(동기화에 반영되지 않아야 함).
        "entity_id": "churn_risk_customer",
        "status": "candidate",
        "payload": {
            "term": "이탈 위험 고객",
            "definition": "최근 6개월 이상 로그인하지 않은 고객(승인 대기 후보).",
            "synonyms": ["휴면 고객", "이탈 예상 고객"],
            "sql_fragment": "last_login_at < CURRENT_DATE - INTERVAL '6 months'",
            "maps_to": [{"table": "customers", "column": "last_login_at"}],
        },
    },
]


# --- few-shot (NLQ ↔ 실행 가능 SELECT) --------------------------------------

FEWSHOTS: list[dict[str, Any]] = [
    {
        "entity_id": "fewshot_region_revenue",
        "status": "published",
        "payload": {
            "question": "지역별 매출을 많은 순으로 보여줘",
            "sql": (
                "SELECT c.region, SUM(o.total_amount) AS revenue "
                "FROM orders o JOIN customers c ON o.customer_id = c.id "
                "GROUP BY c.region ORDER BY revenue DESC"
            ),
        },
    },
    {
        "entity_id": "fewshot_recent_active_count",
        "status": "published",
        "payload": {
            "question": "최근 활성 고객 수는 몇 명이야?",
            "sql": (
                "SELECT COUNT(*) AS active_customers FROM customers "
                "WHERE last_login_at >= CURRENT_DATE - INTERVAL '3 months'"
            ),
        },
    },
    {
        "entity_id": "fewshot_top_category",
        "status": "published",
        "payload": {
            "question": "카테고리별 판매 수량 상위 5개 카테고리는?",
            "sql": (
                "SELECT cat.name, SUM(oi.quantity) AS total_qty "
                "FROM order_items oi "
                "JOIN products p ON oi.product_id = p.id "
                "JOIN categories cat ON p.category_id = cat.id "
                "GROUP BY cat.name ORDER BY total_qty DESC LIMIT 5"
            ),
        },
    },
    {
        "entity_id": "fewshot_monthly_revenue",
        "status": "published",
        "payload": {
            "question": "월별 매출 추이를 알려줘",
            "sql": (
                "SELECT DATE_TRUNC('month', ordered_at) AS month, "
                "SUM(total_amount) AS revenue FROM orders "
                "GROUP BY month ORDER BY month"
            ),
        },
    },
]


def seed(
    *,
    table_name: str,
    region: str = DEFAULT_REGION,
    model_id: str = DEFAULT_EMBEDDING_MODEL,
    repository: SemanticRepository | None = None,
    embedder: Callable[[str], list[float]] | None = None,
    actor: str = "seed",
) -> dict[str, int]:
    """전체 seed 파이프라인 실행. entity_type 별 적재 건수 dict 반환.

    repository/embedder 를 주입하면 AWS 호출 없이 테스트할 수 있다.
    """
    if repository is None:
        repository = SemanticRepository(
            table_name,
            region=region,
            embedder=embedder or _make_bedrock_embedder(region, model_id),
        )

    counts = {"table": 0, "column": 0, "join": 0, "term": 0, "fewshot": 0}

    for entity in derive_schema_entities(_load_tables()):
        repository.put_entity(
            entity["entity_type"],
            entity["entity_id"],
            entity["payload"],
            status="published",
            actor=actor,
        )
        counts[entity["entity_type"]] += 1

    for term in TERMS:
        repository.put_entity(
            "term", term["entity_id"], term["payload"], status=term["status"], actor=actor
        )
        counts["term"] += 1

    for fs in FEWSHOTS:
        repository.put_entity(
            "fewshot", fs["entity_id"], fs["payload"], status=fs["status"], actor=actor
        )
        counts["fewshot"] += 1

    print(f"[seed] 적재 완료: {counts}")
    return counts


# --- OpenSearch semantic 인덱스 부트스트랩 -----------------------------------
# OSIS sink 는 인덱스가 없으면 동적 매핑으로 자동 생성하는데, 그러면 embedding 이
# knn_vector 가 아니라 float 배열로 매핑돼 kNN 질의가 400 으로 실패한다.
# 따라서 seed 가 DynamoDB 쓰기(→Streams→OSIS) 전에 올바른 매핑으로 선생성한다.

SEMANTIC_INDEX_BODY = {
    "settings": {
        "index": {
            "knn": True,
            "number_of_shards": 1,
            "number_of_replicas": 0,
        }
    },
    "mappings": {
        "properties": {
            "pk": {"type": "keyword"},
            "sk": {"type": "keyword"},
            "entity_type": {"type": "keyword"},
            "entity_id": {"type": "keyword"},
            "status": {"type": "keyword"},
            "term": {"type": "text"},
            "definition": {"type": "text"},
            "synonyms": {"type": "text"},
            "sql_fragment": {"type": "keyword"},
            "question": {"type": "text"},
            "sql": {"type": "text"},
            "embedding": {
                "type": "knn_vector",
                "dimension": EMBEDDING_DIM,
                "method": {
                    "name": "hnsw",
                    "engine": "faiss",
                    "space_type": "l2",
                },
            },
        }
    },
}


def ensure_semantic_index(endpoint: str, index: str, region: str) -> None:
    """semantic 인덱스를 kNN 매핑으로 선생성한다(존재하면 skip, 멱등).

    opensearch-py 는 seed extra 의존성이다(Lambda 런타임 경로에서는 호출되지 않음).
    """
    import boto3
    from opensearchpy import AWSV4SignerAuth, OpenSearch, RequestsHttpConnection

    host = endpoint.replace("https://", "").replace("http://", "").rstrip("/")
    auth = AWSV4SignerAuth(boto3.Session().get_credentials(), region, "es")
    client = OpenSearch(
        hosts=[{"host": host, "port": 443}],
        http_auth=auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
    )
    if client.indices.exists(index=index):
        # 잘못된 매핑(동적 생성)으로 존재하면 kNN 질의가 계속 실패하므로 감지·경고.
        mapping = client.indices.get_mapping(index=index)
        props = next(iter(mapping.values()))["mappings"].get("properties", {})
        emb_type = (props.get("embedding") or {}).get("type")
        if emb_type != "knn_vector":
            print(
                f"[semantic-index] '{index}' 의 embedding 매핑이 {emb_type!r} (knn_vector 아님) — "
                "재생성합니다(문서는 Streams 재전파로 복원)."
            )
            client.indices.delete(index=index)
        else:
            print(f"[semantic-index] '{index}' 이미 존재(knn_vector OK) - skip")
            return
    client.indices.create(index=index, body=SEMANTIC_INDEX_BODY)
    print(f"[semantic-index] '{index}' 생성 완료 (knn_vector {EMBEDDING_DIM}d)")


def _make_bedrock_embedder(region: str, model_id: str) -> Callable[[str], list[float]]:
    """Titan Text Embeddings V2 embedder 콜러블 생성(지연 클라이언트)."""
    import boto3

    client = boto3.client("bedrock-runtime", region_name=region)

    def embed(text: str) -> list[float]:
        resp = client.invoke_model(
            modelId=model_id,
            body=json.dumps({"inputText": text, "dimensions": EMBEDDING_DIM}),
            accept="application/json",
            contentType="application/json",
        )
        payload = json.loads(resp["body"].read())
        return payload["embedding"]

    return embed


def main() -> int:
    parser = argparse.ArgumentParser(description="semantic layer 초기 seed")
    parser.add_argument(
        "--table-name", default=os.environ.get("SEMANTIC_TABLE_NAME")
    )
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", DEFAULT_REGION))
    parser.add_argument(
        "--model-id",
        default=os.environ.get("EMBEDDING_MODEL_ID", DEFAULT_EMBEDDING_MODEL),
    )
    parser.add_argument(
        "--opensearch-endpoint",
        default=os.environ.get("OPENSEARCH_ENDPOINT"),
        help="지정 시 semantic 인덱스를 kNN 매핑으로 선생성(OSIS 동적 매핑 방지)",
    )
    parser.add_argument(
        "--semantic-index",
        default=os.environ.get("SEMANTIC_INDEX", "t2sql-semantic"),
    )
    args = parser.parse_args()

    if not args.table_name:
        print(
            "필수 값 누락: SEMANTIC_TABLE_NAME (환경변수 또는 --table-name)",
            file=sys.stderr,
        )
        return 2

    try:
        if args.opensearch_endpoint:
            ensure_semantic_index(args.opensearch_endpoint, args.semantic_index, args.region)
        seed(table_name=args.table_name, region=args.region, model_id=args.model_id)
        return 0
    except Exception as e:  # noqa: BLE001 - CLI 최상위에서 오류 요약 출력
        print(f"오류: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
