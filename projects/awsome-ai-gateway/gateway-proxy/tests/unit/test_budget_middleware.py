# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json

import pytest

from app.middleware.budget import BudgetMiddleware


class FakeApp:
    async def __call__(self, scope, receive, send):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,expected_code,status_label", [
    ("team_budget_unset",    "team_budget_unset",    "팀 예산이 설정되지 않았습니다"),
    ("team_budget_exceeded", "team_budget_exceeded", "Budget limit exceeded"),
    ("user_budget_exceeded", "user_budget_exceeded", "Budget limit exceeded"),
    ("hard_block",           "hard_block",           "Monthly budget exhausted"),
    ("no_budget_assigned",   "no_budget_assigned",   "No budget assigned"),
    ("team_soft_limit_exceeded", "team_soft_limit_exceeded", "Budget limit exceeded"),
    ("user_soft_limit_exceeded", "user_soft_limit_exceeded", "Budget limit exceeded"),
])
async def test_send_429_budget_codes(reason, expected_code, status_label):
    sent = []
    async def send(msg): sent.append(msg)
    mw = BudgetMiddleware(FakeApp())
    await mw._send_429_budget(scope={"type": "http"}, send=send, reason=reason)
    start = sent[0]
    body = json.loads(sent[1]["body"])
    assert start["status"] == 429
    assert body["error"]["code"] == expected_code
    assert status_label in body["error"]["message"]
