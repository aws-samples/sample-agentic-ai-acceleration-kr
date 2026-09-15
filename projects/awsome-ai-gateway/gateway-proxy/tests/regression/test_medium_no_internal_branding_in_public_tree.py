# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""사내 조직명이 공개 트리에 남아 있지 않은지.

무엇이 문제였나
---------------
de-brand 는 완료된 것으로 여겨졌지만, **사용자에게 직접 보이는 문자열**에 사내 조직명이
남아 있었다:

  * notification-worker 의 이메일 제목 템플릿 11개 전부 — 예산 경고, 키 만료/무효화, 인증
    실패 급증, 서비스 저하 등. 즉 이 게이트웨이가 보내는 **모든 알림 메일의 제목**에
    조직명이 실렸다.
  * ``db/init/03_seed_data.sql`` 의 admin JWT issuer/audience 시드 값 2개.
  * ``admin-api`` 의 ``DATABASE_URL`` 기본값에 박힌 DB 이름(그 값은 docker-compose 의
    ``POSTGRES_DB`` 기본값과도 어긋나 있어서, 조직명 제거와 함께 정정됐다).

이것이 코드 주석에 남은 것과 다른 이유: 메일 제목은 수신자에게 전달되고, 시드 값은 새로
설치하는 모든 환경에 들어간다. 공개 저장소에서는 두 경로 모두 되돌릴 수 없다.

⚠️ 이 검사는 **자기 자신을 제외**한다. 이 세션에서 같은 함정을 두 번 밟았다 — 가드가
   자기 docstring/주석에 적힌 금지 문자열을 찾아내 실패하는 것. 그래서 제외 목록에 이
   파일을 넣고, 동시에 "제외가 너무 넓어 실제 유출을 놓치지 않는지" 를 별도 테스트로
   고정한다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# 프로젝트 루트: gateway-proxy/tests/regression → gateway-proxy → <project>
_PROJECT_ROOT = Path(__file__).resolve().parents[3]

_THIS_FILE = Path(__file__).resolve()

# 레거시 사내 접두사 파생형. 대소문자 무시.
#
# ⚠️ 이 목록은 **공개 저장소에 남겨도 되는 형태로만** 적는다. 금지어를 그대로 적는 가드는
#    그 자체가 금지어를 공개 트리에 들여놓는다 — 회사명 자체는 여기 두지 않고, 실제로
#    유출됐던 중립적 접두사 형태만 남긴다. 회사명 스캔은 납품 전 로컬 점검의 몫이다.
#
# 구분자만 바꿔 되살아나는 것이 가장 흔한 회귀이므로 ``-``/``_``/공백을 모두 잡는다.
#
# ⚠️ **왼쪽 단어 경계가 필수다.** 처음에 경계 없이 썼더니 ``holds gateway`` 와
#    ``needs_gateway`` 가 걸렸다 — 앞 단어가 s 로 끝나면 "…ds gateway" 가 되기 때문이다.
#    아래 오탐 대조군이 그것을 잡아냈고, 그 대조군이 없으면 이 가드는 정상 코드를 고치라고
#    요구하면서 동시에 신뢰를 잃는다(끄고 싶어지는 가드는 결국 꺼진다).
_DENY = [
    re.compile(r"\bds[-_ ]gateway", re.IGNORECASE),
    re.compile(r"\bds[-_]llm", re.IGNORECASE),
]

_SKIP_SUFFIXES = {
    ".pyc", ".pack", ".idx", ".png", ".jpg", ".jpeg", ".gif", ".ico",
    ".woff", ".woff2", ".pdf", ".pptx", ".docx", ".xlsx",
}


def _candidate_files():
    """**git 이 추적하는** 파일만 본다.

    ⚠️ 처음에는 작업 트리를 ``rglob`` 로 훑었는데, 그러면 로컬 빌드 산출물이 걸린다 —
       실제로 ``admin-ui/tsconfig.tsbuildinfo``(gitignore 대상)가 내 로컬 절대 경로를
       담고 있어서 오탐이 났다. 공개 저장소에 무엇이 들어 있는지가 질문이므로, 추적 대상이
       정확한 범위다. 부수 효과로 ``node_modules`` / ``.next`` / ``.venv`` 제외가 불필요해진다
       (제외 목록이 길어지면 그 자체가 유출을 놓치는 경로가 된다).
    """
    import subprocess

    try:
        out = subprocess.run(
            ["git", "-C", str(_PROJECT_ROOT), "ls-files", "-z"],
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover
        pytest.skip(f"git 을 쓸 수 없다: {exc}")

    for rel in out.split(b"\0"):
        if not rel:
            continue
        path = _PROJECT_ROOT / rel.decode()
        if not path.is_file():
            continue  # 삭제 예정으로 인덱스에만 남은 항목
        if path.suffix.lower() in _SKIP_SUFFIXES:
            continue
        if path.resolve() == _THIS_FILE:
            continue  # 이 가드가 금지 문자열을 설명하고 있다
        yield path


def _hits():
    found: list[tuple[str, int, str]] = []
    for path in _candidate_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for pattern in _DENY:
                if pattern.search(line):
                    rel = path.relative_to(_PROJECT_ROOT)
                    found.append((str(rel), lineno, line.strip()[:120]))
                    break
    return found


def test_the_public_tree_carries_no_internal_organisation_name():
    """⚠️ 사용자에게 보이는 문자열이 특히 중요하다 — 메일 제목과 시드 값은 되돌릴 수 없다."""
    hits = _hits()
    assert not hits, "사내 조직명이 남아 있다:\n" + "\n".join(
        f"  {f}:{n}  {line}" for f, n, line in hits
    )


def test_the_scan_actually_reaches_the_notification_templates():
    """⚠️ 전제 고정. 스캔 범위가 좁아지면 위 테스트는 "유출 없음" 으로 조용히 통과한다.

    유출이 실제로 있었던 디렉터리에 스캔이 닿는지 직접 확인한다 — 경로 계산이 한 단계
    어긋나면(``parents[3]`` → ``parents[2]``) 게이트웨이 하위만 훑고 지나간다.
    """
    scanned = {str(p.relative_to(_PROJECT_ROOT)) for p in _candidate_files()}
    assert any(
        s.startswith("notification-worker/src/worker/templates/") for s in scanned
    ), f"알림 템플릿 디렉터리를 스캔하지 않는다 (스캔한 파일 {len(scanned)}개)"
    assert any(s.startswith("db/init/") for s in scanned), "db/init 을 스캔하지 않는다"
    assert any(s.startswith("admin-ui/src/") for s in scanned), "admin-ui 를 스캔하지 않는다"
    assert len(scanned) > 500, f"스캔 대상이 {len(scanned)}개뿐이다 — 제외 규칙이 너무 넓다"


def test_the_denylist_would_catch_each_form_that_actually_leaked():
    """⚠️ 대조군. 패턴이 실제 유출 형태를 잡는지 — 구분자만 바꿔 되살아나는 것이 흔하다."""
    leaked_forms = [
        "[DS Gateway] 예산 80% 도달 알림",
        "'ds-gateway-admin'",
        "postgresql+asyncpg://u:p@localhost:5432/ds_gateway",
        "DS_GATEWAY_ADMIN",
    ]
    for form in leaked_forms:
        assert any(p.search(form) for p in _DENY), f"패턴이 {form!r} 을 놓친다"


@pytest.mark.parametrize(
    "innocent",
    [
        "gateway_chat_reader",
        "llm-gateway-admin",
        "AWSome AI Gateway",
        "def test_require_gateway_mode_needs_gateway_and_cognito():",
        "a later one holds gateway's own values",
    ],
)
def test_the_denylist_does_not_fire_on_innocent_strings(innocent: str):
    """⚠️ 반대 방향 대조군. 지나치게 넓으면 정상 문자열을 고치라고 요구하게 된다.

    아래 두 줄은 실제로 초기 스캔에서 오탐이었다(``gateway_mode`` / ``gateway's``).
    """
    assert not any(p.search(innocent) for p in _DENY), f"오탐: {innocent!r}"
