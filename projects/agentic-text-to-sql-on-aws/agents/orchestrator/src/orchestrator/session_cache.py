"""세션별 실행 상태 캐시 (순수 로직, LRU + 크기 상한).

clarification interrupt 재개 전략의 1차 수단. AgentCore Runtime 은 runtimeSessionId 로
같은 microVM 라우팅을 "지향"하지만 프로세스 상태 복원은 보장하지 않으므로, 같은 microVM
내에서라도 이전 요청의 Graph/Agent 인스턴스(interrupt state 보유)를 재사용하려면
모듈 레벨 캐시가 필요하다.

- 키: session_id (AG-UI threadId).
- 값: 임의의 엔트리(Graph/Agent 인스턴스 + 열린 MCP 클라이언트 묶음 등). 캐시는 그 내용을
  알 필요가 없어 Any 로 보관한다.
- 상한 초과 시 LRU(가장 오래 접근되지 않은 항목)를 축출하며, 축출된 엔트리를 반환해
  호출자가 자원(MCP 클라이언트 등)을 정리하게 한다.

SDK 의존이 없어 단위 테스트로 완전 커버한다. 스레드 안전을 위해 Lock 으로 보호한다.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any

DEFAULT_MAX_SIZE = 32


class SessionCache:
    """session_id → 엔트리 LRU 캐시(크기 상한).

    get/put/pop 은 스레드 안전하다. 축출·교체 시 밀려난 엔트리를 반환하므로
    호출자가 그 자원을 닫을 수 있다.
    """

    def __init__(self, max_size: int = DEFAULT_MAX_SIZE) -> None:
        if max_size < 1:
            raise ValueError("max_size 는 1 이상이어야 합니다.")
        self._max_size = max_size
        self._store: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, session_id: str) -> Any | None:
        """엔트리를 조회하고 최근 사용으로 표시(LRU 갱신). 없으면 None."""
        with self._lock:
            if session_id not in self._store:
                return None
            self._store.move_to_end(session_id)
            return self._store[session_id]

    def put(self, session_id: str, entry: Any) -> list[Any]:
        """엔트리를 저장. 같은 키의 기존 엔트리나 상한 초과로 축출된 엔트리 목록을 반환.

        반환된 엔트리는 호출자가 정리(자원 해제)해야 한다.
        """
        evicted: list[Any] = []
        with self._lock:
            if session_id in self._store:
                # 같은 세션의 이전 엔트리는 교체 대상 → 정리하도록 반환.
                evicted.append(self._store.pop(session_id))
            self._store[session_id] = entry
            self._store.move_to_end(session_id)
            while len(self._store) > self._max_size:
                _, old_entry = self._store.popitem(last=False)
                evicted.append(old_entry)
        return evicted

    def pop(self, session_id: str) -> Any | None:
        """엔트리를 제거하고 반환(정상 완료·만료 처리 시). 없으면 None."""
        with self._lock:
            return self._store.pop(session_id, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)

    def __contains__(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._store
