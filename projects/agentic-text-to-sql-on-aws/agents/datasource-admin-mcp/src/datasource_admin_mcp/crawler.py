"""스키마 크롤러 — 커넥터 크롤 결과를 semantic 엔티티로 적재한다.

크롤 산출물은 항상 **candidate** 로 기록한다(§8.3): Manager 승인(publish) 후에야
Streams → OpenSearch/Neptune 로 전파되어 검색에 반영된다. 자동 크롤이 운영 검색 품질을
즉시 흔들지 않도록 하는 안전장치(개선 파이프라인 Track B 와 동일한 승인 게이트).
"""

from __future__ import annotations

from typing import Any

from datasource_admin_mcp.connectors import CrawledSchema, DatasourceConnector


class SchemaCrawler:
    """커넥터에서 크롤한 스키마를 SemanticRepository 에 candidate 로 적재한다."""

    def __init__(self, connector: DatasourceConnector, repository: Any) -> None:
        self._connector = connector
        self._repository = repository

    def crawl_into_repository(self, actor: str = "admin-panel") -> dict[str, int]:
        """크롤 → put_entity(candidate). entity_type 별 적재 건수를 반환.

        적재 순서는 table → column → join (그래프 MERGE 의존성상 안전한 순서 —
        seed_semantic 과 동일).
        """
        crawled = self._connector.crawl()
        counts = {"tables": 0, "columns": 0, "joins": 0}
        for entities, key in (
            (crawled.tables, "tables"),
            (crawled.columns, "columns"),
            (crawled.joins, "joins"),
        ):
            for entity in entities:
                self._repository.put_entity(
                    entity["entity_type"],
                    entity["entity_id"],
                    entity["payload"],
                    status="candidate",
                    actor=actor,
                )
                counts[key] += 1
        return counts

    def preview(self) -> CrawledSchema:
        """적재 없이 크롤 결과만 반환(디버깅·검증용)."""
        return self._connector.crawl()
