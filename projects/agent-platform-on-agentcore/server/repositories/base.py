"""
Base DynamoDB repository
"""
import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from core.config import AWS_REGION

# Routes hand blocking DynamoDB calls to the blocking threadpool (BLOCKING_THREADS,
# 64 by default; see main.lifespan), so
# botocore's default pool of 10 connections would become the new bottleneck and
# re-serialise the very requests the threadpool is meant to run concurrently.
BOTO_CONFIG = BotoConfig(max_pool_connections=64)


class DynamoDBRepository:
    """Base DynamoDB repository class.

    Constructing one does no I/O. The table used to be probed (`DescribeTable`)
    in `__init__`, and every repository is built when `core.dependencies` is
    imported — so importing a route made seven AWS calls. A test, a script or an
    image smoke check could not import the app without credentials and live
    tables, and the unit tests grew a module-level mock to get past collection.
    The check now runs once, from the server's startup (`verify`), where failing
    still stops a misconfigured task from coming up.
    """

    # Defaults to the configured region, not to a literal. Every production
    # caller already passes region_name=AWS_REGION, so a hardcoded default only
    # ever fires for a caller that forgot — and then it silently reads a
    # different region's tables, where `verify` finds nothing and reports
    # "table does not exist" for a table that does.
    def __init__(self, table_name: str, region_name: str = AWS_REGION):
        self.table_name = table_name
        self.region_name = region_name
        self.dynamodb = boto3.resource(
            "dynamodb", region_name=region_name, config=BOTO_CONFIG
        )
        self.table = self.dynamodb.Table(table_name)

    def verify(self) -> None:
        """Fail if the table is missing or this role cannot describe it.

        The ALB health check reads `/` and nothing else, so this is what keeps a
        deployment with a wrong table name or a missing IAM grant from going
        healthy and answering every request with a 500. The error names the
        table, the region and which of the two it was.
        """
        try:
            self.table.load()
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "ResourceNotFoundException":
                raise RuntimeError(
                    f"DynamoDB table {self.table_name} does not exist in {self.region_name}. "
                    "Tables are created by the Terraform stack (infra/), not by this server."
                ) from e
            if code in ("AccessDeniedException", "UnrecognizedClientException"):
                raise RuntimeError(
                    f"Cannot describe DynamoDB table {self.table_name} in {self.region_name} ({code}). "
                    "The task role needs dynamodb:DescribeTable on it."
                ) from e
            raise
