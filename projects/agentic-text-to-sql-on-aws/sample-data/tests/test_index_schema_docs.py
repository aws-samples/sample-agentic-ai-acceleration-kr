"""OpenSearch 인덱싱 로직 테스트 (Bedrock/OpenSearch mock)."""

from __future__ import annotations

import json
from unittest import mock

from sample_data import index_schema_docs as idx


def test_index_mapping_dimension_matches_titan_v2():
    props = idx.INDEX_BODY["mappings"]["properties"]
    assert props["embedding"]["type"] == "knn_vector"
    assert props["embedding"]["dimension"] == 1024
    assert idx.INDEX_BODY["settings"]["index"]["knn"] is True


def test_search_pipeline_uses_normalization_processor():
    procs = idx.SEARCH_PIPELINE_BODY["phase_results_processors"]
    assert any("normalization-processor" in p for p in procs)


def test_embed_text_calls_bedrock_with_dimensions():
    bedrock = mock.Mock()
    body_stream = mock.Mock()
    body_stream.read.return_value = json.dumps({"embedding": [0.1] * 1024}).encode()
    bedrock.invoke_model.return_value = {"body": body_stream}

    vec = idx.embed_text(bedrock, "amazon.titan-embed-text-v2:0", "hello")
    assert len(vec) == 1024
    sent = json.loads(bedrock.invoke_model.call_args.kwargs["body"])
    assert sent["inputText"] == "hello"
    assert sent["dimensions"] == 1024


def test_ensure_index_skips_when_exists():
    os_client = mock.Mock()
    os_client.indices.exists.return_value = True
    idx.ensure_index(os_client, "t2sql-schema-docs")
    os_client.indices.create.assert_not_called()


def test_ensure_index_creates_when_absent():
    os_client = mock.Mock()
    os_client.indices.exists.return_value = False
    idx.ensure_index(os_client, "t2sql-schema-docs")
    os_client.indices.create.assert_called_once()


def test_run_indexes_all_documents():
    os_client = mock.Mock()
    os_client.indices.exists.return_value = False
    bedrock = mock.Mock()
    body_stream = mock.Mock()
    body_stream.read.return_value = json.dumps({"embedding": [0.0] * 1024}).encode()
    bedrock.invoke_model.return_value = {"body": body_stream}

    # bulk 는 index_documents 내부에서 opensearchpy.helpers 에서 import 되므로 원본을 패치.
    with mock.patch("opensearchpy.helpers.bulk", return_value=(42, [])) as bulk_mock:
        count = idx.run(
            endpoint="https://example.us-west-2.es.amazonaws.com",
            index="t2sql-schema-docs",
            model_id="amazon.titan-embed-text-v2:0",
            region="us-west-2",
            os_client=os_client,
            bedrock_client=bedrock,
            include_samples=False,
        )
    assert count == 42
    # 파이프라인/인덱스 생성이 호출됨.
    os_client.indices.create.assert_called_once()
    assert bulk_mock.call_args is not None
    # 모든 문서에 대해 임베딩 호출.
    assert bedrock.invoke_model.call_count > 0
