"""데이터소스 등록 저장소 — Secrets Manager(자격증명) + 커넥터 팩토리.

역할 분리
--------
- **자격증명**: Secrets Manager ``agentic-t2sql/datasource/<datasource_id>`` (env
  ``DATASOURCE_SECRET_PREFIX``). 값은 절대 로깅·응답에 실지 않는다.
- **연결 메타**(engine·설명 등 자격증명 제외분): DynamoDB semantic 테이블의
  ``entity_type="datasource"`` 엔티티(candidate) — 승인 흐름을 다른 엔티티와 통일.

내장 소스(aurora/redshift)는 CDK 가 주입한 env 로 커넥터를 만들 수 있어 시크릿 등록이
불필요하다. 등록된 커스텀 소스는 PUBLIC runtime(VPC 밖)에서 직접 네트워크 연결이 불가하므로
``test_datasource`` 가 시크릿 존재 + 필수 키 검증까지만 수행한다.
"""

from __future__ import annotations

import json
import os
from typing import Any

from datasource_admin_mcp.connectors import (
    AuroraDataApiConnector,
    DatasourceConnector,
    RedshiftDataApiConnector,
)

DEFAULT_SECRET_PREFIX = "agentic-t2sql/datasource/"

# 내장(built-in) 데이터소스 — env 로 커넥터를 만들 수 있는 소스.
BUILTIN_DATASOURCES = ("aurora", "redshift")

# 지원 엔진(§8.3 시그니처).
VALID_ENGINES = ("aurora-postgresql", "redshift-serverless")

# 등록 시크릿에 반드시 있어야 하는 키(엔진 공통 최소 집합).
REQUIRED_SECRET_KEYS = ("host", "database", "username", "password")

# 시크릿에 저장하되 DynamoDB 메타에는 절대 남기지 않는 키.
CREDENTIAL_KEYS = frozenset({"password", "secret", "token", "private_key", "credentials"})


class RegistryError(RuntimeError):
    """등록/조회 오류(존재하지 않는 소스, 필수 키 누락 등)."""


def secret_name(datasource_id: str, prefix: str | None = None) -> str:
    """datasource_id → Secrets Manager 시크릿 이름."""
    resolved = prefix if prefix is not None else os.environ.get(
        "DATASOURCE_SECRET_PREFIX", DEFAULT_SECRET_PREFIX
    )
    if not resolved.endswith("/"):
        resolved += "/"
    return f"{resolved}{datasource_id}"


def sanitize_config(config: dict[str, Any]) -> dict[str, Any]:
    """자격증명 키를 제거한 메타 dict 반환(DynamoDB 기록용)."""
    return {
        key: value
        for key, value in config.items()
        if key.lower() not in CREDENTIAL_KEYS
    }


class DatasourceRegistry:
    """Secrets Manager 기반 데이터소스 등록 저장소.

    boto3 secretsmanager 클라이언트는 주입 가능(단위 테스트용 fake).
    """

    def __init__(
        self,
        region: str | None = None,
        prefix: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.region = region or os.environ.get("AWS_REGION", "us-west-2")
        self.prefix = prefix if prefix is not None else os.environ.get(
            "DATASOURCE_SECRET_PREFIX", DEFAULT_SECRET_PREFIX
        )
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("secretsmanager", region_name=self.region)
        return self._client

    def secret_name(self, datasource_id: str) -> str:
        return secret_name(datasource_id, self.prefix)

    def store_config(self, datasource_id: str, config: dict[str, Any]) -> str:
        """연결 설정을 시크릿에 저장하고 secret ARN 을 반환(멱등).

        신규는 ``create_secret``, 이미 있으면 ``put_secret_value`` 로 새 버전을 만든다.
        (Secrets Manager 는 ResourceExistsException 을 클라이언트 예외 클래스로 노출한다.)
        """
        name = self.secret_name(datasource_id)
        payload = json.dumps(config, ensure_ascii=False)
        try:
            response = self.client.create_secret(
                Name=name,
                SecretString=payload,
                Description=f"agentic-t2sql 데이터소스 연결 설정: {datasource_id}",
            )
            return response["ARN"]
        except Exception as exc:  # noqa: BLE001 — 존재 시에만 put 으로 폴백, 그 외 재전파
            if not _is_already_exists(self.client, exc):
                raise
            response = self.client.put_secret_value(SecretId=name, SecretString=payload)
            return response["ARN"]

    def describe(self, datasource_id: str) -> dict[str, Any]:
        """시크릿 메타데이터(값 제외)를 반환. 없으면 RegistryError."""
        name = self.secret_name(datasource_id)
        try:
            return self.client.describe_secret(SecretId=name)
        except Exception as exc:  # noqa: BLE001 — 없음을 도메인 오류로 정규화
            raise RegistryError(f"등록되지 않은 데이터소스입니다: {datasource_id}") from exc

    def validate_secret(self, datasource_id: str) -> str:
        """시크릿 존재 + 필수 키 보유를 검증하고 detail 문자열을 반환.

        PUBLIC runtime 에서 커스텀 소스로 직접 네트워크 연결이 불가하므로(VPC 밖),
        여기까지가 test_datasource 의 최대 검증 범위다. 시크릿 **값은 반환하지 않는다.**
        """
        name = self.secret_name(datasource_id)
        self.describe(datasource_id)
        try:
            response = self.client.get_secret_value(SecretId=name)
            config = json.loads(response.get("SecretString") or "{}")
        except RegistryError:
            raise
        except Exception as exc:  # noqa: BLE001 — 파싱/권한 오류를 도메인 오류로 정규화
            raise RegistryError(
                f"시크릿을 읽을 수 없습니다({datasource_id}): {type(exc).__name__}"
            ) from exc

        if not isinstance(config, dict):
            raise RegistryError(f"시크릿 형식 오류({datasource_id}): JSON 객체가 아닙니다.")

        missing = [key for key in REQUIRED_SECRET_KEYS if not config.get(key)]
        if missing:
            raise RegistryError(
                f"시크릿 필수 키 누락({datasource_id}): {', '.join(missing)}"
            )
        # 키 이름만 노출(값 금지).
        return (
            f"{datasource_id}: 시크릿 존재·필수 키 검증 통과 "
            f"(키={sorted(config.keys())}). PUBLIC runtime 이라 직접 연결 점검은 생략."
        )


def build_builtin_connector(datasource_id: str) -> DatasourceConnector:
    """내장 데이터소스의 커넥터 생성. env 미설정은 KeyError 로 전파된다."""
    if datasource_id == "aurora":
        return AuroraDataApiConnector()
    if datasource_id == "redshift":
        return RedshiftDataApiConnector()
    raise RegistryError(f"내장 데이터소스가 아닙니다: {datasource_id}")


def _is_already_exists(client: Any, exc: Exception) -> bool:
    """ResourceExistsException 여부 판정(boto3 예외 클래스 + 이름 기반 폴백)."""
    exceptions = getattr(client, "exceptions", None)
    existing = getattr(exceptions, "ResourceExistsException", None)
    if existing is not None and isinstance(exc, existing):
        return True
    return type(exc).__name__ == "ResourceExistsException"
