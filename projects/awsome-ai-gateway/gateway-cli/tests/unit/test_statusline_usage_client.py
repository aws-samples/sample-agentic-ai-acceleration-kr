# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Tests for statusline.usage_client — Gateway usage API.

⚠️ 아래 `_USAGE_ME_PAYLOAD` 는 손으로 지어낸 모양이 아니라 gateway-proxy 가
실제로 돌려주는 응답이다. 생성 근거:

  gateway-proxy/src/app/routers/usage.py  `usage_me()`
    → `UsageMeResponse(...).model_dump(mode="json")` 에
      `content["model_breakdown"] = [...]` 를 덧붙여 JSONResponse 로 반환
  gateway-proxy/src/app/schemas/responses.py  `UsageMeResponse` / `UsageBudgetInfo`

원래 이 테스트는 평평한 `{"used","limit","remaining","percentage"}` 를 넣고
있었다. 서버는 그런 필드를 낸 적이 없고(예산은 `budget.{max_usd,used_usd,
remaining_usd,pct}` 중첩), 클라이언트는 처음부터 서버 모양을 올바르게 읽고
있었다 — 즉 프로덕션 코드가 아니라 **테스트가 계약을 잘못 박고 있었다**.
그래서 `fetch_usage` 는 전부 0 을 돌려주고 단정이 깨졌다.

이 파일을 고칠 일이 생기면 위 두 파일을 먼저 읽고, 서버 응답을 그대로 옮겨라.
Decimal 로 비교하는 것도 의도적이다: 서버는 금액을 **문자열**로 직렬화하므로
float 로 받으면 조용히 정밀도가 깎인다.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
import requests
import responses

from statusline.config import StatuslineConfig
from statusline.usage_client import fetch_usage

_USAGE_ME_PAYLOAD = {
    "user_id": "11111111-1111-1111-1111-111111111111",
    "period": "2026-04",
    "usage": {"total_tokens": 4321, "total_cost_usd": "12.5"},
    "budget": {
        "max_usd": "100.00",
        "used_usd": "12.50",
        "remaining_usd": "87.50",
        "pct": 12.5,
        "policy": "hard_block",
    },
    "daily_breakdown": [
        {
            "date": "2026-04-01",
            "total_tokens": 4321,
            "total_cost_usd": "12.5",
            "by_model": [],
        }
    ],
    "model_breakdown": [
        {
            "model": "claude-sonnet-4",
            "cost_usd": "12.5",
            "input_tokens": 1000,
            "output_tokens": 321,
            "cache_write_tokens": 2000,
            "cache_read_tokens": 1000,
            "requests": 7,
        }
    ],
}


@responses.activate
def test_fetch_usage_success() -> None:
    responses.add(
        responses.GET,
        "https://gw.example.com/v1/usage/me",
        json=_USAGE_ME_PAYLOAD,
        status=200,
    )

    config = StatuslineConfig(gateway_url="https://gw.example.com")
    result = fetch_usage(config, "vk-test")

    assert result.used == Decimal("12.50")
    assert result.limit == Decimal("100.00")
    assert result.remaining == Decimal("87.50")
    assert result.percentage == 12.5
    assert result.period == "2026-04"
    assert result.fetched_at is not None


@responses.activate
def test_fetch_usage_parses_model_breakdown() -> None:
    """statusline 이 모델별 색상/집계를 그리려면 이 배열이 필요하다."""
    responses.add(
        responses.GET,
        "https://gw.example.com/v1/usage/me",
        json=_USAGE_ME_PAYLOAD,
        status=200,
    )

    config = StatuslineConfig(gateway_url="https://gw.example.com")
    result = fetch_usage(config, "vk-test")

    assert len(result.models) == 1
    model = result.models[0]
    assert model.model == "claude-sonnet-4"
    assert model.cost_usd == Decimal("12.5")
    assert model.input_tokens == 1000
    assert model.output_tokens == 321
    assert model.cache_write_tokens == 2000
    assert model.cache_read_tokens == 1000
    assert model.requests == 7


@responses.activate
def test_fetch_usage_tolerates_missing_budget() -> None:
    """예산 미설정 사용자는 서버가 budget 을 0 으로 채운다 — 죽지 말고 0 이어야 한다.

    (⚠️ 예전 근거는 "Redis 에 budget:config 가 없으면 서버가 0 을 준다" 였는데, 그것은
    결함이었고 지금은 서버가 DB 로 내려가 실제 한도를 돌려준다. 이 테스트가 고정하는
    것은 **한도가 정말로 없는** 사용자에 대한 클라이언트 쪽 나눗셈 0 방어다.)
    """
    responses.add(
        responses.GET,
        "https://gw.example.com/v1/usage/me",
        json={
            "user_id": "u",
            "period": "2026-04",
            "usage": {"total_tokens": 0, "total_cost_usd": "0"},
            "budget": {
                "max_usd": "0",
                "used_usd": "0",
                "remaining_usd": "0",
                "pct": 0.0,
                "policy": "hard_block",
            },
            "daily_breakdown": [],
        },
        status=200,
    )

    config = StatuslineConfig(gateway_url="https://gw.example.com")
    result = fetch_usage(config, "vk-test")

    assert result.limit == Decimal("0")
    assert result.percentage == 0.0
    assert result.models == []


@responses.activate
def test_fetch_usage_unauthorized() -> None:
    responses.add(
        responses.GET,
        "https://gw.example.com/v1/usage/me",
        json={"error": "unauthorized"},
        status=401,
    )

    config = StatuslineConfig(gateway_url="https://gw.example.com")
    with pytest.raises(Exception):
        fetch_usage(config, "invalid-key")


@responses.activate
def test_fetch_usage_network_error() -> None:
    # requests 가 실제로 던지는 예외 타입으로 시뮬레이션한다. 빌트인
    # ConnectionError 를 쓰면 requests 의 예외 계층을 벗어나 프로덕션에서
    # 절대 일어나지 않는 경로를 검증하게 된다.
    responses.add(
        responses.GET,
        "https://gw.example.com/v1/usage/me",
        body=requests.exceptions.ConnectionError("refused"),
    )

    config = StatuslineConfig(gateway_url="https://gw.example.com")
    with pytest.raises(requests.exceptions.ConnectionError):
        fetch_usage(config, "vk-test")
