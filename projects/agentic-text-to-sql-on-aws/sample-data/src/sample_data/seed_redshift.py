"""Redshift Serverless 샘플 데이터 적재 스크립트 (Redshift Data API).

Aurora seed(`seed_aurora`)와 동일한 결정적 데이터셋을 Redshift Serverless workgroup 에
적재한다. Redshift 방언 차이를 처리한다.

- `CREATE INDEX` 미지원 → 인덱스 DDL 제외(테이블/코멘트 DDL 만 적용).
- `REFERENCES`(FK)는 NOT ENFORCED 로 허용 → 그대로 둔다.
- `ON CONFLICT` 미지원 → 멱등성은 "행 수 확인 후 일치하면 skip, 불일치면 TRUNCATE 후 재적재".
- 파라미터화 대신 multi-row `INSERT ... VALUES` 리터럴(값 이스케이프)로 배치 적재.
- `DO $$ ... $$` 익명 블록 미지원 → 사용자 존재 확인은 `pg_user` 조회 후 파이썬 분기.
- `CREATE DATABASE` 불필요 → namespace 의 dbName 이 이미 ecommerce(확인 후 skip).

인증: workgroup 이름만으로 Data API 를 호출하면(SecretArn 미지정) namespace 관리자
자격증명으로 실행된다(DDL/DML/역할 생성 권한). read-only 사용자(agent_ro)의 비밀번호는
`REDSHIFT_RO_SECRET_ARN` 시크릿에서 읽어 DB 사용자 생성/동기화 + SELECT-only grant 에 쓴다.

env: REDSHIFT_WORKGROUP, REDSHIFT_DB(기본 ecommerce), REDSHIFT_RO_SECRET_ARN,
     AWS_REGION(기본 us-west-2)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

from sample_data import generator, schema
from sample_data.redshift_dataapi import RedshiftDataApiClient

DEFAULT_DB_NAME = "ecommerce"
AGENT_RO_USER = "agent_ro"

# multi-row INSERT 한 문장에 담을 행 수.
INSERT_CHUNK = 500

_NUMERIC_PREFIXES = (
    "INTEGER",
    "INT",
    "BIGINT",
    "SMALLINT",
    "NUMERIC",
    "DECIMAL",
    "FLOAT",
    "REAL",
    "DOUBLE",
)


def sql_literal(value) -> str:
    """파이썬 값을 Redshift SQL 리터럴로 변환(값 이스케이프 유틸).

    None → NULL, bool → TRUE/FALSE, int/float/Decimal → 숫자 리터럴,
    datetime → '...'::timestamp, 그 외(str) → 작은따옴표 이스케이프 문자열.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    if isinstance(value, dt.datetime):
        return f"'{value.isoformat(sep=' ')}'::timestamp"
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def format_value(column: schema.Column, value) -> str:
    """컬럼 타입에 맞춰 값을 SQL 리터럴로 변환.

    generator 는 타임스탬프/숫자를 문자열로 내보내므로 컬럼 타입 기준으로
    TIMESTAMP 는 `'...'::timestamp` 로 캐스팅, 숫자 컬럼은 인용 없는 숫자 리터럴로 낸다.
    """
    if value is None:
        return "NULL"
    upper = column.type.upper()
    if upper.startswith("TIMESTAMP"):
        escaped = str(value).replace("'", "''")
        return f"'{escaped}'::timestamp"
    if upper.startswith(_NUMERIC_PREFIXES):
        # 이미 유효한 숫자(또는 숫자 문자열) — 인용 없이 그대로.
        return str(value)
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def build_insert_sql(table: schema.Table, rows: list[dict]) -> str:
    """multi-row INSERT ... VALUES 문 생성(리터럴). rows 는 500행 이하 청크로 전달."""
    cols = [c.name for c in table.columns]
    col_list = ", ".join(cols)
    tuples = []
    for row in rows:
        values = ", ".join(format_value(c, row.get(c.name)) for c in table.columns)
        tuples.append(f"({values})")
    return f"INSERT INTO {table.name} ({col_list}) VALUES " + ", ".join(tuples)


def iter_schema_ddl() -> list[str]:
    """인덱스를 제외한 CREATE TABLE + COMMENT DDL statement 목록(세미콜론 제거)."""
    statements: list[str] = []
    for table in schema.TABLES:
        statements.append(table.create_ddl().rstrip(";"))
        statements.extend(s.rstrip(";") for s in table.comment_ddl())
    return statements


def _rows_for_table(dataset: generator.Dataset, table_name: str) -> list[dict]:
    return getattr(dataset, table_name)


def _count_rows(client: RedshiftDataApiClient, table_name: str) -> int:
    """테이블 행 수 조회. 테이블이 없거나 조회 실패 시 -1 반환(재적재 유도)."""
    try:
        result = client.query(f"SELECT COUNT(*) FROM {table_name}")
    except Exception:  # noqa: BLE001 — 테이블 미존재 등은 재적재로 흡수
        return -1
    records = result.get("Records") or []
    if not records or not records[0]:
        return -1
    field = records[0][0]
    if "longValue" in field:
        return int(field["longValue"])
    if "stringValue" in field:
        return int(field["stringValue"])
    return -1


def ensure_database(client: RedshiftDataApiClient, db_name: str) -> None:
    """DB 존재 확인(정보성). namespace dbName 이 이미 있으므로 생성하지 않는다."""
    # workgroup 접속 자체가 db 컨텍스트를 요구하므로 여기 도달했으면 db 는 존재한다.
    # 명시적 확인만 남기고 CREATE DATABASE 는 하지 않는다(Redshift namespace 관리).
    print(f"[db] '{db_name}' 는 namespace 가 제공 - CREATE DATABASE skip")


def apply_schema(client: RedshiftDataApiClient) -> None:
    """스키마 DDL(인덱스 제외) + COMMENT 를 batch 로 적용(멱등: IF NOT EXISTS)."""
    statements = iter_schema_ddl()
    client.batch(statements)
    print(f"[schema] {len(statements)}개 DDL statement 적용 완료(인덱스 제외)")


def load_data(client: RedshiftDataApiClient, dataset: generator.Dataset) -> None:
    """행 수 확인 → 일치 시 skip, 불일치 시 TRUNCATE 후 multi-row INSERT 재적재."""
    for table in schema.TABLES:
        rows = _rows_for_table(dataset, table.name)
        expected = len(rows)
        current = _count_rows(client, table.name)
        if current == expected:
            print(f"[data] {table.name}: {current}행 이미 적재됨 - skip")
            continue
        if current > 0:
            print(f"[data] {table.name}: {current}행 → 기대 {expected}행 불일치, 재적재")
            client.execute(f"TRUNCATE TABLE {table.name}")
        if not rows:
            continue
        for start in range(0, len(rows), INSERT_CHUNK):
            chunk = rows[start : start + INSERT_CHUNK]
            client.execute(build_insert_sql(table, chunk))
        print(f"[data] {table.name}: {expected}행 적재 완료")


def read_agent_ro_password(secrets_client, secret_arn: str) -> tuple[str, str]:
    """agent_ro 시크릿에서 (username, password) 를 읽는다.

    이 시크릿(REDSHIFT_RO_SECRET_ARN)은 SQL MCP 가 Redshift Data API 인증에 쓰는 바로 그
    시크릿이다. seed 는 이 비밀번호로 DB 사용자를 만들어야 MCP 인증이 성공한다.
    """
    resp = secrets_client.get_secret_value(SecretId=secret_arn)
    data = json.loads(resp["SecretString"])
    return data.get("username", AGENT_RO_USER), data["password"]


def create_readonly_user(
    client: RedshiftDataApiClient, username: str, password: str
) -> None:
    """agent_ro 사용자 생성/비밀번호 동기화 + SELECT-only grant (멱등).

    Redshift 는 `DO $$` 익명 블록을 지원하지 않으므로 pg_user 조회 후 파이썬으로 분기한다.
    """
    escaped_pw = password.replace("'", "''")
    result = client.query(f"SELECT 1 FROM pg_user WHERE usename = '{username}'")
    exists = bool(result.get("Records"))
    if exists:
        client.execute(f"ALTER USER {username} PASSWORD '{escaped_pw}'")
        print(f"[user] '{username}' 존재 - 비밀번호 동기화(ALTER USER)")
    else:
        client.execute(f"CREATE USER {username} PASSWORD '{escaped_pw}'")
        print(f"[user] '{username}' 생성(CREATE USER)")

    grants = [
        f"GRANT USAGE ON SCHEMA public TO {username}",
        f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {username}",
        # 향후 생성 테이블에도 SELECT 자동 부여.
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {username}",
    ]
    client.batch(grants)
    print(f"[user] read-only 사용자 '{username}' SELECT-only grant 완료")


def resolve_admin_secret_arn(workgroup: str, region: str, serverless_client=None) -> str:
    """workgroup 의 namespace 를 조회해 관리자 시크릿 ARN 을 얻는다.

    manageAdminPassword=true 인 namespace 는 `redshift!<ns>-<admin>` 시크릿을 자동
    생성한다. Data API 를 이 시크릿으로 호출해야 CREATE USER 등 관리자 권한이 나온다
    (SecretArn 없이 호출하면 IAM 매핑 사용자라 권한 부족).
    """
    serverless_client = serverless_client or boto3.client(
        "redshift-serverless", region_name=region
    )
    wg = serverless_client.get_workgroup(workgroupName=workgroup)["workgroup"]
    ns = serverless_client.get_namespace(namespaceName=wg["namespaceName"])["namespace"]
    arn = ns.get("adminPasswordSecretArn")
    if not arn:
        raise RuntimeError(
            f"namespace '{wg['namespaceName']}' 에 adminPasswordSecretArn 이 없다 "
            "(manageAdminPassword 미사용?)."
        )
    return arn


def seed(
    *,
    workgroup: str,
    db_name: str,
    region: str,
    ro_secret_arn: str,
    admin_secret_arn: str | None = None,
    n_customers: int = generator.DEFAULT_CUSTOMERS,
    n_products: int = generator.DEFAULT_PRODUCTS,
    n_orders: int = generator.DEFAULT_ORDERS,
    redshift_data_client=None,
    secrets_client=None,
    serverless_client=None,
) -> dict[str, int]:
    """전체 Redshift seed 파이프라인 실행. 행 수 dict 반환."""
    redshift_data_client = redshift_data_client or boto3.client(
        "redshift-data", region_name=region
    )
    secrets_client = secrets_client or boto3.client(
        "secretsmanager", region_name=region
    )
    if admin_secret_arn is None:
        admin_secret_arn = resolve_admin_secret_arn(workgroup, region, serverless_client)
        print(f"[secret] namespace 관리자 시크릿 사용: {admin_secret_arn}")

    print("[gen] 결정적 샘플 데이터 생성 중...")
    dataset = generator.generate(
        n_customers=n_customers, n_products=n_products, n_orders=n_orders
    )
    counts = dataset.row_counts()
    print(f"[gen] 생성 완료: {counts}")

    client = RedshiftDataApiClient(
        redshift_data_client, workgroup, db_name, secret_arn=admin_secret_arn
    )
    ensure_database(client, db_name)
    apply_schema(client)
    load_data(client, dataset)

    username, password = read_agent_ro_password(secrets_client, ro_secret_arn)
    print(f"[secret] agent_ro 시크릿 사용: {ro_secret_arn}")
    create_readonly_user(client, username, password)

    print("[done] Redshift seed 완료")
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Redshift Serverless 샘플 데이터 적재")
    parser.add_argument("--workgroup", default=os.environ.get("REDSHIFT_WORKGROUP"))
    parser.add_argument(
        "--db-name", default=os.environ.get("REDSHIFT_DB", DEFAULT_DB_NAME)
    )
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument(
        "--ro-secret-arn", default=os.environ.get("REDSHIFT_RO_SECRET_ARN")
    )
    parser.add_argument("--customers", type=int, default=generator.DEFAULT_CUSTOMERS)
    parser.add_argument("--products", type=int, default=generator.DEFAULT_PRODUCTS)
    parser.add_argument("--orders", type=int, default=generator.DEFAULT_ORDERS)
    args = parser.parse_args()

    missing = [
        n
        for n, v in (
            ("REDSHIFT_WORKGROUP", args.workgroup),
            ("REDSHIFT_RO_SECRET_ARN", args.ro_secret_arn),
        )
        if not v
    ]
    if missing:
        print(
            f"필수 값 누락: {', '.join(missing)} (환경변수 또는 인자로 제공)",
            file=sys.stderr,
        )
        return 2

    try:
        seed(
            workgroup=args.workgroup,
            db_name=args.db_name,
            region=args.region,
            ro_secret_arn=args.ro_secret_arn,
            n_customers=args.customers,
            n_products=args.products,
            n_orders=args.orders,
        )
        return 0
    except ClientError as e:
        print(f"AWS 오류: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
