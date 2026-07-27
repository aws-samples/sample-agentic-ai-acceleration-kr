"""임베딩 클라이언트 — Bedrock InvokeModel(amazon.titan-embed-text-v2:0, 1024차원).

term/fewshot 엔티티는 hybrid 검색용 임베딩이 필요하다. SemanticRepository 에
embedder 콜러블(``str -> list[float]``)로 주입한다.

semantic-retrieval-mcp / semantic-layer seed 와 **동일한 모델·차원·normalize 설정**을
쓴다(인덱스 knn_vector 매핑 1024d 와 일치해야 kNN 질의가 성립).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

# titan-embed-text-v2 기본 출력 차원(OpenSearch knn_vector 매핑과 일치해야 함).
TITAN_V2_DIMENSIONS = 1024
DEFAULT_EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"


class EmbeddingClient:
    """텍스트를 임베딩 벡터로 변환한다(bedrock-runtime InvokeModel)."""

    def __init__(
        self,
        model_id: str | None = None,
        region: str | None = None,
        dimensions: int = TITAN_V2_DIMENSIONS,
        client: Any | None = None,
    ) -> None:
        self.model_id = model_id or os.environ.get("EMBEDDING_MODEL_ID", DEFAULT_EMBEDDING_MODEL)
        self.region = region or os.environ.get("AWS_REGION", "us-west-2")
        self.dimensions = dimensions
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("bedrock-runtime", region_name=self.region)
        return self._client

    def embed(self, text: str) -> list[float]:
        body = json.dumps(
            {
                "inputText": text,
                "dimensions": self.dimensions,
                "normalize": True,
            }
        )
        response = self.client.invoke_model(modelId=self.model_id, body=body)
        payload = json.loads(_read_body(response["body"]))
        return payload["embedding"]


def make_embedder(client: Any | None = None) -> Callable[[str], list[float]]:
    """SemanticRepository 에 주입할 embedder 콜러블 생성(지연 클라이언트)."""
    embedding_client = EmbeddingClient(client=client)
    return embedding_client.embed


def _read_body(body: Any) -> str | bytes:
    """boto3 StreamingBody 또는 이미 bytes/str 인 응답 본문을 읽는다."""
    if hasattr(body, "read"):
        return body.read()
    return body
