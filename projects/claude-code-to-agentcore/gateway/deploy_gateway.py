"""
deploy_gateway.py — 로컬 stdio MCP 서버를 AgentCore Gateway로 승격.

Part 1에서 Claude Code에 붙이던 stdio MCP 서버(`ecommerce`)는 내 노트북에서만
돕니다. 프로덕션에서는 여러 사용자/에이전트가 동시에, 인증을 거쳐 호출해야 하므로
같은 도구를 **AgentCore Gateway(관리형 원격 MCP 서버)** 뒤의 Lambda로 옮깁니다.

전환 매핑:
    로컬                                  →  AgentCore
    ─────────────────────────────────────────────────────────────
    ecommerce_mcp.py (FastMCP, stdio)     →  Lambda 함수 (handler.py)
    @mcp.tool() 데코레이터 + docstring     →  Gateway 타깃의 inline toolSchema
    .mcp.json 의 stdio 등록                →  Gateway MCP 엔드포인트(HTTPS) + JWT

이 스크립트가 하는 일:
    1. Lambda 실행 역할 + Lambda 함수(ecommerce.db 동봉) 생성/갱신
    2. GatewayClient 로 MCP Gateway 생성 (Cognito JWT 인바운드)
    3. 위 Lambda 를 Gateway 타깃으로 등록 (도구 3개 스키마 inline)
    4. Gateway 서비스가 Lambda 를 호출할 수 있도록 리소스 정책 부여
    5. gateway.json 에 엔드포인트/clientInfo 캐시

    python deploy_gateway.py            # 생성 (같은 이름의 Gateway가 있으면 재사용)
"""
import io
import json
import os
import time
import zipfile

import boto3
from bedrock_agentcore_starter_toolkit.operations.gateway import GatewayClient

REGION = "us-east-1"
GATEWAY_NAME = "anycompany-ecommerce-gw"
LAMBDA_NAME = "anycompany-ecommerce-tools"
ROLE_NAME = "anycompany-ecommerce-lambda-role"
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "lambda_src")
DB_SRC = os.path.join(HERE, "..", "local_agent", "data", "ecommerce.db")

# 로컬 MCP 도구의 docstring/시그니처를 그대로 옮긴 Gateway 도구 스키마.
TOOL_SCHEMA = [
    {
        "name": "query_sales",
        "description": "기간/카테고리/지역으로 필터링한 매출·주문 집계를 반환합니다. 날짜는 YYYY-MM-DD.",
        "inputSchema": {"type": "object", "properties": {
            "start_date": {"type": "string"}, "end_date": {"type": "string"},
            "category": {"type": "string"}, "region": {"type": "string"}}},
    },
    {
        "name": "top_products",
        "description": "기간 내 매출 상위 상품 N개를 반환합니다.",
        "inputSchema": {"type": "object", "properties": {
            "limit": {"type": "integer"}, "start_date": {"type": "string"},
            "end_date": {"type": "string"}}},
    },
    {
        "name": "run_sql",
        "description": "임의의 SELECT 쿼리를 실행합니다(read-only). 테이블: orders, products.",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}},
                        "required": ["query"]},
    },
]


def _ensure_lambda_role(iam, account):
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
        "Action": "sts:AssumeRole"}]}
    try:
        role = iam.get_role(RoleName=ROLE_NAME)["Role"]
        print(f"  role 재사용: {ROLE_NAME}")
    except iam.exceptions.NoSuchEntityException:
        role = iam.create_role(RoleName=ROLE_NAME,
                               AssumeRolePolicyDocument=json.dumps(trust))["Role"]
        iam.attach_role_policy(RoleName=ROLE_NAME,
            PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
        print(f"  role 생성: {ROLE_NAME}  (전파 대기 10s)")
        time.sleep(10)
    return role["Arn"]


def _zip_lambda():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(os.path.join(SRC, "handler.py"), "handler.py")
        z.write(os.path.abspath(DB_SRC), "ecommerce.db")   # DB 동봉
    return buf.getvalue()


def _ensure_lambda(lam, role_arn):
    code = _zip_lambda()
    try:
        lam.get_function(FunctionName=LAMBDA_NAME)
        lam.update_function_code(FunctionName=LAMBDA_NAME, ZipFile=code)
        print(f"  lambda 코드 갱신: {LAMBDA_NAME}")
    except lam.exceptions.ResourceNotFoundException:
        lam.create_function(
            FunctionName=LAMBDA_NAME, Runtime="python3.13", Role=role_arn,
            Handler="handler.handler", Code={"ZipFile": code}, Timeout=30, MemorySize=256)
        print(f"  lambda 생성: {LAMBDA_NAME}")
        time.sleep(5)
    return lam.get_function(FunctionName=LAMBDA_NAME)["Configuration"]["FunctionArn"]


def _grant_gateway_invoke(lam, account, gateway_id):
    """Gateway 서비스 principal이 이 Lambda를 호출할 수 있도록 리소스 정책 부여.

    SourceAccount와 SourceArn 조건으로 이 계정의 이 Gateway만 호출을 허용한다."""
    gateway_arn = f"arn:aws:bedrock-agentcore:{REGION}:{account}:gateway/{gateway_id}"
    try:
        lam.remove_permission(FunctionName=LAMBDA_NAME, StatementId="agentcore-gateway-invoke")
    except lam.exceptions.ResourceNotFoundException:
        pass
    lam.add_permission(
        FunctionName=LAMBDA_NAME, StatementId="agentcore-gateway-invoke",
        Action="lambda:InvokeFunction", Principal="bedrock-agentcore.amazonaws.com",
        SourceAccount=account, SourceArn=gateway_arn)
    print(f"  lambda 호출 권한 부여: {gateway_arn}")


def _find_existing_gateway(account):
    """같은 이름의 Gateway가 이미 있고 gateway.json 캐시가 유효하면 재사용."""
    cache_path = os.path.join(HERE, "gateway.json")
    if not os.path.exists(cache_path):
        return None
    with open(cache_path) as f:
        info = json.load(f)
    ctl = boto3.client("bedrock-agentcore-control", region_name=REGION)
    for item in ctl.list_gateways().get("items", []):
        if item["name"] == GATEWAY_NAME and item["gatewayId"] == info.get("gateway_id"):
            return info
    return None


def main():
    sts = boto3.client("sts", region_name=REGION)
    account = sts.get_caller_identity()["Account"]
    iam = boto3.client("iam")
    lam = boto3.client("lambda", region_name=REGION)

    print("1) Lambda 실행 역할")
    role_arn = _ensure_lambda_role(iam, account)
    print("2) Lambda 함수 (도구 3개 + ecommerce.db)")
    lambda_arn = _ensure_lambda(lam, role_arn)

    existing = _find_existing_gateway(account)
    if existing:
        print(f"3) 기존 Gateway 재사용: {existing['gateway_id']}")
        _grant_gateway_invoke(lam, account, existing["gateway_id"])
        print(f"\nGateway 준비 완료 (Lambda 코드만 갱신)")
        print(f"   URL  : {existing['gateway_url']}")
        return

    print("3) MCP Gateway (Cognito JWT 인바운드)")
    gw = GatewayClient(region_name=REGION)
    cognito = gw.create_oauth_authorizer_with_cognito(GATEWAY_NAME)
    gateway = gw.create_mcp_gateway(
        name=GATEWAY_NAME,
        authorizer_config=cognito["authorizer_config"],
        enable_semantic_search=True,
    )
    _grant_gateway_invoke(lam, account, gateway["gatewayId"])

    print("4) Lambda 를 Gateway 타깃으로 등록 (도구 스키마 inline)")
    gw.create_mcp_gateway_target(
        gateway=gateway,
        name="ecommerce",
        target_type="lambda",
        target_payload={
            "lambdaArn": lambda_arn,
            "toolSchema": {"inlinePayload": TOOL_SCHEMA},
        },
    )

    info = {
        "gateway_id": gateway["gatewayId"],
        "gateway_url": gateway["gatewayUrl"],
        "lambda_arn": lambda_arn,
        "client_info": cognito["client_info"],
    }
    with open(os.path.join(HERE, "gateway.json"), "w") as f:
        json.dump(info, f, indent=2)
    os.chmod(os.path.join(HERE, "gateway.json"), 0o600)
    print(f"\nGateway 준비 완료")
    print(f"   URL  : {gateway['gatewayUrl']}")
    print(f"   도구 : ecommerce___query_sales, ecommerce___top_products, ecommerce___run_sql")
    print(f"   캐시 : gateway.json (Cognito client_secret 포함 - 커밋 금지)")


if __name__ == "__main__":
    main()
