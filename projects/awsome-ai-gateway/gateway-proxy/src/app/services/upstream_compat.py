# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Forward the anthropic-beta values Bedrock InvokeModel can take, and only those.

Claude Code 는 ``ANTHROPIC_BASE_URL`` 뒤에서 쓰고 싶은 beta 기능 이름을 ``anthropic-beta``
헤더로 보낸다. Bedrock InvokeModel 은 beta 를 본문(``anthropic_beta``)으로만 받고, 모르는
이름이 하나라도 섞이면 요청 전체를 400 으로 거부한다(2026-10-05:
``prompt-caching-scope-2026-01-05``). 그래서 통째로 넘길 수 없고, 시험을 통과한 것만
설정(``BEDROCK_FORWARD_BETAS``)에서 골라 넘긴다. 기본값:

- ``dangerous-tool-use`` + 본문 ``safeguards``: Auto mode 서버 분류기. 판정은 응답의
  ``safeguard_results`` 로 온다. 없으면 Claude Code 가 PC 쪽 분류기로 바꾸고 분류용 요청을
  따로 보낸다(비용 + 과금 안내).
- ``per-turn-control``: 대화 중간 메시지의 ``output_config``(턴별 effort). 없으면 2.1.289
  이상의 세션 첫 요청이 400.
- ``inline-tools``: system 메시지의 ``tool_addition`` / ``tool_removal``(대화 중간 도구 추가).
- ``thinking-display-updates``: ``thinking.display: "updates"``.

English: Claude Code puts the beta features it wants in the ``anthropic-beta`` header.
Bedrock InvokeModel takes betas only in the body (``anthropic_beta``) and 400s the whole
request on a name it does not know, so they cannot be forwarded wholesale; only the tested
ones listed in ``BEDROCK_FORWARD_BETAS`` go through (see the Korean list above). The
Mantle path forwards none (unchanged). Test records: docs/beta-headers/.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


#: 한 번만 기록할 이름 수의 상한 — 헤더 값은 클라이언트가 정하기 때문이다.
#: English: cap on names remembered for the log-once message — the header is
#: client-controlled.
_LOG_ONCE_CAP = 64
_logged_dropped_betas: set[str] = set()


def forward_beta_map(raw: str | None) -> dict[str, str | None]:
    """``"a:safeguards, b"`` → ``{"a": "safeguards", "b": None}``;
    빈 값 → ``{}``(끔).

    English: ``"a:safeguards, b"`` → ``{"a": "safeguards", "b": None}``;
    blank → ``{}`` (off).
    """
    out: dict[str, str | None] = {}
    for item in (raw or "").split(","):
        name, _, field = item.partition(":")
        name, field = name.strip(), field.strip()
        if name:
            out[name] = field or None
    return out


def client_betas(header_values: Iterable[str] | None) -> list[str]:
    """``anthropic-beta`` 헤더 줄(여러 줄 가능) → beta 이름 목록. 쉼표로
    나누고, 공백을 지우고, 빈 값을 빼고, 처음 나온 순서를 지키며 중복을
    없앤다(프록시가 여러 줄을 쉼표로 합치기도 하고 그대로 두기도 한다).

    English: ``anthropic-beta`` header line(s) → beta names: split on commas,
    trimmed, blanks dropped, duplicates removed in first-seen order (a proxy may
    join repeated header lines with commas or keep them apart).
    """
    seen: dict[str, None] = {}
    for line in header_values or ():
        for b in str(line).split(","):
            if b.strip():
                seen.setdefault(b.strip(), None)
    return list(seen)


def apply_forwarded_betas(out_body: dict, src_body: Any, betas: list[str],
                          fmap: dict[str, str | None]) -> list[str]:
    """넘길 수 있는 beta 를 ``out_body["anthropic_beta"]`` 에 넣고, 각 beta 의
    짝 필드를 ``src_body`` 에서 복사한다. 넘긴 beta 목록을 돌려준다.

    beta 는 클라이언트가 보냈고, ``fmap`` 에 있고, 짝 필드가 있다면 그 필드가
    ``src_body`` 에 있을 때만 넘긴다. 필드가 없는 beta 는 열 것이 없기
    때문이다(Claude Code 의 보조 요청은 ``safeguards`` 없이 beta 만 붙인다).
    짝 필드는 반드시 그 beta 와 함께만 넘어간다 — ``dangerous-tool-use`` 없는
    ``safeguards`` 는 Bedrock 400 이다. ``out_body`` 에 이미 있던
    ``anthropic_beta`` 값은 유지하고, ``src_body`` 는 바꾸지 않는다. ``fmap``
    에 없는 이름은 프로세스당 한 번만 기록한다. 예외를 던지지 않으며, 뜻밖의
    오류가 나면 아무것도 넣지 않는다(이전 동작).

    English: put the forwardable betas into ``out_body["anthropic_beta"]`` and
    copy each one's paired field from ``src_body``. Returns the betas forwarded.

    A beta is forwarded when the client sent it, it is in ``fmap``, and — if it
    has a paired field — that field is in ``src_body``: a beta whose field is
    absent opens nothing (Claude Code's helper requests carry the beta without
    ``safeguards``). A paired field travels ONLY with its beta: ``safeguards``
    without ``dangerous-tool-use`` is a Bedrock 400. Values already in
    ``out_body["anthropic_beta"]`` are kept; ``src_body`` is never mutated.
    Names not in ``fmap`` are logged once per process. Never raises: on an
    unexpected error nothing is added (the previous behaviour).
    """
    try:
        src = src_body if isinstance(src_body, dict) else {}
        kept = [b for b in betas
                if b in fmap and (fmap[b] is None or fmap[b] in src)]
        fields = {fmap[b]: src[fmap[b]] for b in kept if fmap[b]}
        existing = out_body.get("anthropic_beta")
        merged = list(existing) if isinstance(existing, list) else []
        merged += [b for b in kept if b not in merged]
    except Exception:
        logger.warning("upstream_compat.beta_forward_failed", exc_info=True)
        return []
    if kept:
        out_body.update(fields)
        out_body["anthropic_beta"] = merged
    _log_dropped_once([b for b in betas if b not in fmap])
    return kept


def _log_dropped_once(names: list[str]) -> None:
    """버린 beta 이름을 프로세스당 한 번씩, ``_LOG_ONCE_CAP`` 개까지만 기록한다.

    English: log beta names we drop, once each per process, up to
    ``_LOG_ONCE_CAP`` names.
    """
    room = _LOG_ONCE_CAP - len(_logged_dropped_betas)
    new = [n for n in names if n not in _logged_dropped_betas][:max(room, 0)]
    if new:
        _logged_dropped_betas.update(new)
        logger.info("upstream_compat.beta_dropped", betas=new)
