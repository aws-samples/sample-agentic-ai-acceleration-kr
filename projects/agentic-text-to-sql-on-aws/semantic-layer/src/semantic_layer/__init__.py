"""agentic Text-to-SQL semantic layer 패키지.

semantic 지식(비즈니스 용어·동의어·few-shot·스키마 메타)의 system-of-record 를
DynamoDB 한 곳에서 CRUD·버전 관리하고(dual-write 금지), DynamoDB Streams 를 통해
Neptune 그래프로 단방향 동기화한다.

- ``repository`` : DynamoDB CRUD + 조건부 쓰기·버전 이력 (``SemanticRepository``)
- ``graph_sync``: Streams 레코드 → openCypher 변환(순수) + Neptune 실행기
- ``lambda_handler``: Streams 이벤트 핸들러(파샬 배치 응답, boto3 표준 런타임만)
- ``seed_semantic``: 초기 seed 스크립트(sample-data 스키마 재사용)
"""

__all__ = ["repository", "graph_sync"]
