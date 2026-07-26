"""agentic Text-to-SQL 샘플 데이터 패키지.

e-커머스 도메인의 결정적(seed 고정) 샘플 데이터를 생성하고
Aurora PostgreSQL(RDS Data API)에 적재하며, 스키마 메타데이터를
OpenSearch에 임베딩 인덱싱하는 도구 모음.
"""

__all__ = ["schema", "generator"]
