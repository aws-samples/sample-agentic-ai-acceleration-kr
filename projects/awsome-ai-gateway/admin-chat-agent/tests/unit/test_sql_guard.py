# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""sql_guard 결정적 검증 룰 단위 테스트 (DEVLOG §58, L0/L1).

실제 config/schema_whitelist.yaml 을 로드해 ground-truth 메타로 채점한다 —
사본 drift 없이 운영 룰을 그대로 검증. sqlglot 의존(Lambda 레이어와 동일).

핵심: '조용히 틀린 숫자'를 내는 SQL 이 errors/warnings 로 잡히고, 정상 SQL 은
통과해야 한다(false positive = 본 경로 차단 = 안전망 실패).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
pytest.importorskip("sqlglot")

_QUERY_DB = Path(__file__).resolve().parents[2] / "lambdas" / "query_db"
sys.path.insert(0, str(_QUERY_DB))

import sql_guard  # noqa: E402

_WHITELIST_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "schema_whitelist.yaml"
)


@pytest.fixture(scope="module")
def whitelist() -> dict:
    with open(_WHITELIST_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _guard(sql: str, whitelist: dict) -> dict:
    return sql_guard.guard(sql, whitelist)


# ─────────────────────────────────────────────────────────────────────────────
# 정상 SQL — 통과해야 함 (false positive 방지가 안전망의 생명)
# ─────────────────────────────────────────────────────────────────────────────
class TestCleanSQLPasses:
    def test_kst_anchored_monthly_top(self, whitelist):
        sql = (
            "SELECT u.email, SUM(l.cost_usd) AS cost "
            "FROM usage.usage_logs l JOIN auth.users u ON u.id = l.user_id "
            "WHERE l.requested_at AT TIME ZONE 'Asia/Seoul' >= "
            "date_trunc('month', now() AT TIME ZONE 'Asia/Seoul') "
            "GROUP BY u.email ORDER BY cost DESC LIMIT 10"
        )
        r = _guard(sql, whitelist)
        assert r["errors"] == []
        # team_id 경유 아님, KST 앵커 있음, 부모 PK join → fan-out/timezone 경고 없어야
        assert not any("KST" in w for w in r["warnings"]), r["warnings"]
        assert not any("fan-out" in w for w in r["warnings"]), r["warnings"]

    def test_kst_date_cast_today(self, whitelist):
        sql = (
            "SELECT l.model_alias, COUNT(*) AS calls FROM usage.usage_logs l "
            "WHERE (l.requested_at AT TIME ZONE 'Asia/Seoul')::date = "
            "(now() AT TIME ZONE 'Asia/Seoul')::date GROUP BY l.model_alias"
        )
        r = _guard(sql, whitelist)
        assert r["errors"] == []
        assert not any("KST" in w for w in r["warnings"]), r["warnings"]

    def test_team_id_direct_no_fanout(self, whitelist):
        # 팀 집계를 usage_logs.team_id 직접 사용 + teams 를 PK 로 join → 안전
        sql = (
            "SELECT t.name, SUM(l.cost_usd) AS cost "
            "FROM usage.usage_logs l JOIN auth.teams t ON t.id = l.team_id "
            "WHERE l.requested_at AT TIME ZONE 'Asia/Seoul' >= "
            "date_trunc('month', now() AT TIME ZONE 'Asia/Seoul') "
            "GROUP BY t.name"
        )
        r = _guard(sql, whitelist)
        assert r["errors"] == []
        assert not any("fan-out" in w for w in r["warnings"]), r["warnings"]


# ─────────────────────────────────────────────────────────────────────────────
# L0-1 컬럼 검증 (결함⑦) — ERROR
# ─────────────────────────────────────────────────────────────────────────────
class TestColumnValidation:
    def test_phantom_column_rejected(self, whitelist):
        # last_login_at 은 스키마에 없음(whitelist 가 '없음' 명시)
        sql = "SELECT u.email, u.last_login_at FROM auth.users u"
        r = _guard(sql, whitelist)
        assert r["errors"], "유령 컬럼은 reject 돼야 함"
        assert "column" in r["errors"][0].lower()

    def test_ambiguous_column_rejected(self, whitelist):
        # team_id 가 usage_logs 와 users 양쪽 → 모호
        sql = (
            "SELECT team_id FROM usage.usage_logs l "
            "JOIN auth.users u ON u.id = l.user_id"
        )
        r = _guard(sql, whitelist)
        assert r["errors"], "모호 컬럼은 reject 돼야 함"


# ─────────────────────────────────────────────────────────────────────────────
# L0-2 timezone 앵커 (결함③) — WARN
# ─────────────────────────────────────────────────────────────────────────────
class TestTimezoneAnchor:
    def test_raw_utc_date_trunc_month_warns(self, whitelist):
        # 캘린더 경계(월) UTC date_trunc — KST 자정과 9시간 어긋남(결함③).
        sql = (
            "SELECT SUM(cost_usd) AS cost FROM usage.usage_logs "
            "WHERE requested_at >= date_trunc('month', now())"
        )
        r = _guard(sql, whitelist)
        assert any("KST" in w or "앵커" in w for w in r["warnings"]), r["warnings"]

    def test_raw_utc_date_cast_bucket_warns(self, whitelist):
        # 일별 버킷을 UTC ::date 로 — KST 일자 경계 어긋남.
        sql = "SELECT requested_at::date AS d, COUNT(*) FROM usage.usage_logs GROUP BY d"
        r = _guard(sql, whitelist)
        assert any("KST" in w or "앵커" in w for w in r["warnings"]), r["warnings"]

    def test_point_relative_window_no_warn(self, whitelist):
        # "지난 24시간" 류 점-상대 윈도우는 타임존 무관 — false positive 금지.
        sql = (
            "SELECT COUNT(*) FROM usage.usage_logs "
            "WHERE requested_at >= now() - interval '24 hours'"
        )
        r = _guard(sql, whitelist)
        assert not any("KST" in w or "앵커" in w for w in r["warnings"]), r["warnings"]

    def test_kst_anchored_no_warn(self, whitelist):
        sql = (
            "SELECT SUM(cost_usd) FROM usage.usage_logs WHERE requested_at AT TIME ZONE "
            "'Asia/Seoul' >= date_trunc('month', now() AT TIME ZONE 'Asia/Seoul')"
        )
        r = _guard(sql, whitelist)
        assert not any("KST" in w or "앵커" in w for w in r["warnings"]), r["warnings"]


# ─────────────────────────────────────────────────────────────────────────────
# L0-3 fan-out (결함②) — WARN
# ─────────────────────────────────────────────────────────────────────────────
class TestFanout:
    def test_vk_join_sum_cost_warns(self, whitelist):
        # usage_logs ⨝ virtual_keys(비-PK user_id join) 후 SUM(cost_usd) → N배
        sql = (
            "SELECT u.email, SUM(l.cost_usd) AS cost "
            "FROM usage.usage_logs l "
            "JOIN auth.virtual_keys vk ON vk.user_id = l.user_id "
            "JOIN auth.users u ON u.id = l.user_id "
            "WHERE vk.status = 'ACTIVE' GROUP BY u.email"
        )
        r = _guard(sql, whitelist)
        assert any("fan-out" in w for w in r["warnings"]), r["warnings"]


# ─────────────────────────────────────────────────────────────────────────────
# L1 dashboard 정합 (결함⑧) — WARN
# ─────────────────────────────────────────────────────────────────────────────
class TestDashboardConsistency:
    def test_total_cost_without_status_warns(self, whitelist):
        sql = (
            "SELECT SUM(cost_usd) AS total FROM usage.usage_logs "
            "WHERE requested_at AT TIME ZONE 'Asia/Seoul' >= "
            "date_trunc('month', now() AT TIME ZONE 'Asia/Seoul')"
        )
        r = _guard(sql, whitelist)
        assert any("대시보드" in w for w in r["warnings"]), r["warnings"]

    def test_total_cost_with_status_ok(self, whitelist):
        sql = (
            "SELECT SUM(cost_usd) AS total FROM usage.usage_logs "
            "WHERE status = 'SUCCESS' AND requested_at AT TIME ZONE 'Asia/Seoul' >= "
            "date_trunc('month', now() AT TIME ZONE 'Asia/Seoul')"
        )
        r = _guard(sql, whitelist)
        assert not any("대시보드" in w for w in r["warnings"]), r["warnings"]


# ─────────────────────────────────────────────────────────────────────────────
# graceful — 안전망이 본 경로를 죽이면 안 됨
# ─────────────────────────────────────────────────────────────────────────────
class TestGraceful:
    def test_unparseable_returns_empty(self, whitelist):
        r = _guard("NOT EVEN SQL ;;;", whitelist)
        # 파싱 실패는 기존 validate_sql 이 처리 — guard 는 죽지 않고 빈 결과
        assert "errors" in r and "warnings" in r

    def test_guard_never_raises(self, whitelist):
        for sql in ["", "SELECT 1", "WITH x AS (SELECT 1) SELECT * FROM x"]:
            r = sql_guard.guard(sql, whitelist)
            assert isinstance(r, dict)
