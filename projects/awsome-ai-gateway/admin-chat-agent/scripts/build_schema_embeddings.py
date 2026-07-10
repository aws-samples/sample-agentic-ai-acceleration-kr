#!/usr/bin/env python3
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""schema_whitelist.yaml → Bedrock Titan Text Embedding v2 → Aurora pgvector.

docs/admin-chat-agent-spec.md §2.5.1 — Schema Linking RAG bootstrap.

매번 yaml 변경 후 1회 실행 (또는 CI step). idempotent — 같은 (schema, table,
column) 은 UPDATE 됨.

사용:
    DB_URL=postgresql://user:pw@host/db \\
    AWS_REGION=ap-northeast-2 \\
    python build_schema_embeddings.py admin-chat-agent/config/schema_whitelist.yaml

환경변수:
    DB_URL                — gateway DB 의 superuser 또는 chat_agent schema 쓰기 권한자
    AWS_REGION            — Bedrock 호출 region (default ap-northeast-2)
    EMBEDDING_MODEL       — default "amazon.titan-embed-text-v2:0"
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import boto3
import psycopg2
import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "amazon.titan-embed-text-v2:0")
AWS_REGION = os.environ.get("AWS_REGION", "ap-northeast-2")


def embed(text: str, bedrock) -> list[float]:
    """Bedrock Titan Text Embedding v2 → 1024-dim float vector."""
    response = bedrock.invoke_model(
        modelId=EMBEDDING_MODEL,
        body=json.dumps({"inputText": text, "dimensions": 1024}),
    )
    return json.loads(response["body"].read())["embedding"]


def column_text(schema: str, table: str, column: dict) -> str:
    """Embedding 입력 — 컬럼 의미를 자연어로."""
    parts = [
        f"Table {schema}.{table}, column {column['name']}",
        f"Type: {column.get('type', '')}",
    ]
    if column.get("description"):
        parts.append(f"Description: {column['description']}")
    if column.get("sample_values"):
        sv = column["sample_values"]
        if isinstance(sv, list):
            parts.append(f"Sample values: {', '.join(str(v) for v in sv[:5])}")
    return ". ".join(parts)


def main(yaml_path: str) -> None:
    db_url = os.environ.get("DB_URL")
    if not db_url:
        logger.error("DB_URL env required")
        sys.exit(1)

    bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)
    conn = psycopg2.connect(db_url)
    conn.autocommit = False

    wl = yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8"))
    tables = wl.get("allowed_tables", [])

    inserted, updated = 0, 0
    with conn.cursor() as cur:
        for t in tables:
            schema, table = t["schema"], t["table"]
            for col in t.get("columns", []):
                text = column_text(schema, table, col)
                vec = embed(text, bedrock)
                vec_literal = "[" + ",".join(str(v) for v in vec) + "]"

                cur.execute(
                    """
                    INSERT INTO chat_agent.schema_embeddings
                        (schema_name, table_name, column_name, description,
                         sample_values, embedding)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s::vector)
                    ON CONFLICT (schema_name, table_name, column_name)
                    DO UPDATE SET
                        description   = EXCLUDED.description,
                        sample_values = EXCLUDED.sample_values,
                        embedding     = EXCLUDED.embedding
                    RETURNING xmax = 0 AS inserted
                    """,
                    (
                        schema,
                        table,
                        col["name"],
                        col.get("description"),
                        json.dumps(col.get("sample_values")),
                        vec_literal,
                    ),
                )
                row = cur.fetchone()
                if row and row[0]:
                    inserted += 1
                else:
                    updated += 1
                logger.info(
                    "embedded %s.%s.%s (%d-dim)", schema, table, col["name"], len(vec)
                )

    conn.commit()
    conn.close()
    logger.info("Done. inserted=%d updated=%d", inserted, updated)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1])
