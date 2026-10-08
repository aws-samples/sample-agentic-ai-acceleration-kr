"""
Base DynamoDB repository
"""
import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from core.config import AWS_REGION

# Routes hand blocking DynamoDB calls to FastAPI's threadpool (~40 workers), so
# botocore's default pool of 10 connections would become the new bottleneck and
# re-serialise the very requests the threadpool is meant to run concurrently.
BOTO_CONFIG = BotoConfig(max_pool_connections=50)


class DynamoDBRepository:
    """Base DynamoDB repository class"""

    # Defaults to the configured region, not to a literal. Every production
    # caller already passes region_name=AWS_REGION, so a hardcoded default only
    # ever fires for a caller that forgot — and then it silently reads a
    # different region's tables, where _ensure_table_exists finds nothing and
    # raises "table does not exist" for a table that does.
    def __init__(self, table_name: str, region_name: str = AWS_REGION):
        self.table_name = table_name
        self.dynamodb = boto3.resource(
            "dynamodb", region_name=region_name, config=BOTO_CONFIG
        )
        self.table = self.dynamodb.Table(table_name)
        self._ensure_table_exists()

    def _ensure_table_exists(self):
        """Ensure the table exists, create if it doesn't"""
        try:
            self.table.load()
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                self._create_table()
            else:
                raise

    def _create_table(self):
        """Create the DynamoDB table"""
        # This should be done via CloudFormation/CDK in production
        # For now, we'll just raise an error with instructions
        raise RuntimeError(
            f"Table {self.table_name} does not exist. "
            "Please create it using AWS CLI or CloudFormation. "
            "See server/dynamodb_setup.py for setup script."
        )










