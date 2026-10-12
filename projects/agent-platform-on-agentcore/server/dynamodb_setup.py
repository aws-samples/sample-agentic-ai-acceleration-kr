"""
Script to create DynamoDB tables for LangGraph API
Run this once to set up the required tables
"""
import os
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
import boto3
from botocore.exceptions import ClientError

# Load environment variables from .env file
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)


def create_threads_table(region_name: Optional[str] = None):
    """Create threads table"""
    region_name = region_name or os.getenv("AWS_REGION", "ap-northeast-1")
    dynamodb = boto3.resource("dynamodb", region_name=region_name)
    table_name = os.getenv("DYNAMODB_THREADS_TABLE", "langgraph-threads")

    try:
        table = dynamodb.create_table(
            TableName=table_name,
            KeySchema=[
                {"AttributeName": "thread_id", "KeyType": "HASH"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "thread_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",  # On-demand pricing
        )
        print(f"Creating table {table_name}...")
        table.wait_until_exists()
        print(f"Table {table_name} created successfully!")
        return table
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceInUseException":
            print(f"Table {table_name} already exists")
        else:
            print(f"Error creating table: {e}")
            raise


def create_artifacts_table(region_name: Optional[str] = None):
    """Create artifacts table (one item per artifact version)"""
    region_name = region_name or os.getenv("AWS_REGION", "ap-northeast-1")
    dynamodb = boto3.resource("dynamodb", region_name=region_name)
    table_name = os.getenv("ARTIFACTS_TABLE", "agent-artifacts")

    try:
        table = dynamodb.create_table(
            TableName=table_name,
            KeySchema=[
                {"AttributeName": "artifact_id", "KeyType": "HASH"},
                {"AttributeName": "version", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "artifact_id", "AttributeType": "S"},
                {"AttributeName": "version", "AttributeType": "N"},
                {"AttributeName": "thread_id", "AttributeType": "S"},
                {"AttributeName": "created_at", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "thread_id-created_at-index",
                    "KeySchema": [
                        {"AttributeName": "thread_id", "KeyType": "HASH"},
                        {"AttributeName": "created_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        print(f"Creating table {table_name}...")
        table.wait_until_exists()
        print(f"Table {table_name} created successfully!")
        return table
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceInUseException":
            print(f"Table {table_name} already exists")
        else:
            print(f"Error creating table: {e}")
            raise


def create_usage_table(region_name: Optional[str] = None):
    """Create usage rollup table (one item per pk/sk counter)"""
    region_name = region_name or os.getenv("AWS_REGION", "ap-northeast-1")
    dynamodb = boto3.resource("dynamodb", region_name=region_name)
    table_name = os.getenv("USAGE_TABLE", "agent-usage")

    try:
        table = dynamodb.create_table(
            TableName=table_name,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        print(f"Creating table {table_name}...")
        table.wait_until_exists()
        print(f"Table {table_name} created successfully!")
        return table
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceInUseException":
            print(f"Table {table_name} already exists")
        else:
            print(f"Error creating table: {e}")
            raise


if __name__ == "__main__":
    import sys
    from typing import Optional

    region = sys.argv[1] if len(sys.argv) > 1 else None
    if region:
        print(f"Creating tables in region: {region}")
    else:
        region = os.getenv("AWS_REGION", "ap-northeast-1")
        print(f"Creating tables in region: {region} (from .env or default)")

    create_threads_table(region)
    create_artifacts_table(region)
    create_usage_table(region)

    print("\n✅ All tables created successfully!")
    print("\nNext steps:")
    print("1. Set up AWS credentials (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY)")
    print("2. Set environment variables:")
    print("   - DYNAMODB_THREADS_TABLE=bap-threads")
    print("   - BEDROCK_MODEL_ID=global.anthropic.claude-sonnet-5-5")
    print("   - AWS_REGION=ap-northeast-1")
    print("3. Run the FastAPI server: uvicorn main:app --reload")

