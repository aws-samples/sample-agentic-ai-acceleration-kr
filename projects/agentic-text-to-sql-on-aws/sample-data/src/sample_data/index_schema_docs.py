"""OpenSearch 스키마 문서 인덱싱 스크립트.

스키마 메타데이터 문서를 Titan Text Embeddings V2 로 임베딩하여
`t2sql-schema-docs` 인덱스에 적재한다. hybrid(vector + BM25) 검색이
가능하도록 knn_vector 매핑 + normalization-processor search pipeline 을 만든다.

멱등적: 인덱스/파이프라인이 없으면 생성, 문서는 doc_id 로 upsert.

환경변수: OPENSEARCH_ENDPOINT, OPENSEARCH_INDEX(기본 t2sql-schema-docs),
  EMBEDDING_MODEL_ID(기본 amazon.titan-embed-text-v2:0), AWS_REGION
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import boto3
from botocore.exceptions import ClientError

from sample_data import generator, schema_docs

DEFAULT_INDEX = "t2sql-schema-docs"
DEFAULT_EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"
# Titan Text Embeddings V2 기본 차원.
EMBEDDING_DIM = 1024
SEARCH_PIPELINE_NAME = "t2sql-hybrid-pipeline"

# knn_vector + text(BM25) 하이브리드 매핑.
INDEX_BODY = {
    "settings": {
        "index": {
            "knn": True,
            "number_of_shards": 1,
            "number_of_replicas": 0,
        }
    },
    "mappings": {
        "properties": {
            "doc_id": {"type": "keyword"},
            "doc_type": {"type": "keyword"},
            "table": {"type": "keyword"},
            "column": {"type": "keyword"},
            "data_type": {"type": "keyword"},
            "comment": {"type": "text"},
            "content": {"type": "text"},
            "ddl_snippet": {"type": "text"},
            "sample_values": {"type": "keyword"},
            "references": {"type": "keyword"},
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

# hybrid 검색 점수 결합(정규화 후 arithmetic mean).
SEARCH_PIPELINE_BODY = {
    "description": "t2sql schema-docs hybrid search (BM25 + kNN) normalization",
    "phase_results_processors": [
        {
            "normalization-processor": {
                "normalization": {"technique": "min_max"},
                "combination": {
                    "technique": "arithmetic_mean",
                    "parameters": {"weights": [0.3, 0.7]},
                },
            }
        }
    ],
}


def embed_text(bedrock_client, model_id: str, text: str) -> list[float]:
    """Titan Text Embeddings V2 로 단일 텍스트 임베딩."""
    resp = bedrock_client.invoke_model(
        modelId=model_id,
        body=json.dumps({"inputText": text, "dimensions": EMBEDDING_DIM}),
        accept="application/json",
        contentType="application/json",
    )
    payload = json.loads(resp["body"].read())
    return payload["embedding"]


def build_opensearch_client(endpoint: str, region: str):
    """SigV4 인증 OpenSearch 클라이언트 생성."""
    from opensearchpy import (
        AWSV4SignerAuth,
        OpenSearch,
        RequestsHttpConnection,
    )

    host = endpoint.replace("https://", "").replace("http://", "").rstrip("/")
    credentials = boto3.Session().get_credentials()
    auth = AWSV4SignerAuth(credentials, region, "es")
    return OpenSearch(
        hosts=[{"host": host, "port": 443}],
        http_auth=auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        pool_maxsize=20,
    )


def ensure_search_pipeline(os_client) -> None:
    """hybrid 검색 파이프라인 생성(멱등)."""
    os_client.transport.perform_request(
        "PUT",
        f"/_search/pipeline/{SEARCH_PIPELINE_NAME}",
        body=SEARCH_PIPELINE_BODY,
    )
    print(f"[opensearch] search pipeline '{SEARCH_PIPELINE_NAME}' 준비 완료")


def ensure_index(os_client, index: str) -> None:
    """인덱스 생성(없을 때만)."""
    if os_client.indices.exists(index=index):
        print(f"[opensearch] 인덱스 '{index}' 이미 존재 - skip")
        return
    os_client.indices.create(index=index, body=INDEX_BODY)
    print(f"[opensearch] 인덱스 '{index}' 생성 완료")


def index_documents(
    os_client,
    bedrock_client,
    index: str,
    model_id: str,
    documents: list[dict],
) -> int:
    """문서에 임베딩을 붙여 bulk upsert. 적재 문서 수 반환."""
    from opensearchpy.helpers import bulk

    actions = []
    for doc in documents:
        embedding = embed_text(bedrock_client, model_id, doc["content"])
        body = dict(doc)
        body["embedding"] = embedding
        actions.append(
            {
                "_op_type": "index",
                "_index": index,
                "_id": doc["doc_id"],
                "_source": body,
            }
        )
    success, _ = bulk(os_client, actions, refresh=True)
    print(f"[opensearch] {success}개 문서 인덱싱 완료")
    return success


def run(
    *,
    endpoint: str,
    index: str,
    model_id: str,
    region: str,
    os_client=None,
    bedrock_client=None,
    include_samples: bool = True,
) -> int:
    """인덱싱 파이프라인 실행. 적재 문서 수 반환."""
    os_client = os_client or build_opensearch_client(endpoint, region)
    bedrock_client = bedrock_client or boto3.client(
        "bedrock-runtime", region_name=region
    )

    dataset = generator.generate() if include_samples else None
    documents = schema_docs.build_documents(dataset)
    print(f"[docs] {len(documents)}개 스키마 문서 생성")

    ensure_index(os_client, index)
    ensure_search_pipeline(os_client)
    return index_documents(os_client, bedrock_client, index, model_id, documents)


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenSearch 스키마 문서 인덱싱")
    parser.add_argument(
        "--endpoint", default=os.environ.get("OPENSEARCH_ENDPOINT")
    )
    parser.add_argument(
        "--index", default=os.environ.get("OPENSEARCH_INDEX", DEFAULT_INDEX)
    )
    parser.add_argument(
        "--model-id",
        default=os.environ.get("EMBEDDING_MODEL_ID", DEFAULT_EMBEDDING_MODEL),
    )
    parser.add_argument(
        "--region", default=os.environ.get("AWS_REGION", "us-west-2")
    )
    parser.add_argument(
        "--no-samples",
        action="store_true",
        help="예시 값(샘플 데이터 생성) 없이 스키마만 인덱싱",
    )
    args = parser.parse_args()

    if not args.endpoint:
        print(
            "필수 값 누락: OPENSEARCH_ENDPOINT (환경변수 또는 --endpoint)",
            file=sys.stderr,
        )
        return 2

    try:
        run(
            endpoint=args.endpoint,
            index=args.index,
            model_id=args.model_id,
            region=args.region,
            include_samples=not args.no_samples,
        )
        return 0
    except ClientError as e:
        print(f"AWS 오류: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
