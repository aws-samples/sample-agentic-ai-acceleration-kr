# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import re
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

#: 줄 단위로 주석을 지우면 뜻이 바뀌는 구성 — 하나라도 있으면 스크립트를 그대로 보낸다.
#: 긴 괄호(``[[`` ``[=[`` ``[==[`` …와 짝인 ``]]`` ``]=]`` …: 긴 문자열·블록 주석)와 줄 끝
#: 백슬래시(다음 줄로 이어지는 문자열). 그 안의 ``--`` 줄은 주석이 아니다.
_UNSTRIPPABLE = re.compile(r"\[=*\[|\]=*\]|\\$", re.MULTILINE)


class LuaScriptLoader:
    """Lua 스크립트 파일을 로드하고 EVAL 실행을 지원하는 유틸리티.

    스크립트는 EVAL 마다 **본문 그대로** Redis 로 간다 — 이 코드베이스의 규약이다(EVALSHA 를
    쓰지 않는다). 그래서 로드할 때 전체 줄 주석과 빈 줄을 뺀다(:meth:`strip_comments`). 스크립트
    설명은 대부분 주석이라(한글은 UTF-8 로 글자당 3바이트), 한도를 모두 건 요청 하나(RPM·TPM 검사
    3 스코프, 비용 예약 2, 예산, 서킷 브레이커, TPM·비용 정산)가 보내는 스크립트 본문이 72,989
    바이트에서 29,593 바이트로 준다(실측, 59%).

    EVALSHA(redis-py ``register_script``)로 바꾸지 않은 이유: 검사·정산 EVAL 은 비트랜잭션
    파이프라인으로 나가는데(클러스터에서는 명령마다 그 키의 노드로), redis-py 7.4 의 비동기
    ``Script`` 는 단일 노드 ``Pipeline`` 에만 스크립트를 미리 등록하고 ``ClusterPipeline`` 에는
    하지 않는다. 페일오버·재시작·``SCRIPT FLUSH`` 로 그 노드의 스크립트 캐시가 비면 NOSCRIPT 가
    파이프라인 결과의 명령별 오류로 돌아오고(``raise_on_error=False``), 정산은
    ``*_settle_failed`` 경고만 남기고 건너뛰어진다 — 예약이 창 끝까지 남는다. 본문을 보내는
    EVAL 에는 그런 상태가 없다.
    """

    _scripts: dict[str, str] = {}

    @staticmethod
    def strip_comments(text: str) -> str:
        """전체 줄 주석(``--`` 로 시작하는 줄)과 빈 줄을 뺀다. 나머지 줄은 순서·내용 그대로다.

        줄 끝 주석은 그 줄의 코드와 함께 남는다. 긴 괄호(``[[ … ]]``, ``[=[ … ]=]`` 등 단계가
        있는 것까지 — 긴 문자열·블록 주석)나 줄 끝 백슬래시(다음 줄로 이어지는 문자열)가
        있으면 줄 단위로는 안전하게 지울 수 없으므로 아무것도 빼지 않는다(:data:`_UNSTRIPPABLE`).
        보수적이라 ``a[b[1]]`` 같은 중첩 인덱스도 그대로 보낸다 — 지금 스크립트에는 어느 것도
        없다. 지운 뒤에는 Lua 오류 메시지의 줄 번호가 파일의 줄 번호와 다르다.
        """
        if _UNSTRIPPABLE.search(text):
            return text
        return "".join(
            line + "\n"
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("--")
        )

    @classmethod
    def load_all(cls, script_dir: Path) -> None:
        if not script_dir.exists():
            logger.warning("lua_script_dir_not_found", path=str(script_dir))
            return
        for lua_file in script_dir.glob("*.lua"):
            cls._scripts[lua_file.stem] = cls.strip_comments(lua_file.read_text(encoding="utf-8"))
            logger.debug("lua_script_loaded", name=lua_file.stem)

    @classmethod
    def get(cls, name: str) -> str:
        if name not in cls._scripts:
            raise KeyError(f"Lua script '{name}' not loaded")
        return cls._scripts[name]

    @classmethod
    def is_loaded(cls, name: str) -> bool:
        return name in cls._scripts
