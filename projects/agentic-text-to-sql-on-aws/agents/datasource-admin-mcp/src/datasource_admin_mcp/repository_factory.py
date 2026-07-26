"""SemanticRepository / DatasourceRegistry 지연 싱글턴 팩토리.

server 모듈 임포트 시점에 AWS 클라이언트를 만들지 않도록 첫 도구 호출까지 생성을 늦춘다
(단위 테스트가 monkeypatch 로 fake 를 주입할 수 있게 하는 접점도 된다).

DynamoDB 쓰기는 semantic-layer 의 ``SemanticRepository`` 하나만 사용한다 —
dual-write 금지 / 단일 쓰기 지점 원칙(ARCHITECTURE §4.4).
"""

from __future__ import annotations

import os
from typing import Any

from semantic_layer.repository import SemanticRepository

from datasource_admin_mcp.embedding import make_embedder
from datasource_admin_mcp.registry import DatasourceRegistry

_repository: SemanticRepository | None = None
_registry: DatasourceRegistry | None = None


def get_repository() -> SemanticRepository:
    """semantic system-of-record 저장소(지연 생성 싱글턴)."""
    global _repository
    if _repository is None:
        table_name = os.environ.get("SEMANTIC_TABLE_NAME")
        if not table_name:
            raise RuntimeError("환경 변수 SEMANTIC_TABLE_NAME 이 필요합니다.")
        _repository = SemanticRepository(
            table_name,
            region=os.environ.get("AWS_REGION", "us-west-2"),
            # term/fewshot 쓰기 시 Titan 임베딩을 계산한다.
            embedder=make_embedder(),
        )
    return _repository


def get_registry() -> DatasourceRegistry:
    """데이터소스 등록 저장소(지연 생성 싱글턴)."""
    global _registry
    if _registry is None:
        _registry = DatasourceRegistry()
    return _registry


def reset(repository: Any | None = None, registry: Any | None = None) -> None:
    """싱글턴 교체·초기화(테스트 전용)."""
    global _repository, _registry
    _repository = repository
    _registry = registry
