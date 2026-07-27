"""FastMCP 엔트리포인트 — datasource-admin-mcp (관리 평면).

AgentCore Runtime MCP 호스팅 규격(2026-07 검증): Host 0.0.0.0, 포트 8000, POST /mcp,
stateless streamable-HTTP. ``FastMCP(host="0.0.0.0", stateless_http=True)`` +
``mcp.run(transport="streamable-http")``.

도구 10종:
  큐레이션·승인: list_entities / get_entity / put_entity / publish_entity /
                unpublish_entity / reject_entity
  데이터소스:    register_datasource / test_datasource / crawl_schema
  개선 파이프라인(Track B): mine_candidates

admin web 은 DynamoDB 를 직접 쓰지 않고 **사용자 JWT → Gateway MCP → 이 서버**를 경유한다
(semantic 쓰기 경로 단일화 + Cedar 인가 강제 + 사용자별 OBO 실현).

모든 도구는 dict 를 반환하고 실패는 ``{"status":"error","message":"타입: 메시지"}`` 로
정규화한다. 시크릿 값은 절대 로깅·응답하지 않는다.

주의: 도구 함수 어노테이션에 **지연 임포트 타입을 쓰지 않는다.**
``from __future__ import annotations`` 하에서 스키마 생성 시 모듈 전역에서 평가되기 때문.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from datasource_admin_mcp.crawler import SchemaCrawler
from datasource_admin_mcp.miner import MAX_REPORTED_CANDIDATES, CandidateMiner
from datasource_admin_mcp.registry import (
    BUILTIN_DATASOURCES,
    VALID_ENGINES,
    RegistryError,
    build_builtin_connector,
    sanitize_config,
)
from datasource_admin_mcp.repository_factory import get_registry, get_repository

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger("datasource_admin_mcp.server")

mcp = FastMCP(host="0.0.0.0", stateless_http=True)

# 응답에서 제거할 무거운 필드(임베딩 벡터 1024차원 — 관리 UI 에 불필요).
_STRIPPED_FIELDS = ("embedding",)


def _error(exc: Exception) -> dict[str, Any]:
    """예외를 계약상 error 응답으로 정규화(타입+메시지만 — 시크릿 유출 방지)."""
    message = f"{type(exc).__name__}: {exc}"
    logger.warning("admin_tool_error: %s", message)
    return {"status": "error", "message": message}


def _strip(entity: dict[str, Any] | None) -> dict[str, Any] | None:
    """엔티티에서 embedding 등 대용량 필드를 제거(payload 경량화)."""
    if entity is None:
        return None
    return {key: value for key, value in entity.items() if key not in _STRIPPED_FIELDS}


# --- semantic 큐레이션 도구 ---------------------------------------------------


@mcp.tool()
def list_entities(entity_type: str | None = None, status: str | None = None) -> dict[str, Any]:
    """semantic 엔티티 최신본 목록을 조회한다(관리 UI 큐레이션·승인 대기 목록).

    Args:
        entity_type: 필터할 엔티티 타입. term|fewshot|table|column|join|datasource. None 이면 전체.
        status: 필터할 상태. candidate(승인 대기) | published(운영 반영). None 이면 전체.

    Returns:
        {"status":"ok","entities":[{pk,sk,entity_type,entity_id,status,version,
         updated_at,updated_by,...payload}]}  (embedding 필드는 제거됨)
        실패: {"status":"error","message":"..."}
    """
    try:
        entities = get_repository().list_entities(entity_type=entity_type, status=status)
    except Exception as exc:  # noqa: BLE001 — 계약상 error 로 정규화
        return _error(exc)
    return {"status": "ok", "entities": [_strip(entity) for entity in entities]}


@mcp.tool()
def get_entity(entity_type: str, entity_id: str) -> dict[str, Any]:
    """semantic 엔티티 최신본(v0) 하나를 조회한다.

    Args:
        entity_type: term|fewshot|table|column|join|datasource.
        entity_id: 엔티티 식별자(예: term 은 "revenue", column 은 "orders.total_amount").

    Returns:
        {"status":"ok","entity":{...}|None}  — 없으면 entity 는 None.
        실패: {"status":"error","message":"..."}
    """
    try:
        entity = get_repository().get_entity(entity_type, entity_id)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)
    return {"status": "ok", "entity": _strip(entity)}


@mcp.tool()
def put_entity(
    entity_type: str,
    entity_id: str,
    payload: dict,
    status: str = "candidate",
    actor: str = "admin-panel",
) -> dict[str, Any]:
    """semantic 엔티티를 생성·수정한다(항목 단위 버전 증가, 기본 candidate).

    term/fewshot 은 hybrid 검색용 임베딩을 쓰기 시점에 계산해 포함한다.

    Args:
        entity_type: term|fewshot|table|column|join|datasource.
        entity_id: 엔티티 식별자.
        payload: 엔티티 본문(term 은 term/definition/synonyms/sql_fragment/maps_to 등).
        status: candidate(기본, 승인 대기) | published.
        actor: 감사 기록용 행위자(admin web 이 JWT username 전달).

    Returns:
        {"status":"ok","entity":{...}}  — 기록된 새 최신본.
        실패: {"status":"error","message":"..."}
    """
    try:
        entity = get_repository().put_entity(
            entity_type, entity_id, payload, status=status, actor=actor
        )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)
    return {"status": "ok", "entity": _strip(entity)}


@mcp.tool()
def publish_entity(
    entity_type: str, entity_id: str, actor: str = "admin-panel"
) -> dict[str, Any]:
    """candidate 엔티티를 승인(published)한다 — 파생 저장소로 전파된다.

    Args:
        entity_type: term|fewshot|table|column|join|datasource.
        entity_id: 엔티티 식별자.
        actor: 감사 기록용 행위자(승인자).

    Returns:
        {"status":"ok","entity":{...}}  (status=published)
        실패: {"status":"error","message":"..."}
    """
    try:
        entity = get_repository().publish(entity_type, entity_id, actor=actor)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)
    return {"status": "ok", "entity": _strip(entity)}


@mcp.tool()
def unpublish_entity(
    entity_type: str, entity_id: str, actor: str = "admin-panel"
) -> dict[str, Any]:
    """published 엔티티를 candidate 로 강등한다 — 파생 저장소에서 제거된다.

    Args:
        entity_type: term|fewshot|table|column|join|datasource.
        entity_id: 엔티티 식별자.
        actor: 감사 기록용 행위자.

    Returns:
        {"status":"ok","entity":{...}}  (status=candidate)
        실패: {"status":"error","message":"..."}
    """
    try:
        entity = get_repository().unpublish(entity_type, entity_id, actor=actor)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)
    return {"status": "ok", "entity": _strip(entity)}


@mcp.tool()
def reject_entity(
    entity_type: str, entity_id: str, reason: str = "", actor: str = "admin-panel"
) -> dict[str, Any]:
    """candidate 엔티티를 반려(rejected)한다 — 승인 대기 큐에서 제외되고 이력이 남는다.

    rejected 는 published 가 아니므로 파생 저장소(OpenSearch/Neptune)에 노출되지 않는다.
    채굴기(mine_candidates)는 rejected 도 "이미 존재" 로 보아 재적재하지 않는다.
    반려 후에도 publish_entity 로 재승인, unpublish_entity 로 재검토(candidate) 복귀가 가능하다.

    Args:
        entity_type: term|fewshot|table|column|join|datasource.
        entity_id: 엔티티 식별자.
        reason: 반려 사유(감사 기록용 — payload 의 rejection_reason 으로 저장). 빈 값이면 미기록.
        actor: 감사 기록용 행위자(반려자).

    Returns:
        {"status":"ok","entity":{...}}  (status=rejected)
        실패: {"status":"error","message":"..."}
    """
    try:
        entity = get_repository().reject(entity_type, entity_id, reason=reason, actor=actor)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)
    return {"status": "ok", "entity": _strip(entity)}


# --- 개선 파이프라인 Track B: 후보 채굴 ----------------------------------------


@mcp.tool()
def mine_candidates(hours: int = 24, actor: str = "mining-batch") -> dict[str, Any]:
    """운영 로그에서 semantic 후보(fewshot/term)를 채굴해 candidate 로 적재한다.

    orchestrator 가 남긴 구조화 로그(`t2sql_query_record`)를 CloudWatch Logs 에서 읽어
    성공 질의는 fewshot 후보로, 실패·재질의 질문에서 반복 등장하는 표현은 term 후보로
    적재한다. 적재는 전부 **candidate** 이므로 Manager 승인(publish) 후에야 검색에 반영된다.
    동일 entity_id(질문 해시)가 이미 있으면(rejected 포함) 재적재하지 않는다.

    Args:
        hours: 스캔할 최근 시간 범위(기본 24시간).
        actor: 감사 기록용 행위자(기본 mining-batch).

    Returns:
        {"status":"ok","scanned":N,"mined":N,"skipped_existing":N,
         "candidates":[{"entity_type":"fewshot|term","entity_id":"mined-..."}]}
        (candidates 는 응답 경량화를 위해 최대 50건까지만 실린다 — 카운트는 전체값)
        실패: {"status":"error","message":"..."}
    """
    try:
        summary = CandidateMiner(get_repository()).mine(hours=hours, actor=actor)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)
    return {
        "status": "ok",
        "scanned": summary["scanned"],
        "mined": summary["mined"],
        "skipped_existing": summary["skipped_existing"],
        "candidates": summary["candidates"][:MAX_REPORTED_CANDIDATES],
    }


# --- 데이터소스 관리 도구 -----------------------------------------------------


@mcp.tool()
def register_datasource(
    datasource_id: str,
    engine: str,
    config: dict,
    actor: str = "admin-panel",
) -> dict[str, Any]:
    """데이터소스 연결 설정을 등록한다(시크릿 저장 + 연결 메타 기록).

    자격증명 포함 config 는 Secrets Manager `agentic-t2sql/datasource/<id>` 에 저장하고,
    자격증명을 **제외한** 메타(engine·host·database 등)만 DynamoDB
    entity_type="datasource" 엔티티(candidate)로 기록한다.

    Args:
        datasource_id: 데이터소스 식별자(시크릿 이름 suffix).
        engine: aurora-postgresql | redshift-serverless.
        config: 연결 설정(host/port/database/username/password 등).
        actor: 감사 기록용 행위자.

    Returns:
        {"status":"ok","secret_arn":"arn:aws:secretsmanager:..."}
        실패: {"status":"error","message":"..."}
    """
    try:
        if not datasource_id or "/" in datasource_id:
            raise ValueError(f"잘못된 datasource_id: {datasource_id!r}")
        if engine not in VALID_ENGINES:
            raise ValueError(
                f"지원하지 않는 engine: {engine!r} (허용: {', '.join(VALID_ENGINES)})"
            )
        if not isinstance(config, dict) or not config:
            raise ValueError("config 는 비어 있지 않은 객체여야 합니다.")

        registry = get_registry()
        secret_arn = registry.store_config(datasource_id, config)

        meta = sanitize_config(config)
        meta.update(
            {
                "datasource_id": datasource_id,
                "engine": engine,
                "secret_name": registry.secret_name(datasource_id),
            }
        )
        get_repository().put_entity(
            "datasource", datasource_id, meta, status="candidate", actor=actor
        )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)
    return {"status": "ok", "secret_arn": secret_arn}


@mcp.tool()
def test_datasource(datasource_id: str) -> dict[str, Any]:
    """데이터소스 연결을 점검한다.

    내장 소스(aurora/redshift)는 Data API 로 `SELECT 1` 을 실행한다. 등록된 커스텀
    소스는 PUBLIC runtime 에서 직접 네트워크 연결이 불가하므로 시크릿 존재 + 필수 키
    검증까지만 수행한다(시크릿 값은 반환하지 않는다).

    Args:
        datasource_id: 점검할 데이터소스 식별자(aurora | redshift | 등록된 커스텀 id).

    Returns:
        {"status":"ok","ok":true|false,"detail":"..."}
        실패: {"status":"error","message":"..."}
    """
    try:
        if datasource_id in BUILTIN_DATASOURCES:
            detail = build_builtin_connector(datasource_id).test_connection()
        else:
            detail = get_registry().validate_secret(datasource_id)
    except RegistryError as exc:
        # 도메인 오류(미등록·키 누락)는 ok=false 로 표면화(운영자가 조치 가능한 상태).
        return {"status": "ok", "ok": False, "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"status": "ok", "ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    return {"status": "ok", "ok": True, "detail": detail}


@mcp.tool()
def crawl_schema(datasource_id: str, actor: str = "admin-panel") -> dict[str, Any]:
    """데이터소스의 information_schema 를 크롤해 스키마 엔티티를 적재한다.

    table/column/join 엔티티를 **candidate** 로 기록한다 — Manager 승인(publish) 후에야
    파생 저장소(OpenSearch/Neptune)로 전파되어 검색에 반영된다.

    Args:
        datasource_id: 크롤 대상(aurora | redshift — Data API 로 접근 가능한 내장 소스).
        actor: 감사 기록용 행위자.

    Returns:
        {"status":"ok","tables":N,"columns":N,"joins":N}
        실패: {"status":"error","message":"..."}
    """
    try:
        if datasource_id not in BUILTIN_DATASOURCES:
            raise ValueError(
                f"크롤은 내장 데이터소스만 지원합니다: {datasource_id!r} "
                f"(허용: {', '.join(BUILTIN_DATASOURCES)}). "
                "커스텀 소스는 PUBLIC runtime 에서 직접 연결할 수 없습니다."
            )
        connector = build_builtin_connector(datasource_id)
        counts = SchemaCrawler(connector, get_repository()).crawl_into_repository(actor=actor)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)
    return {"status": "ok", **counts}


def main() -> None:
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
