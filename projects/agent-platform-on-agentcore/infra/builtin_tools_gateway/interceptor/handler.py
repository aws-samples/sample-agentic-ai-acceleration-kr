"""
Gateway REQUEST interceptor: stamps the caller's identity onto every tool call.

This exists because a Lambda MCP target cannot tell who is calling it. The gateway
hands a target only `client_context.custom` — gateway id, target id, tool name,
request id — with no caller identity, under both AWS_IAM and CUSTOM_JWT inbound
auth. Custom HTTP headers and MCP `params._meta` are dropped too, so the only
channel that reaches a target is `params.arguments`, which the model writes.

That makes a model-supplied session label useless as a security boundary: an agent
that sends `session: "someone-else"` would land in someone else's sandbox and
browser cookies. A REQUEST interceptor is the one place that sees both the
gateway-verified identity and the request body, so it derives the session label
here and overwrites whatever the model sent. `__session` is therefore trusted by
the target precisely because the model cannot influence it.

Contract (empirically confirmed against a deployed gateway):

    in   {"interceptorInputVersion": "1.0",
          "mcp": {"gatewayRequest": {"path", "httpMethod", "headers", "body",
                                     "context": {"identity": {"awsPrincipalArn"}}},
                  "rawGatewayRequest": {"body": "<json>"}}}
    out  {"interceptorOutputVersion": "1.0",
          "mcp": {"transformedGatewayRequest": {"body": <modified body>}}}

Echoing the input back is rejected with "InterceptorException - Received invalid
response from interceptor", so the response is always built explicitly.
"""
import base64
import hashlib
import json
import logging
import os
import re

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Header a trusted caller uses to subdivide its own identity — one sandbox per
# conversation rather than one per service account. Never a substitute for
# identity: it is only ever appended *under* the verified principal.
SCOPE_HEADERS = (
    "x-agent-session-scope",
    "x-amzn-bedrock-agentcore-runtime-session-id",
)

# Only the Lambda bridge has sessions to isolate. The web search connector has a
# fixed input schema, so stamping an extra argument onto its calls is at best
# ignored and at worst rejected. Tools arrive as "<target>___<tool>", and this must
# stay in step with BRIDGE_TARGET_NAME in deploy.py.
BRIDGE_TARGET = "builtin-tools"
TOOL_SEPARATOR = "___"

# Session names allow [A-Za-z0-9_-] and 100 chars total; the target prepends a
# prefix of up to 21 (project[:20] + "-"), leaving 79 for the label. Both halves are
# "<readable>-<digest>", so a full label is at most
# IDENTITY_CHARS + 1 + DIGEST_CHARS + 1 + SCOPE_CHARS + 1 + DIGEST_CHARS = 63 — under
# the budget, which matters because a final truncation would chop off the very digest
# that keeps two similar scopes apart.
SCOPE_CHARS = 24
IDENTITY_CHARS = 20
DIGEST_CHARS = 8
MAX_LABEL_CHARS = IDENTITY_CHARS + SCOPE_CHARS + 2 * DIGEST_CHARS + 3

REQUIRE_IDENTITY = os.environ.get("REQUIRE_IDENTITY", "true").lower() != "false"


def _sanitize(value, limit):
    return re.sub(r"[^A-Za-z0-9_-]", "-", value or "").strip("-")[:limit]


def _jwt_claims(headers):
    """Read claims from the inbound bearer token.

    A CUSTOM_JWT gateway leaves `context.identity` empty, so the token is the only
    identity signal. The signature is not checked here on purpose: the gateway
    already rejected every request that failed its own JWT validation, so anything
    reaching this function has an authentic token.
    """
    token = ""
    for key, value in (headers or {}).items():
        if key.lower() == "authorization":
            token = value or ""
            break
    if not token.lower().startswith("bearer "):
        return {}

    parts = token.split(None, 1)[1].split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}
    return claims if isinstance(claims, dict) else {}


def _identity(gateway_request):
    """Return (label_fragment, description) for the verified caller, or (None, why)."""
    identity = (gateway_request.get("context") or {}).get("identity") or {}

    # AWS_IAM gateways populate this; CUSTOM_JWT ones leave identity empty.
    principal = identity.get("awsPrincipalArn")
    if principal:
        # The tail of an ARN ("user/yoo", "assumed-role/x/session") is the readable
        # part; the digest keeps two different ARNs from colliding after truncation.
        tail = _sanitize(principal.rsplit("/", 1)[-1], IDENTITY_CHARS)
        return f"{tail}-{_digest(principal)}", principal

    claims = _jwt_claims(gateway_request.get("headers"))
    subject = claims.get("sub")
    if subject:
        return f"{_sanitize(subject, IDENTITY_CHARS)}-{_digest(subject)}", f"sub:{subject}"

    return None, "no verified caller identity on the request"


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:DIGEST_CHARS]


def _scope(headers):
    """Return a collision-free label fragment for the caller's scope header.

    Truncation alone is unsafe: an AgentCore runtime session id is derived from a
    thread id and padded to at least 33 characters, so two threads sharing a prefix
    would truncate to the same fragment and land in one sandbox. The digest is taken
    over the full value, so distinct scopes stay distinct.
    """
    for name in SCOPE_HEADERS:
        for key, value in (headers or {}).items():
            if key.lower() == name and value:
                return f"{_sanitize(value, SCOPE_CHARS)}-{_digest(value)}"
    return ""


def _deny(body, message):
    """Short-circuit the call: returning a response means the target is never invoked."""
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayResponse": {
                "statusCode": 200,
                "body": {
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "result": {
                        "isError": True,
                        "content": [{"type": "text", "text": message}],
                    },
                },
            }
        },
    }


def lambda_handler(event, context):
    gateway_request = (event.get("mcp") or {}).get("gatewayRequest") or {}
    # Copy before mutating: the input is echoed in logs on failure.
    body = json.loads(json.dumps(gateway_request.get("body") or {}))

    passthrough = {
        "interceptorOutputVersion": "1.0",
        "mcp": {"transformedGatewayRequest": {"body": body}},
    }

    # Interceptors also see initialize, tools/list and notifications; only a tool
    # call carries arguments worth stamping.
    if body.get("method") != "tools/call":
        return passthrough

    params = body.get("params") or {}
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        return passthrough

    # A stateless tool has no session to bind, so leave its arguments alone.
    tool = params.get("name") or ""
    if tool.split(TOOL_SEPARATOR, 1)[0] != BRIDGE_TARGET:
        return passthrough

    fragment, description = _identity(gateway_request)
    if not fragment:
        if REQUIRE_IDENTITY:
            logger.warning("denying tool call: %s", description)
            return _deny(
                body,
                "This tool requires an identified caller and the gateway supplied none.",
            )
        fragment = "anonymous"
        description = "anonymous (REQUIRE_IDENTITY=false)"

    scope = _scope(gateway_request.get("headers"))
    label = f"{fragment}-{scope}" if scope else fragment

    # Overwrite, never merge: a model-supplied session label must not survive, and
    # `session` is removed so a stale schema cannot reintroduce it.
    arguments.pop("session", None)
    arguments["__session"] = label[:MAX_LABEL_CHARS]

    logger.info(
        "tool=%s identity=%s scope=%s label=%s",
        tool,
        description,
        scope or "<none>",
        arguments["__session"],
    )
    return passthrough
