"""Data API 러너 테스트 (boto3 호출 없이 fake client 주입)."""

from __future__ import annotations

import pytest

from evaluation.dataapi import AuroraReadOnlyRunner, DataApiError

from .fakes import FakeDataApiClient

_RESPONSE = {
    "columnMetadata": [{"label": "region"}, {"name": "revenue"}],
    "records": [
        [{"stringValue": "서울"}, {"doubleValue": 100.0}],
        [{"stringValue": "부산"}, {"isNull": True}],
    ],
}


def _runner(client, **kwargs):
    return AuroraReadOnlyRunner(
        cluster_arn="arn:cluster",
        secret_arn="arn:secret",
        db_name="ecommerce",
        client=client,
        **kwargs,
    )


def test_run_normalizes_columns_and_rows():
    client = FakeDataApiClient(_RESPONSE)
    result = _runner(client).run("SELECT 1")
    assert result.columns == ["region", "revenue"]
    assert result.rows == [["서울", 100.0], ["부산", None]]
    assert result.row_count == 2
    # read-only 실행 규약: continueAfterTimeout=False, 메타데이터 포함.
    call = client.calls[0]
    assert call["continueAfterTimeout"] is False
    assert call["includeResultMetadata"] is True
    assert call["database"] == "ecommerce"


def test_run_wraps_client_error():
    client = FakeDataApiClient(RuntimeError("permission denied for table orders"))
    with pytest.raises(DataApiError) as exc:
        _runner(client).run("SELECT 1")
    assert "permission denied" in str(exc.value)


def test_run_rejects_oversized_result():
    response = {
        "columnMetadata": [{"label": "n"}],
        "records": [[{"longValue": i}] for i in range(5)],
    }
    with pytest.raises(DataApiError) as exc:
        _runner(FakeDataApiClient(response), max_rows=3).run("SELECT 1")
    assert "상한" in str(exc.value)


def test_require_config_reports_missing_env():
    runner = AuroraReadOnlyRunner(
        cluster_arn="", secret_arn="", db_name="", client=object()
    )
    with pytest.raises(DataApiError) as exc:
        runner.run("SELECT 1")
    assert "AURORA_CLUSTER_ARN" in str(exc.value)


def test_array_and_blob_values():
    response = {
        "columnMetadata": [{"label": "tags"}, {"label": "blob"}],
        "records": [
            [
                {"arrayValue": {"stringValues": ["a", "b"]}},
                {"blobValue": b"hello"},
            ]
        ],
    }
    result = _runner(FakeDataApiClient(response)).run("SELECT 1")
    assert result.rows == [[["a", "b"], "hello"]]
