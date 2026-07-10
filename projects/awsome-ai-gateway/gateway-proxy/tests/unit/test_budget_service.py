# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.budget_service import BudgetService


def _resp(*, allowed: bool, reason=None, scope: str,
          used_usd=0.0, remaining_usd=0.0, limit_usd=0.0,
          policy="hard_block", throttle_active=False, throttle_rpm_pct=50,
          threshold_pct=0, soft_warning=False, config_present=True):
    """Build a Lua response JSON for single-scope budget_check."""
    return json.dumps({
        "allowed": allowed,
        "reason": reason,
        "used_usd": used_usd,
        "remaining_usd": remaining_usd,
        "limit_usd": limit_usd,
        "policy": policy,
        "throttle_active": throttle_active,
        "throttle_rpm_pct": throttle_rpm_pct,
        "threshold_pct": threshold_pct,
        "soft_warning": soft_warning,
        "scope": scope,
        "config_present": config_present,
    }).encode()


def _eval_side_effect_user_team(user_resp: bytes, team_resp: bytes):
    """Return eval side_effect that alternates user -> team responses."""
    responses = [user_resp, team_resp]
    idx = {"i": 0}

    async def _side(*_args, **_kwargs):
        r = responses[idx["i"]]
        idx["i"] += 1
        return r

    return _side


@pytest.mark.asyncio
async def test_budget_allowed(mock_redis):
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_check"] = "-- mock"

    mock_redis.eval = AsyncMock(side_effect=_eval_side_effect_user_team(
        _resp(allowed=True, scope="user", used_usd=2.0, remaining_usd=8.0, limit_usd=10.0),
        _resp(allowed=True, scope="team", used_usd=5.0, remaining_usd=95.0, limit_usd=100.0),
    ))

    svc = BudgetService()
    status = await svc.check_budget(mock_redis, None, "user-1", "team-1", "2026-04")
    assert status.remaining_usd > Decimal("0")


@pytest.mark.asyncio
async def test_budget_hard_block(mock_redis):
    """TEAM hard_block on exceeded usage."""
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_check"] = "-- mock"

    mock_redis.eval = AsyncMock(side_effect=_eval_side_effect_user_team(
        _resp(allowed=True, scope="user", used_usd=2.0, remaining_usd=8.0, limit_usd=10.0),
        _resp(allowed=False, reason="team_budget_exceeded", scope="team",
              used_usd=100.0, remaining_usd=0.0, limit_usd=100.0, threshold_pct=100),
    ))

    svc = BudgetService()
    with pytest.raises(PermissionError, match="team_budget_exceeded"):
        await svc.check_budget(mock_redis, None, "user-1", "team-1", "2026-04")


@pytest.mark.asyncio
async def test_budget_team_unset_denies(mock_redis):
    """TEAM config 미설정(config_present=False) -> team_budget_unset."""
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_check"] = "-- mock"

    mock_redis.eval = AsyncMock(side_effect=_eval_side_effect_user_team(
        _resp(allowed=True, scope="user", config_present=False),
        _resp(allowed=True, scope="team", config_present=False),
    ))

    svc = BudgetService()
    with pytest.raises(PermissionError, match="team_budget_unset"):
        await svc.check_budget(mock_redis, None, "user-1", "team-1", "2026-04")


@pytest.mark.asyncio
async def test_check_budget_uses_single_scope_per_eval(mock_redis):
    """Regression: 각 EVAL 은 2 KEYS 만 받고, 같은 hash tag 로 묶여 있어야 한다.

    Redis Cluster CROSSSLOT 재발 방지. user EVAL 의 KEYS 는 모두 {user_id} slot,
    team EVAL 의 KEYS 는 모두 {team_id} slot.
    """
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_check"] = "-- mock"

    lua_call = AsyncMock(side_effect=_eval_side_effect_user_team(
        _resp(allowed=True, scope="user", used_usd=0, remaining_usd=100, limit_usd=100),
        _resp(allowed=True, scope="team", used_usd=0, remaining_usd=100, limit_usd=100),
    ))
    mock_redis.eval = lua_call

    svc = BudgetService()
    await svc.check_budget(mock_redis, None, "u1", "t1", "2026-04")

    assert lua_call.call_count == 2, "EVAL 은 scope 당 1회씩 총 2회 호출되어야 함"

    # Call 1: USER scope  — Signature (script, num_keys, key1, key2, argv1)
    user_args = lua_call.call_args_list[0].args
    assert user_args[1] == 2, f"USER EVAL num_keys=2 기대, got {user_args[1]}"
    assert user_args[2] == "budget:user:{u1}:2026-04"
    assert user_args[3] == "budget:config:user:{u1}"
    assert user_args[4] == "user"

    # Call 2: TEAM scope
    team_args = lua_call.call_args_list[1].args
    assert team_args[1] == 2, f"TEAM EVAL num_keys=2 기대, got {team_args[1]}"
    assert team_args[2] == "budget:team:{t1}:2026-04"
    assert team_args[3] == "budget:config:team:{t1}"
    assert team_args[4] == "team"


@pytest.mark.asyncio
async def test_lowercase_contract_user_budget_exceeded(mock_redis):
    """USER hard_block → PermissionError args[0] == 'user_budget_exceeded'."""
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_check"] = "-- mock"

    mock_redis.eval = AsyncMock(side_effect=_eval_side_effect_user_team(
        _resp(allowed=False, reason="user_budget_exceeded", scope="user",
              used_usd=100.0, remaining_usd=0.0, limit_usd=100.0, threshold_pct=100),
        _resp(allowed=True, scope="team"),   # 호출되지 않아야 함
    ))

    svc = BudgetService()
    with pytest.raises(PermissionError) as exc_info:
        await svc.check_budget(mock_redis, None, "u1", "t1", "2026-04")
    assert str(exc_info.value) == "user_budget_exceeded"


@pytest.mark.asyncio
async def test_lowercase_contract_team_budget_exceeded(mock_redis):
    """TEAM hard_block → PermissionError args[0] == 'team_budget_exceeded'."""
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_check"] = "-- mock"

    mock_redis.eval = AsyncMock(side_effect=_eval_side_effect_user_team(
        _resp(allowed=True, scope="user", used_usd=0, remaining_usd=100, limit_usd=100),
        _resp(allowed=False, reason="team_budget_exceeded", scope="team",
              used_usd=200.0, remaining_usd=0.0, limit_usd=200.0, threshold_pct=100),
    ))

    svc = BudgetService()
    with pytest.raises(PermissionError) as exc_info:
        await svc.check_budget(mock_redis, None, "u1", "t1", "2026-04")
    assert str(exc_info.value) == "team_budget_exceeded"


@pytest.mark.asyncio
async def test_user_unset_team_exceeded_still_denies(mock_redis):
    """USER 미설정은 pass, TEAM 은 검사. TEAM 초과 시 deny."""
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_check"] = "-- mock"

    mock_redis.eval = AsyncMock(side_effect=_eval_side_effect_user_team(
        _resp(allowed=True, scope="user", config_present=False),  # Q: USER 미설정
        _resp(allowed=False, reason="team_budget_exceeded", scope="team",
              used_usd=100.0, remaining_usd=0.0, limit_usd=100.0),
    ))

    svc = BudgetService()
    with pytest.raises(PermissionError, match="team_budget_exceeded"):
        await svc.check_budget(mock_redis, None, "u1", "t1", "2026-04")


# ── Task B: TEAM config cold-cache DB fallback ──

@pytest.mark.asyncio
async def test_check_budget_hydrates_team_config_when_redis_miss():
    """team_config_key 가 Redis에 없을 때 DB에서 조회해 캐시를 채운다."""
    from app.services.lua_loader import LuaScriptLoader
    from app.models.budget import BudgetConfig, BudgetScope, BudgetPolicy as OrmBudgetPolicy
    from sqlalchemy.ext.asyncio import AsyncSession

    LuaScriptLoader._scripts["budget_check"] = "-- mock"

    team_id = "t1"

    fake_redis = MagicMock()
    # exists: team_config_key=False (cold-cache miss), user_key=False
    fake_redis.exists = AsyncMock(side_effect=[False, False, False])
    fake_redis.set = AsyncMock()
    fake_redis.eval = AsyncMock(side_effect=_eval_side_effect_user_team(
        _resp(allowed=True, scope="user", used_usd=0, remaining_usd=100, limit_usd=100),
        _resp(allowed=True, scope="team", used_usd=0, remaining_usd=100, limit_usd=100),
    ))
    fake_redis.get = AsyncMock(return_value=None)

    # DB mock: TEAM BudgetConfig 존재
    team_cfg = MagicMock(spec=BudgetConfig)
    team_cfg.scope = BudgetScope.TEAM
    team_cfg.scope_id = team_id
    team_cfg.max_budget_usd = Decimal("5000")
    team_cfg.policy = OrmBudgetPolicy.HARD_BLOCK
    team_cfg.is_active = True

    db_execute_result = MagicMock()
    db_execute_result.scalar_one_or_none = MagicMock(return_value=team_cfg)
    fake_db = MagicMock(spec=AsyncSession)
    fake_db.execute = AsyncMock(return_value=db_execute_result)

    svc = BudgetService()
    await svc.check_budget(fake_redis, fake_db, "u1", team_id, "2026-04")

    # budget:config:team:{t1} SET 이 한 번 이상 호출됐는지 확인
    set_calls = [c for c in fake_redis.set.call_args_list if "budget:config:team" in str(c.args[0])]
    assert len(set_calls) >= 1, "TEAM config cache 를 Redis 에 SET 해야 함"

    # EX=300 TTL 확인
    for c in set_calls:
        assert c.kwargs.get("ex") == 300, f"TTL 이 300 이어야 함, got {c.kwargs.get('ex')}"


@pytest.mark.asyncio
async def test_check_budget_no_set_when_db_has_no_team_config():
    """DB에 TEAM budget 없으면 Redis SET 없이 Lua 가 team_budget_unset 반환."""
    from app.services.lua_loader import LuaScriptLoader
    from sqlalchemy.ext.asyncio import AsyncSession

    LuaScriptLoader._scripts["budget_check"] = "-- mock"

    fake_redis = MagicMock()
    fake_redis.exists = AsyncMock(side_effect=[False, False, False])
    fake_redis.set = AsyncMock()
    # user pass (config_present=False 이니 pass-through), team config_present=False → team_budget_unset
    fake_redis.eval = AsyncMock(side_effect=_eval_side_effect_user_team(
        _resp(allowed=True, scope="user", config_present=False),
        _resp(allowed=True, scope="team", config_present=False),
    ))
    fake_redis.get = AsyncMock(return_value=None)

    # DB mock: TEAM BudgetConfig 없음
    db_execute_result = MagicMock()
    db_execute_result.scalar_one_or_none = MagicMock(return_value=None)
    fake_db = MagicMock(spec=AsyncSession)
    fake_db.execute = AsyncMock(return_value=db_execute_result)

    svc = BudgetService()
    with pytest.raises(PermissionError, match="team_budget_unset"):
        await svc.check_budget(fake_redis, fake_db, "u1", "t1", "2026-04")

    # DB에 없으므로 budget:config:team 키 SET 없어야 함
    set_calls = [c for c in fake_redis.set.call_args_list if "budget:config:team" in str(c.args[0])]
    assert len(set_calls) == 0, "DB에 TEAM budget 없으면 Redis SET 하지 않아야 함"
