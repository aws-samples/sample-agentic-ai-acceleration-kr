"""agentic Text-to-SQL 평가 패키지 (Track A).

AgentCore Evaluations 의 custom **code-based evaluator** Lambda 를 제공한다.
Execution Accuracy(EX): 트레이스 스팬에서 (질문, 생성 SQL) 을 뽑아 goldset 과 매칭하고,
gold SQL 과 생성 SQL 을 read-only(agent_ro) 자격증명으로 각각 실행해 결과셋을 비교한다.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
