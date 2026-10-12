"""
Bridges the AgentCore built-in tool primitives onto an MCP Gateway.

Code Interpreter and Browser are *not* gateway target types — unlike Web Search,
they have no connector, only data-plane APIs (`bedrock-agentcore`). This function
is the Lambda target that turns those APIs into MCP tools, so a single gateway can
serve web search, code execution and browsing side by side.

Sessions are the awkward part: both primitives are stateful, this function is not.
The session id is rediscovered by name on every call (`list_*_sessions` filtered to
READY), which keeps `x = 1` in one call visible to `print(x)` in the next, across
cold starts.

The session label is *not* a tool argument. A gateway tells a Lambda target nothing
about who is calling — no JWT claims, no principal — and it drops custom headers and
MCP `_meta`, so the only channel into this function is the argument dict the model
writes. A model-chosen label would therefore be a trivial way to read another user's
sandbox and browser cookies. Instead a REQUEST interceptor
(../interceptor/handler.py) derives the label from the gateway-verified caller and
stamps it as `__session`, overwriting anything the model sent. This function trusts
`__session` alone and refuses to run without it.

Only the standard library and boto3 are used — the Lambda runtime's bundled boto3
already models both primitives, so nothing is vendored.
"""
import json
import logging
import os
import re
import time

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CODE_INTERPRETER_ID = os.environ.get("CODE_INTERPRETER_ID", "aws.codeinterpreter.v1")
BROWSER_ID = os.environ.get("BROWSER_ID", "aws.browser.v1")
SESSION_PREFIX = os.environ.get("SESSION_PREFIX", "gw")
SESSION_TIMEOUT_SECONDS = int(os.environ.get("SESSION_TIMEOUT_SECONDS", "1800"))
SCREENSHOT_BUCKET = os.environ.get("SCREENSHOT_BUCKET", "")
SCREENSHOT_URL_TTL_SECONDS = int(os.environ.get("SCREENSHOT_URL_TTL_SECONDS", "3600"))

VIEWPORT_WIDTH = int(os.environ.get("VIEWPORT_WIDTH", "1280"))
VIEWPORT_HEIGHT = int(os.environ.get("VIEWPORT_HEIGHT", "800"))

# Time for the page to settle after a navigation before a screenshot is worth taking.
NAVIGATE_SETTLE_SECONDS = float(os.environ.get("NAVIGATE_SETTLE_SECONDS", "4"))

MAX_OUTPUT_CHARS = 40000

# Set false only for a single-tenant deployment with no interceptor attached.
REQUIRE_TRUSTED_SESSION = (
    os.environ.get("REQUIRE_TRUSTED_SESSION", "true").lower() != "false"
)

_clients = {}


class UntrustedSession(Exception):
    """Raised when no interceptor-stamped identity is present on the call."""


def _client(service):
    if service not in _clients:
        _clients[service] = boto3.client(service)
    return _clients[service]


def _label(args):
    """Return the caller's session label, which only the interceptor may set.

    `__session` is injected by the gateway REQUEST interceptor from the verified
    caller identity, so it cannot be chosen by the model. A plain `session`
    argument is ignored on purpose — honouring it would hand any agent a way to
    address another user's sandbox.
    """
    trusted = args.get("__session")
    if isinstance(trusted, str) and trusted.strip():
        return trusted.strip()
    if REQUIRE_TRUSTED_SESSION:
        raise UntrustedSession(
            "No caller identity on this request. The gateway's REQUEST interceptor "
            "must be attached so tool calls are bound to the authenticated user."
        )
    return "default"


def _session_name(label):
    """Build a session name that satisfies the API's 1-100 character limit."""
    label = re.sub(r"[^A-Za-z0-9_-]", "-", (label or "default").strip()) or "default"
    return f"{SESSION_PREFIX}-{label}"[:100]


# --- Code Interpreter --------------------------------------------------------


def _find_code_sessions(name):
    """Every READY session carrying this name, oldest first.

    A list is required rather than a single hit because AgentCore does *not* enforce
    unique session names: concurrent first calls each create their own session.
    """
    dp = _client("bedrock-agentcore")
    found, token = [], None
    while True:
        kwargs = {
            "codeInterpreterIdentifier": CODE_INTERPRETER_ID,
            "status": "READY",
            "maxResults": 100,
        }
        if token:
            kwargs["nextToken"] = token
        resp = dp.list_code_interpreter_sessions(**kwargs)
        found.extend(i for i in resp.get("items", []) if i.get("name") == name)
        token = resp.get("nextToken")
        if not token:
            return sorted(found, key=lambda i: (i.get("createdAt"), i["sessionId"]))


def _find_code_session(name):
    winner, _ = _resolve(_find_code_sessions(name), _stop_code)
    return winner


def _resolve(sessions, stop):
    """Pick one session for a name and retire any duplicates.

    Concurrent first calls for the same user each create a session, because
    duplicate names are allowed. Left alone the extras burn quota for the full idle
    timeout and, worse, later calls bind to whichever one `list` happens to return
    first, so the user's state appears to come and go between calls. Everyone
    independently keeps the oldest, which makes the choice converge without
    coordination, and the losers are stopped so the state cannot resurface.
    """
    if not sessions:
        return None, 0
    winner = sessions[0]["sessionId"]
    retired = 0
    for extra in sessions[1:]:
        try:
            stop(extra["sessionId"])
            retired += 1
        except ClientError:
            # Another invocation racing the same cleanup already stopped it.
            logger.info("duplicate session %s already gone", extra["sessionId"])
    if retired:
        logger.warning("retired %d duplicate session(s), kept %s", retired, winner)
    return winner, retired


def _stop_code(session_id):
    _client("bedrock-agentcore").stop_code_interpreter_session(
        codeInterpreterIdentifier=CODE_INTERPRETER_ID, sessionId=session_id
    )


def _code_session(label):
    """Return the session id for a label, starting a session the first time."""
    name = _session_name(label)
    existing = _find_code_session(name)
    if existing:
        return existing

    dp = _client("bedrock-agentcore")
    created = dp.start_code_interpreter_session(
        codeInterpreterIdentifier=CODE_INTERPRETER_ID,
        name=name,
        sessionTimeoutSeconds=SESSION_TIMEOUT_SECONDS,
    )["sessionId"]

    # No ConflictException is available to catch — duplicate names are accepted — so
    # the race is settled after the fact by re-reading and keeping the oldest.
    winner = _find_code_session(name) or created
    if winner != created:
        logger.info("lost a session-creation race; reusing the oldest session")
    return winner


def _render_block(block):
    """Flatten one MCP content block to text.

    executeCode returns plain `text` blocks, but listFiles returns `resource_link`
    blocks and readFiles returns `resource` blocks whose text is nested — reading
    only top-level `text` silently loses both.
    """
    kind = block.get("type")

    if kind == "resource_link" or "uri" in block:
        path = (block.get("uri") or "").replace("file://", "") or block.get("name", "")
        label = block.get("description") or ""
        suffix = "/" if label == "Directory" else ""
        return f"{path}{suffix}"

    if kind == "resource" or "resource" in block:
        resource = block.get("resource") or {}
        path = (resource.get("uri") or "").replace("file://", "")
        text = resource.get("text")
        if text is None and resource.get("blob") is not None:
            return f"{path}: <{len(resource['blob'])} bytes of binary content>"
        return f"{path}:\n{text}" if path else (text or "")

    if "text" in block:
        return block["text"]

    # Unknown block types are surfaced rather than dropped.
    return json.dumps(block, default=str)


def _invoke_code(label, tool, arguments):
    session_id = _code_session(label)
    resp = _client("bedrock-agentcore").invoke_code_interpreter(
        codeInterpreterIdentifier=CODE_INTERPRETER_ID,
        sessionId=session_id,
        name=tool,
        arguments=arguments,
    )

    # The response is an event stream; a tool call yields a single result event.
    text_parts, structured, is_error = [], None, False
    for event in resp.get("stream", []):
        result = event.get("result")
        if result is None:
            # Any non-result member is a modelled exception for this call.
            for key, value in event.items():
                return {
                    "error": f"{key}: {value.get('message') if isinstance(value, dict) else value}",
                    "session": label,
                }
            continue
        is_error = is_error or bool(result.get("isError"))
        structured = result.get("structuredContent", structured)
        for block in result.get("content", []):
            rendered = _render_block(block)
            if rendered:
                text_parts.append(rendered)

    output = "\n".join(text_parts)
    payload = {
        "session": label,
        "isError": is_error,
        "output": output[:MAX_OUTPUT_CHARS],
        "truncated": len(output) > MAX_OUTPUT_CHARS,
    }
    if structured:
        # stdout/stderr duplicate `output`; exitCode and timing are what add signal.
        payload["exitCode"] = structured.get("exitCode")
        payload["executionTime"] = structured.get("executionTime")
    return payload


def _execute_code(args):
    code = args.get("code")
    if not isinstance(code, str) or not code.strip():
        return {"error": "'code' is required."}
    language = (args.get("language") or "python").lower()
    if language not in ("python", "javascript", "typescript"):
        return {"error": f"Unsupported language: {language}"}
    return _invoke_code(
        _label(args),
        "executeCode",
        {
            "code": code,
            "language": language,
            "clearContext": bool(args.get("clear_context")),
        },
    )


def _execute_command(args):
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        return {"error": "'command' is required."}
    return _invoke_code(_label(args), "executeCommand", {"command": command})


def _write_files(args):
    files = args.get("files")
    if not isinstance(files, list) or not files:
        return {"error": "'files' must be a non-empty list of {path, text}."}
    content = []
    for entry in files:
        if not isinstance(entry, dict) or not entry.get("path"):
            return {"error": "Every file needs a 'path'."}
        content.append({"path": entry["path"], "text": entry.get("text") or ""})
    return _invoke_code(_label(args), "writeFiles", {"content": content})


def _read_files(args):
    paths = args.get("paths")
    if not isinstance(paths, list) or not paths:
        return {"error": "'paths' must be a non-empty list."}
    return _invoke_code(_label(args), "readFiles", {"paths": paths})


def _list_files(args):
    # "" is the sandbox root; "/" is rejected as path traversal and "." produces
    # uglier "file:///./name" URIs, so an empty default is the useful one.
    path = args.get("path") or ""
    return _invoke_code(_label(args), "listFiles", {"directoryPath": path})


def _remove_files(args):
    paths = args.get("paths")
    if not isinstance(paths, list) or not paths:
        return {"error": "'paths' must be a non-empty list."}
    return _invoke_code(_label(args), "removeFiles", {"paths": paths})


def _stop_code_session(args):
    label = _label(args)
    name = _session_name(label)
    session_id = _find_code_session(name)
    if not session_id:
        return {"session": label, "stopped": False, "reason": "no live session"}
    _client("bedrock-agentcore").stop_code_interpreter_session(
        codeInterpreterIdentifier=CODE_INTERPRETER_ID, sessionId=session_id
    )
    return {"session": label, "stopped": True}


# --- Browser -----------------------------------------------------------------


def _find_browser_sessions(name):
    dp = _client("bedrock-agentcore")
    found, token = [], None
    while True:
        kwargs = {
            "browserIdentifier": BROWSER_ID,
            "status": "READY",
            "maxResults": 100,
        }
        if token:
            kwargs["nextToken"] = token
        resp = dp.list_browser_sessions(**kwargs)
        found.extend(i for i in resp.get("items", []) if i.get("name") == name)
        token = resp.get("nextToken")
        if not token:
            return sorted(found, key=lambda i: (i.get("createdAt"), i["sessionId"]))


def _stop_browser(session_id):
    _client("bedrock-agentcore").stop_browser_session(
        browserIdentifier=BROWSER_ID, sessionId=session_id
    )


def _find_browser_session(name):
    winner, _ = _resolve(_find_browser_sessions(name), _stop_browser)
    return winner


def _browser_session(label):
    name = _session_name(label)
    existing = _find_browser_session(name)
    if existing:
        return existing

    dp = _client("bedrock-agentcore")
    created = dp.start_browser_session(
        browserIdentifier=BROWSER_ID,
        name=name,
        sessionTimeoutSeconds=SESSION_TIMEOUT_SECONDS,
        viewPort={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
    )["sessionId"]

    # Duplicate names are accepted, so settle the race by keeping the oldest.
    winner = _find_browser_session(name) or created
    if winner != created:
        logger.info("lost a session-creation race; reusing the oldest session")
    return winner


def _browser_act(label, action):
    session_id = _browser_session(label)
    result = _client("bedrock-agentcore").invoke_browser(
        browserIdentifier=BROWSER_ID, sessionId=session_id, action=action
    )["result"]

    # The result is a union keyed by the action that was sent.
    key = next(iter(result), None)
    outcome = result.get(key) or {}
    return session_id, key, outcome


def _browser_result(label, action):
    _, key, outcome = _browser_act(label, action)
    payload = {
        "session": label,
        "action": key,
        "status": outcome.get("status"),
    }
    if outcome.get("error"):
        payload["error"] = outcome["error"]
    return payload


def _browser_navigate(args):
    url = (args.get("url") or "").strip()
    if not re.match(r"^https?://", url):
        return {"error": "'url' must be an absolute http(s) URL."}

    label = _label(args)
    # There is no navigate action: focus the address bar, type, submit.
    for action in (
        {"keyShortcut": {"keys": ["ctrl", "l"]}},
        {"keyType": {"text": url}},
        {"keyPress": {"key": "enter"}},
    ):
        _, key, outcome = _browser_act(label, action)
        if outcome.get("status") != "SUCCESS":
            return {
                "session": label,
                "error": f"navigation failed at {key}: {outcome.get('error')}",
            }

    time.sleep(NAVIGATE_SETTLE_SECONDS)
    return {
        "session": label,
        "url": url,
        "status": "SUCCESS",
        "note": "Call browser_screenshot to see the page.",
    }


def _browser_screenshot(args):
    label = _label(args)
    _, _, outcome = _browser_act(label, {"screenshot": {"format": "PNG"}})
    if outcome.get("status") != "SUCCESS":
        return {
            "session": label,
            "error": outcome.get("error") or "screenshot failed",
        }

    data = outcome.get("data") or b""
    if not SCREENSHOT_BUCKET:
        return {
            "session": label,
            "error": "No screenshot bucket configured; set SCREENSHOT_BUCKET.",
        }

    key = f"screenshots/{_session_name(label)}/{int(time.time() * 1000)}.png"
    s3 = _client("s3")
    s3.put_object(
        Bucket=SCREENSHOT_BUCKET, Key=key, Body=data, ContentType="image/png"
    )
    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": SCREENSHOT_BUCKET, "Key": key},
        ExpiresIn=SCREENSHOT_URL_TTL_SECONDS,
    )
    return {
        "session": label,
        "status": "SUCCESS",
        "bytes": len(data),
        "url": url,
        "expires_in_seconds": SCREENSHOT_URL_TTL_SECONDS,
        # The presigned url expires in an hour, well before the object does (the
        # bucket keeps screenshots for 7 days). A reopened thread re-fetches the
        # image through the platform's own route, which reads this key and streams
        # the object; without it a stored transcript keeps only a dead url.
        "s3_key": key,
    }


def _browser_click(args):
    x, y = args.get("x"), args.get("y")
    if not isinstance(x, int) or not isinstance(y, int):
        return {"error": "'x' and 'y' must be integers."}
    action = {"mouseClick": {"x": x, "y": y}}
    if args.get("button"):
        action["mouseClick"]["button"] = str(args["button"]).upper()
    if args.get("click_count"):
        action["mouseClick"]["clickCount"] = int(args["click_count"])
    return _browser_result(_label(args), action)


def _browser_type(args):
    text = args.get("text")
    if not isinstance(text, str) or not text:
        return {"error": "'text' is required."}
    return _browser_result(_label(args), {"keyType": {"text": text}})


def _browser_press(args):
    key = (args.get("key") or "").lower()
    if not key:
        return {"error": "'key' is required."}
    action = {"keyPress": {"key": key}}
    if args.get("presses"):
        action["keyPress"]["presses"] = int(args["presses"])
    return _browser_result(_label(args), action)


def _browser_shortcut(args):
    keys = args.get("keys")
    if not isinstance(keys, list) or not keys:
        return {"error": "'keys' must be a non-empty list, e.g. [\"ctrl\", \"t\"]."}
    return _browser_result(
        _label(args), {"keyShortcut": {"keys": [str(k).lower() for k in keys]}}
    )


def _browser_scroll(args):
    x = args.get("x", VIEWPORT_WIDTH // 2)
    y = args.get("y", VIEWPORT_HEIGHT // 2)
    action = {
        "mouseScroll": {
            "x": int(x),
            "y": int(y),
            "deltaX": int(args.get("delta_x") or 0),
            # Negative deltaY scrolls down, so a positive "amount" reads naturally.
            "deltaY": int(args.get("delta_y") if args.get("delta_y") is not None else -300),
        }
    }
    return _browser_result(_label(args), action)


def _browser_stop(args):
    label = _label(args)
    name = _session_name(label)
    session_id = _find_browser_session(name)
    if not session_id:
        return {"session": label, "stopped": False, "reason": "no live session"}
    _client("bedrock-agentcore").stop_browser_session(
        browserIdentifier=BROWSER_ID, sessionId=session_id
    )
    return {"session": label, "stopped": True}


TOOLS = {
    "execute_code": _execute_code,
    "execute_command": _execute_command,
    "write_files": _write_files,
    "read_files": _read_files,
    "list_files": _list_files,
    "remove_files": _remove_files,
    "stop_code_session": _stop_code_session,
    "browser_navigate": _browser_navigate,
    "browser_screenshot": _browser_screenshot,
    "browser_click": _browser_click,
    "browser_type": _browser_type,
    "browser_press": _browser_press,
    "browser_shortcut": _browser_shortcut,
    "browser_scroll": _browser_scroll,
    "browser_stop": _browser_stop,
}


def lambda_handler(event, context):
    # The gateway passes "<target-name>___<tool-name>"; only the suffix identifies the tool.
    raw_name = ""
    client_context = getattr(context, "client_context", None)
    if client_context and getattr(client_context, "custom", None):
        raw_name = client_context.custom.get("bedrockAgentCoreToolName", "")

    tool_name = raw_name.split("___")[-1] if raw_name else ""
    # The trusted label is logged so a session can be traced back to its caller.
    logger.info(
        "tool=%s session=%s event=%s",
        tool_name,
        (event or {}).get("__session") if isinstance(event, dict) else None,
        json.dumps(event, default=str)[:500],
    )

    handler = TOOLS.get(tool_name)
    if handler is None:
        return {"error": f"Unknown tool: {tool_name or '<missing>'}"}

    try:
        result = handler(event if isinstance(event, dict) else {})
    except UntrustedSession as exc:
        # A deployment problem, not a tool failure: say so rather than leaking a
        # traceback, and never fall back to a shared session.
        logger.error("refused %s: %s", tool_name, exc)
        result = {"error": str(exc)}
    except Exception as exc:
        logger.exception("tool %s failed", tool_name)
        result = {"error": str(exc)}

    return result
