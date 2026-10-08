"""
Tool implementations exposed to agents through the AgentCore MCP Gateway.

The gateway invokes this function once per tool call. The tool name arrives in
the `bedrockAgentCoreToolName` client context (prefixed with the target name),
and the tool arguments are the event body.
"""
import json
import logging
import re
import urllib.error
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

logger = logging.getLogger()
logger.setLevel(logging.INFO)

HTTP_TIMEOUT_SECONDS = 20
MAX_PAGE_CHARS = 20000


def _current_time(args):
    tz_name = args.get("timezone") or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        return {"error": f"Unknown timezone: {tz_name}"}
    now = datetime.now(tz)
    return {
        "timezone": tz_name,
        "iso8601": now.isoformat(),
        "readable": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
    }


def _calculate(args):
    a = args.get("a")
    b = args.get("b")
    operation = (args.get("operation") or "").lower()

    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return {"error": "Both 'a' and 'b' must be numbers."}

    if operation == "divide" and b == 0:
        return {"error": "Cannot divide by zero."}

    results = {
        "add": lambda: a + b,
        "subtract": lambda: a - b,
        "multiply": lambda: a * b,
        "divide": lambda: a / b,
    }
    if operation not in results:
        return {"error": f"Unsupported operation: {operation}"}

    return {"operation": operation, "a": a, "b": b, "result": results[operation]()}


class _TextExtractor(HTMLParser):
    """Collect visible text, dropping script/style content."""

    _SKIP = {"script", "style", "noscript", "template"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self._skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skipping += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1

    def handle_data(self, data):
        if not self._skipping:
            text = data.strip()
            if text:
                self.parts.append(text)


def _fetch_url(args):
    url = (args.get("url") or "").strip()
    if not re.match(r"^https?://", url):
        return {"error": "'url' must be an absolute http(s) URL."}

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "bap-tools/1.0"},
    )

    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            content_type = response.headers.get("Content-Type", "")
            charset = response.headers.get_content_charset() or "utf-8"
            raw = response.read(2_000_000)
    except urllib.error.HTTPError as exc:
        return {"error": f"Fetch failed ({exc.code}) for {url}"}
    except Exception as exc:
        return {"error": f"Fetch failed: {exc}"}

    decoded = raw.decode(charset, errors="replace")

    if "html" in content_type.lower():
        parser = _TextExtractor()
        parser.feed(decoded)
        text = " ".join(parser.parts)
    else:
        text = decoded

    text = re.sub(r"\s+", " ", text).strip()
    return {
        "url": url,
        "content_type": content_type,
        "truncated": len(text) > MAX_PAGE_CHARS,
        "text": text[:MAX_PAGE_CHARS],
    }


def _create_artifact(args):
    """Acknowledge an artifact tool call; the document never lives here.

    Artifacts render in a side panel that the platform builds from the tool's
    *input*, not its result: the runtime does it for InvokeAgentRuntime and the
    server does it for a harness (InvokeHarness). This tool exists only so the
    gateway advertises it to any agent — including harness-composed ones — that
    attaches this gateway. Returning the body would just re-feed the whole
    document into the model's context, so return a short confirmation instead.
    """
    artifact_id = (args.get("artifact_id") or "").strip()
    title = args.get("title") or "Untitled"
    kind = args.get("kind") or "text"
    if not args.get("content"):
        return {"error": "'content' is required."}
    return {
        "status": "displayed",
        "message": (
            f"Created artifact '{artifact_id}' ({title}, {kind}). It is displayed "
            f"to the user in the artifact panel. To revise it, call update_artifact "
            f"with artifact_id='{artifact_id}'. Do not repeat its content in your "
            f"reply — briefly describe it instead."
        ),
    }


def _update_artifact(args):
    """Acknowledge a new artifact version; see _create_artifact for why it is a no-op."""
    artifact_id = (args.get("artifact_id") or "").strip()
    title = args.get("title") or "Untitled"
    kind = args.get("kind") or "text"
    if not args.get("content"):
        return {"error": "'content' is required."}
    return {
        "status": "displayed",
        "message": (
            f"Saved a new version of artifact '{artifact_id}' ({title}, {kind}). The "
            f"user sees it in the artifact panel. Do not repeat its content in your "
            f"reply — briefly describe what changed instead."
        ),
    }


TOOLS = {
    "current_time": _current_time,
    "calculate": _calculate,
    "fetch_url": _fetch_url,
    "create_artifact": _create_artifact,
    "update_artifact": _update_artifact,
}


def lambda_handler(event, context):
    # The gateway passes "<target-name>___<tool-name>"; only the suffix identifies the tool.
    raw_name = ""
    client_context = getattr(context, "client_context", None)
    if client_context and getattr(client_context, "custom", None):
        raw_name = client_context.custom.get("bedrockAgentCoreToolName", "")

    tool_name = raw_name.split("___")[-1] if raw_name else ""
    logger.info("tool=%s event=%s", tool_name, json.dumps(event, default=str)[:500])

    handler = TOOLS.get(tool_name)
    if handler is None:
        return {
            "statusCode": 400,
            "body": json.dumps({"error": f"Unknown tool: {tool_name or '<missing>'}"}),
        }

    try:
        result = handler(event if isinstance(event, dict) else {})
    except Exception as exc:
        logger.exception("tool %s failed", tool_name)
        result = {"error": str(exc)}

    return {"statusCode": 200, "body": json.dumps(result)}
