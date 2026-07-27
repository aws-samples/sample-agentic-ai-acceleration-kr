"""Redshift Data API 헬퍼.

boto3 ``redshift-data`` 클라이언트를 감싸 비동기 statement 실행을 단순화한다.
Redshift Data API 는 비동기이므로 ``execute_statement`` → ``describe_statement`` 폴링
(FINISHED/FAILED/ABORTED) → 필요 시 ``get_statement_result`` 순서로 동작한다.

인증: ``secret_arn``(namespace 관리자 시크릿, manageAdminPassword 가 생성한
``redshift!<namespace>-<admin>`` 시크릿)을 주면 관리자 권한으로 실행된다 —
seed 의 DDL/DML/사용자 생성에 필요. SecretArn 없이 workgroup 이름만으로 호출하면
호출자 IAM 자격증명에 매핑된 DB 사용자(IAM:...)로 실행되어 CREATE USER 권한이 없다.

boto3 클라이언트와 sleep 함수를 주입할 수 있어 단위 테스트가 AWS 없이 검증한다.
"""

from __future__ import annotations

import time
from collections.abc import Callable

# describe_statement 의 종료 상태.
FINISHED = "FINISHED"
_TERMINAL_FAILURES = ("FAILED", "ABORTED")

DEFAULT_POLL_INTERVAL_SEC = 0.5
DEFAULT_TIMEOUT_SEC = 120.0


class RedshiftDataApiError(RuntimeError):
    """Redshift Data API statement 실패/타임아웃."""


class RedshiftDataApiClient:
    """redshift-data 클라이언트 래퍼.

    ``secret_arn``(관리자 시크릿)을 주면 그 자격증명으로, 없으면 호출자 IAM 매핑
    사용자로 실행된다. seed 는 관리자 시크릿을 사용한다.
    """

    def __init__(
        self,
        client,
        workgroup: str,
        database: str,
        *,
        secret_arn: str | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SEC,
        timeout: float = DEFAULT_TIMEOUT_SEC,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._workgroup = workgroup
        self._database = database
        self._secret_arn = secret_arn
        self._poll_interval = poll_interval
        self._timeout = timeout
        self._sleep = sleep

    @property
    def database(self) -> str:
        return self._database

    def _base_kwargs(self, database: str | None = None) -> dict:
        kwargs = {
            "WorkgroupName": self._workgroup,
            "Database": database if database is not None else self._database,
        }
        if self._secret_arn:
            kwargs["SecretArn"] = self._secret_arn
        return kwargs

    def execute(self, sql: str, *, database: str | None = None) -> dict:
        """단일 statement 실행 후 종료까지 폴링. 실패 시 예외. describe_statement 결과 반환."""
        submit = self._client.execute_statement(
            **self._base_kwargs(database), Sql=sql
        )
        return self._wait_until_done(submit["Id"])

    def batch(self, sqls: list[str], *, database: str | None = None) -> dict:
        """여러 statement 를 batch_execute_statement 로 묶어 실행 후 폴링(왕복 절감)."""
        submit = self._client.batch_execute_statement(
            **self._base_kwargs(database), Sqls=sqls
        )
        return self._wait_until_done(submit["Id"])

    def query(self, sql: str, *, database: str | None = None) -> dict:
        """SELECT 실행 후 get_statement_result 결과(ColumnMetadata + Records) 반환."""
        submit = self._client.execute_statement(
            **self._base_kwargs(database), Sql=sql
        )
        statement_id = submit["Id"]
        self._wait_until_done(statement_id)
        return self._client.get_statement_result(Id=statement_id)

    def _wait_until_done(self, statement_id: str) -> dict:
        """describe_statement 를 종료 상태까지 폴링. FAILED/ABORTED·타임아웃은 예외."""
        elapsed = 0.0
        while True:
            description = self._client.describe_statement(Id=statement_id)
            status = description.get("Status")
            if status == FINISHED:
                return description
            if status in _TERMINAL_FAILURES:
                error = description.get("Error") or f"statement 가 {status} 상태로 종료됨."
                raise RedshiftDataApiError(
                    f"Redshift statement 실패({status}): {error}"
                )
            if elapsed >= self._timeout:
                raise RedshiftDataApiError(
                    f"Redshift statement 타임아웃({self._timeout:.0f}s 초과, id={statement_id})."
                )
            self._sleep(self._poll_interval)
            elapsed += self._poll_interval
