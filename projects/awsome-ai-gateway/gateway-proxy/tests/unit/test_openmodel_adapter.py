# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.providers.openmodel_adapter import OpenModelAdapter


def make_mock_client(status=200, response_json=None):
    response_json = response_json or {
        "choices": [{"message": {"content": "hello"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }
    response = MagicMock(spec=httpx.Response)
    response.status_code = status
    response.content = json.dumps(response_json).encode()
    response.headers = {}
    client = AsyncMock(spec=httpx.AsyncClient)
    client.post = AsyncMock(return_value=response)
    return client


def test_estimate_input_tokens():
    messages = [{"role": "user", "content": "Hello world!"}]
    tokens = OpenModelAdapter.estimate_input_tokens(messages)
    assert tokens > 0
    assert tokens == max(1, len("Hello world!") // 4)


def test_build_request_body_adds_stream_options():
    client = AsyncMock()
    adapter = OpenModelAdapter(client, "http://mock:8080")
    body = json.dumps({"model": "llama-70b", "messages": [], "stream": True}).encode()
    result = adapter._build_request_body(body, "meta-llama/Llama-3.1-70B", stream=True)
    assert result["stream_options"]["include_usage"] is True
    assert result["model"] == "meta-llama/Llama-3.1-70B"


@pytest.mark.asyncio
async def test_invoke_success():
    client = make_mock_client()
    adapter = OpenModelAdapter(client, "http://mock:8080")
    body = json.dumps({"model": "llama-70b", "messages": []}).encode()
    status, response_body, headers, usage = await adapter.invoke(body, "meta-llama/Llama-3.1-70B")
    assert status == 200
    assert usage.input_tokens == 10
    assert usage.output_tokens == 20
