"""agentic Text-to-SQL 오케스트레이터 에이전트.

Strands Graph 기반 파이프라인(intent → schema_linking → sql_generation →
execution(self-correction) → synthesis)을 AgentCore Runtime 에 AG-UI 로 노출한다.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
