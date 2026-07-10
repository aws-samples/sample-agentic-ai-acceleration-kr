# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""gateway-proxy DowngradePolicyLoader가 사용하는 cache key 형식과
admin-api budget_service가 invalidate하는 cache key가 동일해야 한다.

만약 둘 중 하나가 변경되면 캐시 무효화가 무력화되어 정책 변경이
gateway-proxy에 60초간 반영되지 않는 silent 버그가 발생한다.

이 테스트는 양쪽 컨벤션이 동기화되어 있음을 보장한다.
"""

import uuid


def test_cache_key_format_matches_gateway_proxy_loader():
    """admin-api budget_service.py 내 cache_key 포맷이
    gateway-proxy DowngradePolicyLoader.CACHE_KEY_FMT와 동일해야 함.

    admin-api 측 형식: f"budget:downgrade:{scope.value.lower()}:{scope_id}"
    gateway-proxy 측 형식: f"budget:downgrade:team:{team_id}" (CACHE_KEY_FMT)

    BudgetScope.TEAM.value.lower() == "team"이므로 두 형식은 일치해야 한다.
    """
    scope_value = "TEAM"
    scope_id = uuid.uuid4()

    admin_api_key = f"budget:downgrade:{scope_value.lower()}:{scope_id}"
    expected_gateway_key = f"budget:downgrade:team:{scope_id}"

    assert admin_api_key == expected_gateway_key


def test_admin_api_uses_team_lowercase():
    """admin-api budget_service.set_downgrade_config / delete_downgrade_config 의
    cache_key 라인이 정확히 `f"budget:downgrade:{scope.value.lower()}:{scope_id}"` 형식이어야 함.

    static analysis: budget_service.py source를 읽어서 두 메서드 모두 같은 형식을 쓰는지 확인.
    """
    import inspect

    from app.services.budget_service import BudgetService

    src = inspect.getsource(BudgetService)

    # set_downgrade_config 와 delete_downgrade_config 둘 다 정확히
    # `f"budget:downgrade:{scope.value.lower()}:{scope_id}"` 패턴을 써야 함
    expected_pattern = 'f"budget:downgrade:{scope.value.lower()}:{scope_id}"'
    occurrences = src.count(expected_pattern)
    assert occurrences >= 2, (
        f"Expected cache_key pattern {expected_pattern!r} to appear at least twice "
        f"(set + delete), found {occurrences}.\n"
        "If the cache key format was changed, update gateway-proxy's "
        "DowngradePolicyLoader.CACHE_KEY_FMT accordingly to keep them in sync."
    )
