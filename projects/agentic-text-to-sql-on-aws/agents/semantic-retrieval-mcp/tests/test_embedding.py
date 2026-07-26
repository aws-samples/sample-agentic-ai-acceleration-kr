"""임베딩 클라이언트 유닛 테스트 — bedrock-runtime mock."""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock

from semantic_retrieval_mcp.embedding import EmbeddingClient


def test_embed_invokes_titan_and_parses_vector() -> None:
    client = MagicMock()
    client.invoke_model.return_value = {
        "body": io.BytesIO(json.dumps({"embedding": [0.1, 0.2, 0.3]}).encode("utf-8"))
    }
    ec = EmbeddingClient(
        model_id="amazon.titan-embed-text-v2:0", region="us-west-2", client=client
    )

    vector = ec.embed("최근 사용자")

    assert vector == [0.1, 0.2, 0.3]
    kwargs = client.invoke_model.call_args.kwargs
    assert kwargs["modelId"] == "amazon.titan-embed-text-v2:0"
    body = json.loads(kwargs["body"])
    assert body["inputText"] == "최근 사용자"
    assert body["dimensions"] == 1024
    assert body["normalize"] is True


def test_embed_handles_raw_bytes_body() -> None:
    client = MagicMock()
    client.invoke_model.return_value = {"body": json.dumps({"embedding": [1.0]}).encode("utf-8")}
    ec = EmbeddingClient(region="us-west-2", client=client)
    assert ec.embed("x") == [1.0]
