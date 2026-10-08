"""
Minimal MCP client for talking to an AgentCore Gateway.

Exists because the repo's agent runtime only knows how to send
`Authorization: Bearer <token>` (see agent-runtime/auth/access_token.py), while a
gateway with AWS_IAM inbound auth needs SigV4-signed requests. This speaks both, so
`deploy.py test` can verify a gateway under either authorizer without pulling in the
`mcp` package.

Streamable HTTP only, and only the two methods a smoke test needs: tools/list and
tools/call. Responses may come back as JSON or as an SSE stream, so both are parsed.
"""
import json
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

PROTOCOL_VERSION = "2025-06-18"
SERVICE = "bedrock-agentcore"
TIMEOUT_SECONDS = 180


class McpError(RuntimeError):
    pass


class McpGatewayClient:
    def __init__(
        self,
        endpoint,
        region,
        session=None,
        bearer_token=None,
        sigv4=True,
        scope=None,
    ):
        # get_gateway returns a URL that already ends in /mcp.
        self.endpoint = endpoint if endpoint.endswith("/mcp") else f"{endpoint}/mcp"
        self.region = region
        self.bearer_token = bearer_token
        self.sigv4 = sigv4 and not bearer_token
        self.session = session or boto3.Session(region_name=region)
        # Subdivides the caller's own sandbox, e.g. one per conversation. Read by the
        # gateway's identity interceptor, which only ever nests it under the verified
        # caller, so it cannot be used to reach another caller's session.
        self.scope = scope
        self._request_id = 0

    def _headers(self, body):
        headers = {
            "Content-Type": "application/json",
            # A gateway may answer either way; accept both.
            "Accept": "application/json, text/event-stream",
        }
        if self.scope:
            headers["X-Agent-Session-Scope"] = self.scope
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        elif self.sigv4:
            credentials = self.session.get_credentials()
            if credentials is None:
                raise McpError("no AWS credentials available for SigV4 signing")
            request = AWSRequest(
                method="POST", url=self.endpoint, data=body, headers=dict(headers)
            )
            SigV4Auth(credentials.get_frozen_credentials(), SERVICE, self.region).add_auth(
                request
            )
            headers = dict(request.headers)
        return headers

    def _rpc(self, method, params):
        self._request_id += 1
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
                "params": params,
            }
        ).encode()

        request = urllib.request.Request(
            self.endpoint, data=body, headers=self._headers(body), method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                raw = response.read().decode("utf-8", errors="replace")
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise McpError(f"HTTP {exc.code} from gateway: {detail[:500]}")

        payload = _parse_body(raw, content_type)
        if "error" in payload:
            raise McpError(json.dumps(payload["error"]))
        return payload.get("result", {})

    def list_tools(self):
        tools, cursor = [], None
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = self._rpc("tools/list", params)
            tools.extend(result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                return tools

    def call_tool(self, name, arguments):
        return self._rpc("tools/call", {"name": name, "arguments": arguments})


def _parse_body(raw, content_type):
    """Return the JSON-RPC payload from either a plain body or an SSE stream."""
    if "text/event-stream" in content_type:
        for line in raw.splitlines():
            if line.startswith("data:"):
                chunk = line[len("data:") :].strip()
                if not chunk:
                    continue
                try:
                    payload = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                # Skip notifications; the response carries an id.
                if "id" in payload or "error" in payload:
                    return payload
        raise McpError(f"no JSON-RPC payload in SSE response: {raw[:500]}")

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise McpError(f"unparseable response: {raw[:500]}")
