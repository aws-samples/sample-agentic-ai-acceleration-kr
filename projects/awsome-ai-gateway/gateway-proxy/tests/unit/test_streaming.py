# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import pytest

from app.services.streaming import _try_extract_usage


def test_try_extract_usage_openai_format():
    chunk = b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":20,"total_tokens":30}}\n\n'
    usage = _try_extract_usage(chunk)
    assert usage is not None
    assert usage.input_tokens == 10
    assert usage.output_tokens == 20
    assert usage.total_tokens == 30


def test_try_extract_usage_done_chunk():
    chunk = b"data: [DONE]\n\n"
    usage = _try_extract_usage(chunk)
    assert usage is None


def test_try_extract_usage_no_usage():
    chunk = b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
    usage = _try_extract_usage(chunk)
    assert usage is None


def test_try_extract_usage_invalid_json():
    chunk = b"not json at all"
    usage = _try_extract_usage(chunk)
    assert usage is None
