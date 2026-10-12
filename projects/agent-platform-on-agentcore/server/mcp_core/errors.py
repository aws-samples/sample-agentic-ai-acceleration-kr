"""MCP 세션에서 올라온 예외를 사람이 읽을 수 있는 원인으로 바꾼다."""
from typing import List


def root_causes(exc: BaseException) -> str:
    """ExceptionGroup을 펼쳐 실제 원인을 문자열로 만든다.

    MCP 세션은 anyio task group 안에서 열리므로, 안에서 난 오류가
    "unhandled errors in a TaskGroup (1 sub-exception)"으로 뭉개진다. 그 메시지만
    보면 원인을 전혀 알 수 없다 — 배포 환경에서 SigV4 누락을 진단하는 데 이것 때문에
    시간을 썼다.
    """
    leaves: List[str] = []

    def walk(e: BaseException) -> None:
        if isinstance(e, BaseExceptionGroup):
            for sub in e.exceptions:
                walk(sub)
        else:
            leaves.append(f"{type(e).__name__}: {e}")

    walk(exc)
    return " / ".join(leaves) or f"{type(exc).__name__}: {exc}"
