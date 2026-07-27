"""Aurora PostgreSQL 샘플 데이터 적재 스크립트 (RDS Data API).

멱등적(재실행 안전)으로 다음을 수행한다.
1. 데이터베이스 `ecommerce` 생성 (존재 시 skip)
2. 스키마 DDL 적용 (CREATE TABLE IF NOT EXISTS + COMMENT + 인덱스)
3. 결정적 샘플 데이터 배치 적재 (BatchExecuteStatement, 파라미터화)
4. read-only 사용자 `agent_ro` 생성 + SELECT-only grant
   - 비밀번호는 Secrets Manager 시크릿 `agentic-t2sql/aurora/agent-ro` 에 저장(없으면 생성)

AWS 자격증명/리소스는 환경변수로 주입한다.
  AURORA_CLUSTER_ARN, AURORA_SECRET_ARN(admin), DB_NAME(기본 ecommerce), AWS_REGION

주의: 이 스크립트는 실제 적재를 수행한다. 실행은 배포 담당(Task #6)이 맡는다.
"""

from __future__ import annotations

import argparse
import os
import sys

import boto3
from botocore.exceptions import ClientError

from sample_data import generator, schema
from sample_data.dataapi import DataApiClient, row_to_parameters

DEFAULT_DB_NAME = "ecommerce"
AGENT_RO_USER = "agent_ro"
AGENT_RO_SECRET_NAME = "agentic-t2sql/aurora/agent-ro"

# 컬럼별 RDS Data API typeHint (문자열로 넘기고 서버가 캐스팅).
TABLE_HINTS: dict[str, dict[str, str]] = {
    "categories": {},
    "customers": {"created_at": "TIMESTAMP", "last_login_at": "TIMESTAMP"},
    "products": {"price": "DECIMAL", "created_at": "TIMESTAMP"},
    "orders": {"total_amount": "DECIMAL", "ordered_at": "TIMESTAMP"},
    "order_items": {"unit_price": "DECIMAL"},
}

BATCH_SIZE = 500


def build_insert_sql(table: schema.Table) -> str:
    """파라미터화된 INSERT ... ON CONFLICT DO NOTHING SQL 생성(멱등)."""
    cols = [c.name for c in table.columns]
    col_list = ", ".join(cols)
    placeholders = ", ".join(f":{c}" for c in cols)
    return (
        f"INSERT INTO {table.name} ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT (id) DO NOTHING"
    )


def _rows_for_table(dataset: generator.Dataset, table_name: str) -> list[dict]:
    return getattr(dataset, table_name)


def ensure_database(admin: DataApiClient, db_name: str) -> None:
    """DB 존재 확인 후 없으면 생성. postgres DB 컨텍스트에서 실행."""
    resp = admin.execute(
        "SELECT 1 FROM pg_database WHERE datname = :name",
        [{"name": "name", "value": {"stringValue": db_name}}],
        database="postgres",
    )
    if resp.get("records"):
        print(f"[db] '{db_name}' 이미 존재 - skip")
        return
    # CREATE DATABASE 는 파라미터/트랜잭션 불가.
    admin.execute(f'CREATE DATABASE "{db_name}"', database="postgres")
    print(f"[db] '{db_name}' 생성 완료")


def apply_schema(admin: DataApiClient) -> None:
    """DDL statement 를 순차 적용(멱등)."""
    for stmt in schema.iter_ddl_statements():
        admin.execute(stmt)
    print(f"[schema] {len(schema.iter_ddl_statements())}개 DDL statement 적용 완료")


def load_data(admin: DataApiClient, dataset: generator.Dataset) -> None:
    """FK 순서대로 배치 insert."""
    for table in schema.TABLES:
        rows = _rows_for_table(dataset, table.name)
        if not rows:
            continue
        sql = build_insert_sql(table)
        hints = TABLE_HINTS.get(table.name, {})
        tx = admin.begin_transaction()
        try:
            for start in range(0, len(rows), BATCH_SIZE):
                chunk = rows[start : start + BATCH_SIZE]
                parameter_sets = [row_to_parameters(r, hints) for r in chunk]
                admin.batch_execute(sql, parameter_sets, transaction_id=tx)
            admin.commit_transaction(tx)
        except ClientError:
            admin.rollback_transaction(tx)
            raise
        print(f"[data] {table.name}: {len(rows)}행 적재 완료")


def read_agent_ro_password(secrets_client, secret_arn: str) -> tuple[str, str]:
    """CDK 가 생성·관리하는 agent_ro 시크릿에서 (username, password) 를 읽는다.

    이 시크릿(AGENT_RO_SECRET_ARN)은 SQL MCP 가 Data API 인증에 사용하는 바로 그 시크릿이다
    (base-stack 의 `agentRoSecret`). seed 는 이 비밀번호로 DB 역할을 생성/동기화해야
    MCP 의 인증이 성공한다. 별도 시크릿을 새로 만들면 비밀번호가 어긋나 인증에 실패한다.
    """
    import json

    resp = secrets_client.get_secret_value(SecretId=secret_arn)
    data = json.loads(resp["SecretString"])
    return data.get("username", AGENT_RO_USER), data["password"]


def ensure_agent_ro_secret(
    secrets_client, region: str, cluster_arn: str, db_name: str, host: str | None
) -> str:
    """agent_ro 비밀번호 시크릿 확보. 없으면 생성, 있으면 재사용.

    반환: 사용할 비밀번호(plaintext, DB grant 용도로만 사용).
    """
    try:
        resp = secrets_client.get_secret_value(SecretId=AGENT_RO_SECRET_NAME)
        import json

        existing = json.loads(resp["SecretString"])
        print(f"[secret] '{AGENT_RO_SECRET_NAME}' 재사용")
        return existing["password"]
    except secrets_client.exceptions.ResourceNotFoundException:
        pass

    # 새 비밀번호 생성(Secrets Manager 서버측 생성 사용).
    pw_resp = secrets_client.get_random_password(
        PasswordLength=32,
        ExcludePunctuation=True,
        RequireEachIncludedType=True,
    )
    password = pw_resp["RandomPassword"]
    import json

    secret_value = {
        "username": AGENT_RO_USER,
        "password": password,
        "engine": "postgres",
        "dbname": db_name,
        "dbClusterIdentifier": cluster_arn,
    }
    if host:
        secret_value["host"] = host
    secrets_client.create_secret(
        Name=AGENT_RO_SECRET_NAME,
        Description="agentic-t2sql read-only DB user (agent_ro) credentials",
        SecretString=json.dumps(secret_value),
    )
    print(f"[secret] '{AGENT_RO_SECRET_NAME}' 생성 완료")
    return password


def create_readonly_user(admin: DataApiClient, password: str) -> None:
    """agent_ro 사용자 생성 + SELECT-only grant (멱등).

    - CREATE USER (없으면) / ALTER USER 로 비밀번호 동기화
    - CONNECT/USAGE + SELECT ON ALL TABLES + 기본 권한(ALTER DEFAULT PRIVILEGES)
    - 쓰기 권한은 부여하지 않음(read-only 강제, 4중 방어의 한 축)
    """
    escaped_pw = password.replace("'", "''")
    db = admin.database
    # DO 블록으로 사용자 존재 여부에 따라 CREATE/ALTER (멱등).
    admin.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{AGENT_RO_USER}') THEN
                CREATE ROLE {AGENT_RO_USER} LOGIN PASSWORD '{escaped_pw}';
            ELSE
                ALTER ROLE {AGENT_RO_USER} WITH LOGIN PASSWORD '{escaped_pw}';
            END IF;
        END
        $$;
        """
    )
    grants = [
        f'GRANT CONNECT ON DATABASE "{db}" TO {AGENT_RO_USER}',
        f"GRANT USAGE ON SCHEMA public TO {AGENT_RO_USER}",
        f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {AGENT_RO_USER}",
        # 향후 생성될 테이블에도 SELECT 자동 부여.
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT ON TABLES TO {AGENT_RO_USER}",
    ]
    for g in grants:
        admin.execute(g)
    print(f"[user] read-only 사용자 '{AGENT_RO_USER}' 생성/grant 완료")


def seed(
    *,
    cluster_arn: str,
    secret_arn: str,
    db_name: str,
    region: str,
    host: str | None = None,
    agent_ro_secret_arn: str | None = None,
    n_customers: int = generator.DEFAULT_CUSTOMERS,
    n_products: int = generator.DEFAULT_PRODUCTS,
    n_orders: int = generator.DEFAULT_ORDERS,
    rds_data_client=None,
    secrets_client=None,
) -> dict[str, int]:
    """전체 seed 파이프라인 실행. 행 수 dict 반환.

    agent_ro_secret_arn 이 주어지면 CDK 가 관리하는 그 시크릿의 비밀번호로 agent_ro
    DB 역할을 동기화한다(권장 경로 — SQL MCP 가 인증에 쓰는 바로 그 시크릿이라 인증이 성공).
    없으면(레거시/로컬) seed 가 별도 시크릿을 self-generate 한다.
    """
    rds_data_client = rds_data_client or boto3.client("rds-data", region_name=region)
    secrets_client = secrets_client or boto3.client(
        "secretsmanager", region_name=region
    )

    print("[gen] 결정적 샘플 데이터 생성 중...")
    dataset = generator.generate(
        n_customers=n_customers, n_products=n_products, n_orders=n_orders
    )
    counts = dataset.row_counts()
    print(f"[gen] 생성 완료: {counts}")

    admin_default = DataApiClient(rds_data_client, cluster_arn, secret_arn, db_name)
    ensure_database(admin_default, db_name)
    apply_schema(admin_default)
    load_data(admin_default, dataset)

    if agent_ro_secret_arn:
        # CDK 관리 시크릿(= SQL MCP 의 AURORA_SECRET_ARN)에서 비밀번호를 읽어 역할을 동기화.
        _, password = read_agent_ro_password(secrets_client, agent_ro_secret_arn)
        print(f"[secret] CDK 관리 agent_ro 시크릿 사용: {agent_ro_secret_arn}")
    else:
        password = ensure_agent_ro_secret(
            secrets_client, region, cluster_arn, db_name, host
        )
    create_readonly_user(admin_default, password)

    print("[done] seed 완료")
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Aurora PostgreSQL 샘플 데이터 적재")
    parser.add_argument(
        "--cluster-arn", default=os.environ.get("AURORA_CLUSTER_ARN")
    )
    parser.add_argument("--secret-arn", default=os.environ.get("AURORA_SECRET_ARN"))
    parser.add_argument(
        "--db-name", default=os.environ.get("DB_NAME", DEFAULT_DB_NAME)
    )
    parser.add_argument(
        "--region", default=os.environ.get("AWS_REGION", "us-west-2")
    )
    parser.add_argument("--host", default=os.environ.get("AURORA_HOST"))
    parser.add_argument(
        "--agent-ro-secret-arn",
        default=os.environ.get("AGENT_RO_SECRET_ARN"),
        help="CDK 가 관리하는 agent_ro 시크릿 ARN(= SQL MCP 의 AURORA_SECRET_ARN). "
        "주면 이 비밀번호로 DB 역할을 동기화한다.",
    )
    parser.add_argument("--customers", type=int, default=generator.DEFAULT_CUSTOMERS)
    parser.add_argument("--products", type=int, default=generator.DEFAULT_PRODUCTS)
    parser.add_argument("--orders", type=int, default=generator.DEFAULT_ORDERS)
    args = parser.parse_args()

    missing = [
        n
        for n, v in (
            ("AURORA_CLUSTER_ARN", args.cluster_arn),
            ("AURORA_SECRET_ARN", args.secret_arn),
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
            cluster_arn=args.cluster_arn,
            secret_arn=args.secret_arn,
            db_name=args.db_name,
            region=args.region,
            host=args.host,
            agent_ro_secret_arn=args.agent_ro_secret_arn,
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
