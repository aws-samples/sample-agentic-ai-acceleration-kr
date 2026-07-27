"""AgentCore Memory (STM) 세션 매니저 생성.

현재는 short-term memory 만 사용한다: 세션별 대화 히스토리 영속화.
`AgentCoreMemorySessionManager` 는 `bedrock_agentcore.memory.integrations.strands` 소속.
SDK 의존성은 지연 임포트한다.
"""

from __future__ import annotations

from typing import Any


def create_session_manager(
    memory_id: str | None,
    actor_id: str,
    session_id: str,
    region: str,
) -> Any | None:
    """STM 세션 매니저를 생성. memory_id 가 없으면 None(메모리 비활성).

    반환된 세션 매니저는 Strands Agent(session_manager=...) 에 전달한다.
    호출자가 컨텍스트 매니저로 관리하도록 인스턴스를 그대로 반환한다.
    """
    if not memory_id:
        return None
    from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
    from bedrock_agentcore.memory.integrations.strands.session_manager import (
        AgentCoreMemorySessionManager,
    )

    config = AgentCoreMemoryConfig(
        memory_id=memory_id,
        actor_id=actor_id,
        session_id=session_id,
    )
    return AgentCoreMemorySessionManager(config, region_name=region)
