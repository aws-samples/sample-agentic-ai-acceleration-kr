import os
import boto3
import json
from dotenv import load_dotenv
from boto3.session import Session

boto_session = Session()
region = boto_session.region_name

load_dotenv()

def create_agentcore_client():
    # ap-northeast-1 is the platform default (config.py, deploy.sh, infra/envs/standalone).
    # us-west-2 here meant this script probed a different region than the one the
    # runtime was deployed to, so a healthy deployment answered with
    # ResourceNotFoundException.
    region_name = os.getenv("AWS_REGION", "ap-northeast-1")
    # Create boto session
    boto_session = boto3.Session(region_name=region_name)
    
    # Create bedrock-agentcore client directly using boto3
    agentcore_client = boto_session.client(
        'bedrock-agentcore',
        region_name=region_name
    )
    
    return boto_session, agentcore_client


if __name__ == "__main__":
    # Create AgentCore client
    try:
        (boto_session, agentcore_client) = create_agentcore_client()
        
        # Client for data plane
        boto3_response = agentcore_client.invoke_agent_runtime(
            agentRuntimeArn=os.getenv("AGENT_RUNTIME_ARN"),
            qualifier="DEFAULT",
            payload=json.dumps({"prompt": "Hello world"})
        )

        for event in boto3_response.get("response", []):
            print(event)

        print(boto3_response)
    except Exception as e:
        print(f"❌ Error connecting to AgentCore: {e}")
        exit(1)