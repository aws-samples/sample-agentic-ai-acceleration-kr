"""deploy.sh 의 런타임 ARN 조회가 페이지네이션에서 깨지지 않는지.

aws CLI 는 자동 페이지네이션 중 `--query` 를 **페이지마다** 적용한다. 매치 없는
페이지에 `| [0]` 를 걸면 `None` 이 나오고, `--output text` 는 페이지별 결과를
줄바꿈으로 이어 붙인다. us-east-1 런타임이 10개(기본 페이지 크기)를 넘은 뒤
tag_runtime 은 "None\\n<arn>" 을 ARN 으로 넘겨 TagResources 가 "not a valid ARN"
으로 거절했다(2026-10-10 실측). 기존 런타임의 태그는 남아 있지만, 이 상태로 새
런타임을 만들면 태그 없이 태어나 Insights 청구 집계에서 빠진다.
`[0]` 없이 리스트만 뽑으면 빈 페이지는 아무 줄도 내지 않는다.

deploy.sh 는 bash 라 여기서는 실제 호출 대신 소스의 쿼리 형태를 고정한다
(test_config_resolution 과 같은 방식).
"""
import os
import re

DEPLOY_SH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "deploy.sh"
)


def _runtime_lookup_queries():
    with open(DEPLOY_SH, encoding="utf-8") as fh:
        src = fh.read()
    return re.findall(r"--query\s+\"(agentRuntimes\[[^\"]*)\"", src)


def test_arn_lookup_exists():
    assert _runtime_lookup_queries(), "deploy.sh no longer looks up the runtime ARN by name"


def test_arn_lookup_does_not_index_per_page():
    for query in _runtime_lookup_queries():
        assert "[0]" not in query, (
            f"{query!r}: `| [0]` is evaluated per page and yields a 'None' line for "
            "every page without the runtime"
        )
