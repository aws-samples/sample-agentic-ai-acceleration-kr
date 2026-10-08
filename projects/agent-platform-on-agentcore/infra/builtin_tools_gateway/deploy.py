#!/usr/bin/env python3
"""
Deploy an AgentCore MCP Gateway carrying every built-in tool AgentCore offers:
web search, code interpreter and browser.

Why a script and not Terraform: only Web Search is a real gateway target
(`connectorId: "web-search"`). Code Interpreter and Browser have no connector and
no gateway target type — they are data-plane primitives — so they are bridged
through a Lambda MCP target built from ./lambda and ./tools.json. On top of that,
the awscc provider still has no `awscc_bedrockagentcore_gateway_target` resource,
which is why ../modules/mcp_gateway already drives its target through boto3. This
script keeps the whole stack in one reconcilable place.

Code interpreter and browser sessions are per caller, not shared. A gateway hands a
Lambda target no caller identity and drops custom headers, so a REQUEST interceptor
(./interceptor) stamps the verified caller onto every tool call and the bridge keys
its sessions off that. Pass --shared-sessions to opt out for a single trusted tenant.

Everything is idempotent: re-running converges instead of duplicating.

    ./deploy.py up                       # create/update everything
    ./deploy.py up --no-web-search       # skip the web search connector
    ./deploy.py status                   # show gateway, targets and tool list
    ./deploy.py test                     # exercise the tools through MCP
    ./deploy.py down                     # delete everything it created

Inbound auth defaults to AWS_IAM (what a harness uses). Pass --cognito-user-pool-id
and --cognito-client-id for CUSTOM_JWT instead, which is what this repo's agent
runtime speaks (see agent-runtime/auth/access_token.py).
"""
import argparse
import io
import json
import os
import sys
import time
import zipfile

import boto3
from botocore.exceptions import ClientError

HERE = os.path.dirname(os.path.abspath(__file__))
LAMBDA_DIR = os.path.join(HERE, "lambda")
INTERCEPTOR_DIR = os.path.join(HERE, "interceptor")
TOOL_SCHEMA = os.path.join(HERE, "tools.json")

CONTROL = "bedrock-agentcore-control"

# Web Search is offered in fewer regions than the other primitives.
WEB_SEARCH_REGIONS = ("us-east-1", "eu-west-1", "ap-northeast-1")
WEB_SEARCH_CONNECTOR_ID = "web-search"

CODE_INTERPRETER_ID = "aws.codeinterpreter.v1"
BROWSER_ID = "aws.browser.v1"

BRIDGE_TARGET_NAME = "builtin-tools"
WEB_SEARCH_TARGET_NAME = "web-search"

READY_TIMEOUT_SECONDS = 300
POLL_INTERVAL_SECONDS = 5

# Freshly attached role policies are eventually consistent; CreateGatewayTarget
# and Lambda's role validation both reject a policy they cannot see yet.
PROPAGATION_ATTEMPTS = 12
PROPAGATION_INTERVAL_SECONDS = 10

# Substrings AWS uses when the failure is really "the IAM change has not landed yet".
# A brand new gateway role produces the AssumeRole variant on the first attempt even
# though the trust policy is already correct, so all of these must be retried.
PROPAGATION_ERRORS = (
    "lacks permission",
    "not authorized to perform AssumeRole",
    "cannot be assumed",
    "Update trust policy and retry",
)


def _is_propagation_error(exc):
    message = str(exc)
    return any(needle in message for needle in PROPAGATION_ERRORS)


def log(message):
    print(f"  {message}", file=sys.stderr)


def step(message):
    print(f"\n=> {message}", file=sys.stderr)


# --- naming ------------------------------------------------------------------


class Names:
    def __init__(self, project, region, account):
        self.project = project
        self.region = region
        self.account = account
        # Gateway names allow only alphanumerics and dashes, max 48 chars.
        self.gateway = f"{project}-builtin-tools"[:48].strip("-")
        self.gateway_role = f"{project}-builtin-gw-role"
        self.lambda_name = f"{project}-builtin-tools"
        self.lambda_role = f"{project}-builtin-tools-lambda"
        self.interceptor_name = f"{project}-builtin-tools-interceptor"
        self.interceptor_role = f"{project}-builtin-tools-icept"
        self.bucket = f"{project}-builtin-tools-{account}"


# --- IAM ---------------------------------------------------------------------


def _put_role(iam, name, trust, description):
    try:
        iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description=description,
        )
        log(f"created role {name}")
    except iam.exceptions.EntityAlreadyExistsException:
        iam.update_assume_role_policy(
            RoleName=name, PolicyDocument=json.dumps(trust)
        )
        log(f"role {name} exists")
    return iam.get_role(RoleName=name)["Role"]["Arn"]


def _put_role_policy(iam, role, policy_name, statements):
    iam.put_role_policy(
        RoleName=role,
        PolicyName=policy_name,
        PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": statements}),
    )
    log(f"attached policy {policy_name} to {role}")


def ensure_lambda_role(iam, names, bucket):
    """Role the tool Lambda runs as: it drives the built-in tool data-plane APIs."""
    arn = _put_role(
        iam,
        names.lambda_role,
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "lambda.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        },
        "Runs the AgentCore built-in tool bridge Lambda",
    )
    iam.attach_role_policy(
        RoleName=names.lambda_role,
        PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    )

    statements = [
        {
            "Sid": "CodeInterpreter",
            "Effect": "Allow",
            "Action": [
                "bedrock-agentcore:StartCodeInterpreterSession",
                "bedrock-agentcore:StopCodeInterpreterSession",
                "bedrock-agentcore:GetCodeInterpreterSession",
                "bedrock-agentcore:ListCodeInterpreterSessions",
                "bedrock-agentcore:InvokeCodeInterpreter",
            ],
            # System primitives live under the service's own account, not yours.
            "Resource": [
                f"arn:aws:bedrock-agentcore:{names.region}:aws:code-interpreter/*",
                f"arn:aws:bedrock-agentcore:{names.region}:{names.account}:code-interpreter/*",
            ],
        },
        {
            "Sid": "Browser",
            "Effect": "Allow",
            "Action": [
                "bedrock-agentcore:StartBrowserSession",
                "bedrock-agentcore:StopBrowserSession",
                "bedrock-agentcore:GetBrowserSession",
                "bedrock-agentcore:ListBrowserSessions",
                "bedrock-agentcore:InvokeBrowser",
                "bedrock-agentcore:UpdateBrowserStream",
            ],
            "Resource": [
                f"arn:aws:bedrock-agentcore:{names.region}:aws:browser/*",
                f"arn:aws:bedrock-agentcore:{names.region}:{names.account}:browser/*",
            ],
        },
        {
            "Sid": "Screenshots",
            "Effect": "Allow",
            "Action": ["s3:PutObject", "s3:GetObject"],
            "Resource": f"arn:aws:s3:::{bucket}/*",
        },
    ]
    _put_role_policy(iam, names.lambda_role, f"{names.lambda_role}-policy", statements)
    return arn


def ensure_interceptor_role(iam, names):
    """Role for the identity interceptor: it only reads the request, so logs suffice."""
    arn = _put_role(
        iam,
        names.interceptor_role,
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "lambda.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        },
        "Runs the gateway REQUEST interceptor that stamps caller identity",
    )
    iam.attach_role_policy(
        RoleName=names.interceptor_role,
        PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    )
    return arn


def ensure_gateway_role(iam, names, lambda_arn, with_web_search, interceptor_arn=None):
    """Role the gateway assumes to reach its targets on the agent's behalf."""
    arn = _put_role(
        iam,
        names.gateway_role,
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                    "Condition": {
                        "StringEquals": {"aws:SourceAccount": names.account},
                        "ArnLike": {
                            "aws:SourceArn": f"arn:aws:bedrock-agentcore:{names.region}:{names.account}:gateway/*"
                        },
                    },
                }
            ],
        },
        "Assumed by the AgentCore built-in tools gateway",
    )

    statements = [
        {
            "Sid": "InvokeToolLambda",
            "Effect": "Allow",
            "Action": ["lambda:InvokeFunction"],
            # The interceptor runs under the same gateway role as the tool target.
            "Resource": [arn for arn in (lambda_arn, interceptor_arn) if arn],
        }
    ]
    if with_web_search:
        statements += [
            {
                "Sid": "InvokeGateway",
                "Effect": "Allow",
                "Action": "bedrock-agentcore:InvokeGateway",
                "Resource": f"arn:aws:bedrock-agentcore:{names.region}:{names.account}:gateway/*",
            },
            {
                "Sid": "InvokeWebSearch",
                "Effect": "Allow",
                "Action": "bedrock-agentcore:InvokeWebSearch",
                # Service-owned tool ARN, checked per request.
                "Resource": f"arn:aws:bedrock-agentcore:{names.region}:aws:tool/web-search.v1",
            },
        ]
    _put_role_policy(iam, names.gateway_role, f"{names.gateway_role}-policy", statements)
    return arn


# --- screenshot bucket -------------------------------------------------------


def ensure_bucket(s3, names):
    """Browser screenshots are PNGs; MCP returns text, so they go to S3 instead."""
    bucket = names.bucket
    try:
        s3.head_bucket(Bucket=bucket)
        log(f"bucket {bucket} exists")
        return bucket
    except ClientError as exc:
        if exc.response["ResponseMetadata"]["HTTPStatusCode"] not in (403, 404):
            raise

    kwargs = {"Bucket": bucket}
    if names.region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": names.region}
    s3.create_bucket(**kwargs)
    s3.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    # Screenshots are debugging breadcrumbs, not records worth keeping.
    s3.put_bucket_lifecycle_configuration(
        Bucket=bucket,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "expire-screenshots",
                    "Status": "Enabled",
                    "Filter": {"Prefix": "screenshots/"},
                    "Expiration": {"Days": 7},
                }
            ]
        },
    )
    log(f"created bucket {bucket}")
    return bucket


# --- Lambda ------------------------------------------------------------------


def _zip_dir(directory):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for entry in sorted(os.listdir(directory)):
            if entry.endswith(".py"):
                archive.write(os.path.join(directory, entry), entry)
    return buffer.getvalue()


def _zip_lambda():
    return _zip_dir(LAMBDA_DIR)


def _has_stale_kms_grant(lam, iam, names):
    """Detect a function whose env-var KMS grant points at a deleted role.

    Lambda encrypts environment variables under the aws/lambda key and creates the
    decrypt grant for the execution role's *unique id* when the function is created.
    Deleting and recreating the role reuses the name but not the id, so the grant is
    now orphaned and every invocation fails with KMSAccessDeniedException — which
    reaches the agent as a bare "An internal error occurred". Updating the function
    does not refresh the grant, so the function has to be replaced.
    """
    try:
        role_id = iam.get_role(RoleName=names.lambda_role)["Role"]["RoleId"]
    except iam.exceptions.NoSuchEntityException:
        return False

    try:
        response = lam.invoke(
            FunctionName=names.lambda_name,
            Payload=json.dumps({"__probe__": True}).encode(),
        )
    except ClientError as exc:
        return exc.response.get("Error", {}).get("Code") == "KMSAccessDeniedException"

    error = response.get("FunctionError")
    if not error:
        return False
    # A probe with no tool name returns an "Unknown tool" payload, not a KMS failure.
    body = response["Payload"].read().decode("utf-8", errors="replace")
    return "KMSAccessDenied" in body and role_id not in body


def ensure_lambda(lam, names, role_arn, bucket, shared_sessions=False):
    payload = _zip_lambda()
    env = {
        "Variables": {
            "CODE_INTERPRETER_ID": CODE_INTERPRETER_ID,
            "BROWSER_ID": BROWSER_ID,
            "SESSION_PREFIX": names.project[:20],
            "SCREENSHOT_BUCKET": bucket,
            # The bridge refuses unattributed calls unless sharing is deliberate.
            "REQUIRE_TRUSTED_SESSION": "false" if shared_sessions else "true",
        }
    }

    try:
        lam.get_function(FunctionName=names.lambda_name)
        if _has_stale_kms_grant(lam, boto3.client("iam"), names):
            log("stale KMS grant from a recreated role; replacing the function")
            lam.delete_function(FunctionName=names.lambda_name)
            raise lam.exceptions.ResourceNotFoundException(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "recreating"}},
                "GetFunction",
            )
        lam.update_function_code(FunctionName=names.lambda_name, ZipFile=payload)
        _wait_lambda_updated(lam, names.lambda_name)
        lam.update_function_configuration(
            FunctionName=names.lambda_name,
            Role=role_arn,
            Timeout=120,
            MemorySize=512,
            Environment=env,
        )
        _wait_lambda_updated(lam, names.lambda_name)
        log(f"updated lambda {names.lambda_name}")
    except lam.exceptions.ResourceNotFoundException:
        last = None
        for attempt in range(PROPAGATION_ATTEMPTS):
            try:
                lam.create_function(
                    FunctionName=names.lambda_name,
                    Runtime="python3.12",
                    Role=role_arn,
                    Handler="handler.lambda_handler",
                    Code={"ZipFile": payload},
                    # Browser navigation sleeps to let pages settle.
                    Timeout=120,
                    MemorySize=512,
                    Environment=env,
                    Description="Bridges AgentCore code interpreter and browser onto MCP",
                )
                break
            except ClientError as exc:
                if "cannot be assumed" not in str(exc):
                    raise
                last = exc
                log(f"waiting for role propagation ({attempt + 1}/{PROPAGATION_ATTEMPTS})")
                time.sleep(PROPAGATION_INTERVAL_SECONDS)
        else:
            raise SystemExit(f"lambda role never became assumable: {last}")
        _wait_lambda_active(lam, names.lambda_name)
        log(f"created lambda {names.lambda_name}")

    return lam.get_function(FunctionName=names.lambda_name)["Configuration"][
        "FunctionArn"
    ]


def ensure_interceptor(lam, names, role_arn):
    """Deploy the REQUEST interceptor that binds each tool call to its caller.

    Deliberately configured with no environment variables: that keeps Lambda from
    creating an aws/lambda KMS grant, so this function is immune to the stale-grant
    failure that `_has_stale_kms_grant` exists to repair on the tool Lambda.
    """
    payload = _zip_dir(INTERCEPTOR_DIR)
    description = "Stamps the gateway-verified caller onto every tool call"

    try:
        lam.get_function(FunctionName=names.interceptor_name)
        lam.update_function_code(FunctionName=names.interceptor_name, ZipFile=payload)
        _wait_lambda_updated(lam, names.interceptor_name)
        lam.update_function_configuration(
            FunctionName=names.interceptor_name,
            Role=role_arn,
            Timeout=10,
            MemorySize=256,
        )
        _wait_lambda_updated(lam, names.interceptor_name)
        log(f"updated interceptor {names.interceptor_name}")
    except lam.exceptions.ResourceNotFoundException:
        last = None
        for attempt in range(PROPAGATION_ATTEMPTS):
            try:
                lam.create_function(
                    FunctionName=names.interceptor_name,
                    Runtime="python3.12",
                    Role=role_arn,
                    Handler="handler.lambda_handler",
                    Code={"ZipFile": payload},
                    # Runs on every single tool call, so it must stay cheap.
                    Timeout=10,
                    MemorySize=256,
                    Description=description,
                )
                break
            except ClientError as exc:
                if "cannot be assumed" not in str(exc):
                    raise
                last = exc
                log(
                    f"waiting for interceptor role propagation "
                    f"({attempt + 1}/{PROPAGATION_ATTEMPTS})"
                )
                time.sleep(PROPAGATION_INTERVAL_SECONDS)
        else:
            raise SystemExit(f"interceptor role never became assumable: {last}")
        _wait_lambda_active(lam, names.interceptor_name)
        log(f"created interceptor {names.interceptor_name}")

    return lam.get_function(FunctionName=names.interceptor_name)["Configuration"][
        "FunctionArn"
    ]


def _wait_lambda_active(lam, name):
    lam.get_waiter("function_active_v2").wait(FunctionName=name)


def _wait_lambda_updated(lam, name):
    lam.get_waiter("function_updated_v2").wait(FunctionName=name)


def ensure_lambda_permission(lam, names, gateway_arn, with_interceptor=True):
    """Let the gateway (and only this gateway) invoke the tool and interceptor Lambdas."""
    statement_id = "AllowAgentCoreGateway"
    functions = [names.lambda_name]
    if with_interceptor:
        functions.append(names.interceptor_name)
    for function in functions:
        try:
            lam.remove_permission(FunctionName=function, StatementId=statement_id)
        except lam.exceptions.ResourceNotFoundException:
            pass
        lam.add_permission(
            FunctionName=function,
            StatementId=statement_id,
            Action="lambda:InvokeFunction",
            Principal="bedrock-agentcore.amazonaws.com",
            SourceArn=gateway_arn,
        )
    log(f"granted gateway lambda:InvokeFunction on {' and '.join(functions)}")


# --- gateway -----------------------------------------------------------------


def find_gateway(ctl, name):
    token = None
    while True:
        kwargs = {"maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        resp = ctl.list_gateways(**kwargs)
        for item in resp.get("items", []):
            if item.get("name") == name:
                return item
        token = resp.get("nextToken")
        if not token:
            return None


def interceptor_configurations(interceptor_arn):
    """Gateway kwargs that run the identity interceptor on every inbound request.

    `passRequestHeaders` is required: on a CUSTOM_JWT gateway the request context
    carries no identity at all, so the bearer token in the headers is the only place
    the caller's `sub` can be read.

    Returns kwargs rather than a list because the parameter has a minimum length of
    one — "no interceptor" has to be expressed by omitting the key, not by sending [].
    """
    if not interceptor_arn:
        return {}
    return {
        "interceptorConfigurations": [
            {
                "interceptor": {"lambda": {"arn": interceptor_arn}},
                "interceptionPoints": ["REQUEST"],
                "inputConfiguration": {"passRequestHeaders": True},
            }
        ]
    }


def ensure_gateway(ctl, names, role_arn, cognito, interceptor_arn=None):
    if cognito:
        pool_id, client_id = cognito
        authorizer_type = "CUSTOM_JWT"
        authorizer_config = {
            "customJWTAuthorizer": {
                "discoveryUrl": f"https://cognito-idp.{names.region}.amazonaws.com/{pool_id}/.well-known/openid-configuration",
                "allowedClients": [client_id],
            }
        }
    else:
        authorizer_type = "AWS_IAM"
        authorizer_config = None

    existing = find_gateway(ctl, names.gateway)
    if existing:
        gateway_id = existing["gatewayId"]
        # AgentCore refuses to change a gateway's authorizer type after creation,
        # so say what has to happen instead of surfacing a bare ValidationException.
        current_auth = ctl.get_gateway(gatewayIdentifier=gateway_id).get(
            "authorizerType"
        )
        if current_auth != authorizer_type:
            raise SystemExit(
                f"gateway {names.gateway} already exists with {current_auth} inbound auth "
                f"and AgentCore cannot switch it to {authorizer_type}.\n"
                f"Run 'down' first, or deploy under a different --project name."
            )
        kwargs = {
            "gatewayIdentifier": gateway_id,
            "name": names.gateway,
            "roleArn": role_arn,
            "protocolType": "MCP",
            "authorizerType": authorizer_type,
            # UpdateGateway replaces the whole configuration, so omitting the key
            # (--shared-sessions) detaches an interceptor attached by an earlier run.
            **interceptor_configurations(interceptor_arn),
        }
        if authorizer_config:
            kwargs["authorizerConfiguration"] = authorizer_config
        ctl.update_gateway(**kwargs)
        log(f"updated gateway {gateway_id}")
    else:
        kwargs = {
            "name": names.gateway,
            "roleArn": role_arn,
            "protocolType": "MCP",
            "authorizerType": authorizer_type,
            "description": "AgentCore built-in tools: web search, code interpreter, browser",
            **interceptor_configurations(interceptor_arn),
        }
        if authorizer_config:
            kwargs["authorizerConfiguration"] = authorizer_config
        resp = _create_gateway_with_retry(ctl, kwargs)
        gateway_id = resp["gatewayId"]
        log(f"created gateway {gateway_id}")

    return wait_gateway_ready(ctl, gateway_id)


def wait_gateway_ready(ctl, gateway_id):
    """Block until the gateway settles.

    Attaching an interceptor puts the gateway into UPDATING, and every
    Create/UpdateGatewayTarget call in that window fails with "Cannot perform
    operation ... when gateway is in UPDATING status".
    """
    deadline = time.time() + READY_TIMEOUT_SECONDS
    while True:
        gateway = ctl.get_gateway(gatewayIdentifier=gateway_id)
        status = gateway.get("status")
        if status == "READY":
            return gateway
        if status in ("CREATE_FAILED", "UPDATE_FAILED", "FAILED"):
            raise SystemExit(
                f"gateway {gateway_id} entered {status}: {gateway.get('statusReasons')}"
            )
        if time.time() >= deadline:
            raise SystemExit(
                f"gateway {gateway_id} still {status} after {READY_TIMEOUT_SECONDS}s"
            )
        time.sleep(POLL_INTERVAL_SECONDS)


def _create_gateway_with_retry(ctl, kwargs):
    last = None
    for attempt in range(PROPAGATION_ATTEMPTS):
        try:
            return ctl.create_gateway(**kwargs)
        except ClientError as exc:
            if not _is_propagation_error(exc):
                raise
            last = exc
            log(f"waiting for gateway role propagation ({attempt + 1}/{PROPAGATION_ATTEMPTS})")
            time.sleep(PROPAGATION_INTERVAL_SECONDS)
    raise SystemExit(f"gateway role never became usable: {last}")


def find_target(ctl, gateway_id, name):
    token = None
    while True:
        kwargs = {"gatewayIdentifier": gateway_id, "maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        resp = ctl.list_gateway_targets(**kwargs)
        for item in resp.get("items", []):
            if item.get("name") == name:
                return item
        token = resp.get("nextToken")
        if not token:
            return None


def wait_target_ready(ctl, gateway_id, target_id):
    deadline = time.time() + READY_TIMEOUT_SECONDS
    while time.time() < deadline:
        target = ctl.get_gateway_target(
            gatewayIdentifier=gateway_id, targetId=target_id
        )
        status = target.get("status")
        if status == "READY":
            return
        if status in ("CREATE_FAILED", "UPDATE_FAILED", "FAILED"):
            raise SystemExit(
                f"target {target_id} entered {status}: {target.get('statusReasons')}"
            )
        time.sleep(POLL_INTERVAL_SECONDS)
    raise SystemExit(f"target {target_id} not READY within {READY_TIMEOUT_SECONDS}s")


def ensure_target(ctl, gateway_id, name, configuration, description):
    existing = find_target(ctl, gateway_id, name)
    credentials = [{"credentialProviderType": "GATEWAY_IAM_ROLE"}]

    if existing:
        target_id = existing["targetId"]
        ctl.update_gateway_target(
            gatewayIdentifier=gateway_id,
            targetId=target_id,
            name=name,
            targetConfiguration=configuration,
            credentialProviderConfigurations=credentials,
        )
        wait_target_ready(ctl, gateway_id, target_id)
        log(f"updated target {name} ({target_id})")
        return target_id

    last = None
    for attempt in range(PROPAGATION_ATTEMPTS):
        try:
            resp = ctl.create_gateway_target(
                gatewayIdentifier=gateway_id,
                name=name,
                description=description,
                targetConfiguration=configuration,
                credentialProviderConfigurations=credentials,
            )
            target_id = resp["targetId"]
            wait_target_ready(ctl, gateway_id, target_id)
            log(f"created target {name} ({target_id})")
            return target_id
        except ClientError as exc:
            if not _is_propagation_error(exc):
                raise
            last = exc
            log(f"waiting for IAM propagation ({attempt + 1}/{PROPAGATION_ATTEMPTS})")
            time.sleep(PROPAGATION_INTERVAL_SECONDS)
    raise SystemExit(f"gateway role permission never propagated: {last}")


def bridge_target_configuration(lambda_arn):
    with open(TOOL_SCHEMA) as handle:
        tools = json.load(handle)
    return {
        "mcp": {
            "lambda": {"lambdaArn": lambda_arn, "toolSchema": {"inlinePayload": tools}}
        }
    }


def web_search_target_configuration():
    return {
        "mcp": {
            "connector": {
                # `connectorId` is the only member `source` accepts — passing a
                # `version` is rejected by both Create- and UpdateGatewayTarget.
                "source": {"connectorId": WEB_SEARCH_CONNECTOR_ID},
                "configurations": [{"name": "WebSearch", "parameterValues": {}}],
            }
        }
    }


# --- commands ----------------------------------------------------------------


def cmd_up(args):
    session = boto3.Session(region_name=args.region, profile_name=args.profile)
    account = session.client("sts").get_caller_identity()["Account"]
    names = Names(args.project, args.region, account)

    iam = session.client("iam")
    lam = session.client("lambda")
    s3 = session.client("s3")
    ctl = session.client(CONTROL)

    with_web_search = not args.no_web_search
    if with_web_search and args.region not in WEB_SEARCH_REGIONS:
        log(
            f"web search is not available in {args.region} "
            f"(only {', '.join(WEB_SEARCH_REGIONS)}); skipping that target"
        )
        with_web_search = False

    cognito = None
    if args.cognito_user_pool_id or args.cognito_client_id:
        if not (args.cognito_user_pool_id and args.cognito_client_id):
            raise SystemExit(
                "--cognito-user-pool-id and --cognito-client-id must be given together"
            )
        cognito = (args.cognito_user_pool_id, args.cognito_client_id)

    step("Screenshot bucket")
    bucket = ensure_bucket(s3, names)

    step("Lambda role")
    lambda_role_arn = ensure_lambda_role(iam, names, bucket)

    step("Tool bridge Lambda")
    lambda_arn = ensure_lambda(lam, names, lambda_role_arn, bucket, args.shared_sessions)

    interceptor_arn = None
    if args.shared_sessions:
        log(
            "--shared-sessions: no interceptor, so every caller shares one sandbox "
            "and browser profile. Only safe for a single trusted tenant."
        )
    else:
        step("Identity interceptor")
        interceptor_role_arn = ensure_interceptor_role(iam, names)
        interceptor_arn = ensure_interceptor(lam, names, interceptor_role_arn)

    step("Gateway role")
    gateway_role_arn = ensure_gateway_role(
        iam, names, lambda_arn, with_web_search, interceptor_arn
    )

    step("Gateway")
    gateway = ensure_gateway(ctl, names, gateway_role_arn, cognito, interceptor_arn)
    gateway_id = gateway["gatewayId"]
    gateway_arn = gateway["gatewayArn"]
    gateway_url = gateway["gatewayUrl"]

    step("Lambda invoke permission")
    ensure_lambda_permission(lam, names, gateway_arn, bool(interceptor_arn))

    step("Target: code interpreter + browser (Lambda bridge)")
    ensure_target(
        ctl,
        gateway_id,
        BRIDGE_TARGET_NAME,
        bridge_target_configuration(lambda_arn),
        "AgentCore code interpreter and browser exposed as MCP tools",
    )

    if with_web_search:
        step("Target: web search (built-in connector)")
        ensure_target(
            ctl,
            gateway_id,
            WEB_SEARCH_TARGET_NAME,
            web_search_target_configuration(),
            "AgentCore managed Web Search Tool connector",
        )
    else:
        existing = find_target(ctl, gateway_id, WEB_SEARCH_TARGET_NAME)
        if existing:
            log("removing existing web search target (disabled)")
            ctl.delete_gateway_target(
                gatewayIdentifier=gateway_id, targetId=existing["targetId"]
            )

    auth = "CUSTOM_JWT (Cognito)" if cognito else "AWS_IAM (SigV4)"
    print(
        json.dumps(
            {
                "gateway_name": names.gateway,
                "gateway_id": gateway_id,
                "gateway_arn": gateway_arn,
                "mcp_endpoint": gateway_url,
                "inbound_auth": auth,
                "targets": {
                    BRIDGE_TARGET_NAME: "code interpreter + browser (Lambda)",
                    WEB_SEARCH_TARGET_NAME: "connector" if with_web_search else "disabled",
                },
                "tool_lambda": lambda_arn,
                "identity_interceptor": interceptor_arn or "none (sessions are shared)",
                "session_isolation": (
                    "shared across all callers"
                    if args.shared_sessions
                    else "one sandbox per authenticated caller"
                ),
                "screenshot_bucket": bucket,
            },
            indent=2,
        )
    )
    print(
        "\nAttach to a harness as an agentcore_gateway tool:\n"
        f"  agentcore add tool --harness <name> --type agentcore_gateway \\\n"
        f"    --name builtin-tools --gateway-arn {gateway_arn}\n"
        f"\nVerify with:  {sys.argv[0]} test --region {args.region}",
        file=sys.stderr,
    )


def cmd_status(args):
    session = boto3.Session(region_name=args.region, profile_name=args.profile)
    account = session.client("sts").get_caller_identity()["Account"]
    names = Names(args.project, args.region, account)
    ctl = session.client(CONTROL)

    gateway = find_gateway(ctl, names.gateway)
    if not gateway:
        raise SystemExit(f"gateway {names.gateway} not found in {args.region}")

    detail = ctl.get_gateway(gatewayIdentifier=gateway["gatewayId"])
    print(f"gateway    {detail['name']}  ({detail['status']})")
    print(f"endpoint   {detail['gatewayUrl']}")
    print(f"auth       {detail.get('authorizerType')}")
    print(f"arn        {detail['gatewayArn']}")

    interceptors = detail.get("interceptorConfigurations") or []
    if interceptors:
        arn = interceptors[0].get("interceptor", {}).get("lambda", {}).get("arn", "?")
        print(f"identity   per-caller sessions via {arn.rsplit(':', 1)[-1]}")
    else:
        print("identity   SHARED — no interceptor, every caller uses the same sandbox")

    print("targets")
    for item in ctl.list_gateway_targets(
        gatewayIdentifier=gateway["gatewayId"], maxResults=100
    ).get("items", []):
        target = ctl.get_gateway_target(
            gatewayIdentifier=gateway["gatewayId"], targetId=item["targetId"]
        )
        mcp = target.get("targetConfiguration", {}).get("mcp", {})
        kind = next(iter(mcp), "unknown")
        names_in_target = []
        if kind == "lambda":
            names_in_target = [
                tool["name"]
                for tool in mcp["lambda"].get("toolSchema", {}).get("inlinePayload", [])
            ]
        elif kind == "connector":
            names_in_target = [
                config.get("name")
                for config in mcp["connector"].get("configurations", [])
            ]
        print(f"  {item['name']:<16} {target.get('status'):<8} {kind}")
        for tool in names_in_target:
            print(f"      - {tool}")


def cmd_down(args):
    session = boto3.Session(region_name=args.region, profile_name=args.profile)
    account = session.client("sts").get_caller_identity()["Account"]
    names = Names(args.project, args.region, account)

    ctl = session.client(CONTROL)
    lam = session.client("lambda")
    iam = session.client("iam")
    s3 = session.client("s3")

    gateway = find_gateway(ctl, names.gateway)
    if gateway:
        gateway_id = gateway["gatewayId"]
        for item in ctl.list_gateway_targets(
            gatewayIdentifier=gateway_id, maxResults=100
        ).get("items", []):
            ctl.delete_gateway_target(
                gatewayIdentifier=gateway_id, targetId=item["targetId"]
            )
            log(f"deleted target {item['name']}")
        # Targets are removed asynchronously; the gateway refuses to go while they linger.
        for _ in range(int(READY_TIMEOUT_SECONDS / POLL_INTERVAL_SECONDS)):
            if not ctl.list_gateway_targets(
                gatewayIdentifier=gateway_id, maxResults=100
            ).get("items"):
                break
            time.sleep(POLL_INTERVAL_SECONDS)
        ctl.delete_gateway(gatewayIdentifier=gateway_id)
        log(f"deleted gateway {gateway_id}")
    else:
        log("gateway not found")

    for function in (names.lambda_name, names.interceptor_name):
        try:
            lam.delete_function(FunctionName=function)
            log(f"deleted lambda {function}")
        except lam.exceptions.ResourceNotFoundException:
            log(f"lambda {function} not found")

    for role, inline in (
        (names.lambda_role, [f"{names.lambda_role}-policy"]),
        (names.interceptor_role, []),
        (names.gateway_role, [f"{names.gateway_role}-policy"]),
    ):
        try:
            for policy in inline:
                try:
                    iam.delete_role_policy(RoleName=role, PolicyName=policy)
                except iam.exceptions.NoSuchEntityException:
                    pass
            for attached in iam.list_attached_role_policies(RoleName=role).get(
                "AttachedPolicies", []
            ):
                iam.detach_role_policy(
                    RoleName=role, PolicyArn=attached["PolicyArn"]
                )
            iam.delete_role(RoleName=role)
            log(f"deleted role {role}")
        except iam.exceptions.NoSuchEntityException:
            log(f"role {role} not found")

    if args.delete_bucket:
        try:
            paginator = s3.get_paginator("list_object_versions")
            for page in paginator.paginate(Bucket=names.bucket):
                objects = [
                    {"Key": o["Key"], "VersionId": o["VersionId"]}
                    for key in ("Versions", "DeleteMarkers")
                    for o in page.get(key, [])
                ]
                if objects:
                    s3.delete_objects(
                        Bucket=names.bucket, Delete={"Objects": objects}
                    )
            s3.delete_bucket(Bucket=names.bucket)
            log(f"deleted bucket {names.bucket}")
        except ClientError as exc:
            log(f"bucket not deleted: {exc}")
    else:
        log(f"kept bucket {names.bucket} (pass --delete-bucket to remove it)")


def cmd_test(args):
    """Exercise the deployed gateway over MCP, the way an agent would."""
    session = boto3.Session(region_name=args.region, profile_name=args.profile)
    account = session.client("sts").get_caller_identity()["Account"]
    names = Names(args.project, args.region, account)
    ctl = session.client(CONTROL)

    gateway = find_gateway(ctl, names.gateway)
    if not gateway:
        raise SystemExit(f"gateway {names.gateway} not found in {args.region}")
    detail = ctl.get_gateway(gatewayIdentifier=gateway["gatewayId"])
    endpoint = detail["gatewayUrl"]
    authorizer = detail.get("authorizerType")

    from mcp_client import McpGatewayClient

    client = McpGatewayClient(
        endpoint,
        region=args.region,
        session=session,
        bearer_token=args.bearer_token,
        sigv4=(authorizer == "AWS_IAM" and not args.bearer_token),
        scope=args.scope,
    )

    step(f"tools/list against {endpoint}")
    tools = client.list_tools()
    for tool in tools:
        print(f"  {tool['name']}")

    def call(name, arguments):
        step(f"{name} {json.dumps(arguments)}")
        try:
            result = client.call_tool(name, arguments)
        except Exception as exc:
            print(f"  FAILED: {exc}")
            return None
        text = "\n".join(
            block.get("text", "")
            for block in result.get("content", [])
            if block.get("type") == "text"
        )
        print(f"  {text[:600]}")
        return result

    def resolve(suffix):
        for tool in tools:
            if tool["name"].endswith(suffix):
                return tool["name"]
        return None

    code_tool = resolve("execute_code")
    if code_tool:
        call(code_tool, {"code": "value = 6 * 7\nprint(value)"})
        # Proves session state survives between separate gateway calls.
        call(code_tool, {"code": "print('remembered', value)"})

        if detail.get("interceptorConfigurations"):
            step("session label cannot be chosen by the caller")
            # Both spellings are attempts to address someone else's sandbox: the
            # interceptor must overwrite them, so this stays in the caller's own
            # session and still sees its own `value`.
            result = call(
                code_tool,
                {
                    "code": "print('still mine', value)",
                    "session": "someone-else",
                    "__session": "someone-else",
                },
            )
            text = json.dumps(result) if result else ""
            if "still mine 42" in text:
                print("  OK: forged session labels were ignored")
            else:
                print("  WARNING: expected the forged label to be ignored")

    command_tool = resolve("execute_command")
    if command_tool:
        call(command_tool, {"command": "python --version"})

    nav_tool = resolve("browser_navigate")
    shot_tool = resolve("browser_screenshot")
    if nav_tool and shot_tool:
        call(nav_tool, {"url": args.url})
        call(shot_tool, {})

    search_tool = resolve("WebSearch")
    if search_tool:
        call(search_tool, {"query": "Amazon Bedrock AgentCore gateway", "maxResults": 3})
    else:
        log("no web search tool on this gateway")

    if not args.keep_sessions:
        for suffix in ("stop_code_session", "browser_stop"):
            tool = resolve(suffix)
            if tool:
                call(tool, {})


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--project", default="bap")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "ap-northeast-1"))
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("up", help="create or update the gateway and its targets")
    up.add_argument(
        "--no-web-search",
        action="store_true",
        help="skip the managed Web Search connector target",
    )
    up.add_argument(
        "--cognito-user-pool-id",
        help="use CUSTOM_JWT inbound auth against this Cognito pool instead of AWS_IAM",
    )
    up.add_argument("--cognito-client-id", help="app client allowed to call the gateway")
    up.add_argument(
        "--shared-sessions",
        action="store_true",
        help="skip the identity interceptor and let every caller share one sandbox "
        "and browser profile (single trusted tenant only)",
    )
    up.set_defaults(func=cmd_up)

    status = sub.add_parser("status", help="show the gateway, its targets and tools")
    status.set_defaults(func=cmd_status)

    test = sub.add_parser("test", help="call the tools through the gateway over MCP")
    test.add_argument(
        "--scope",
        help="X-Agent-Session-Scope header value, which subdivides this caller's own "
        "sandbox (e.g. one per conversation)",
    )
    test.add_argument("--url", default="https://example.com")
    test.add_argument(
        "--bearer-token", help="JWT to send instead of signing with SigV4"
    )
    test.add_argument(
        "--keep-sessions",
        action="store_true",
        help="leave the code interpreter and browser sessions running",
    )
    test.set_defaults(func=cmd_test)

    down = sub.add_parser("down", help="delete everything this script created")
    down.add_argument(
        "--delete-bucket",
        action="store_true",
        help="also delete the screenshot bucket and its contents",
    )
    down.set_defaults(func=cmd_down)

    args = parser.parse_args()
    try:
        args.func(args)
    except ClientError as exc:
        raise SystemExit(f"AWS error: {exc}")


if __name__ == "__main__":
    main()
