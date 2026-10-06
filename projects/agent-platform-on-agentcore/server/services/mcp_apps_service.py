"""레지스트리 레코드를 MCP 엔드포인트로 해석하고 릴레이한다.

엔드포인트 해석을 harness_service와 같은 방식(descriptor의 server.remotes[].url)으로
맞춘다 — 두 곳이 다른 규칙을 쓰면 에이전트가 붙는 서버와 앱이 붙는 서버가 갈라진다.
"""
import logging
import threading
from typing import Any, Dict, List, Optional

from botocore.exceptions import ClientError

from mcp_core.app_session import UiResourcePayload, parse_resource_result
from mcp_core.ui_meta import resource_uri_of
from models.registry import DESCRIPTOR_MCP
from services.harness_service import _mcp_url
from services.registry_service import RegistryService, gateway_arn_of

logger = logging.getLogger(__name__)

# AgentCore 게이트웨이가 타깃 이름을 툴 이름에 붙일 때 쓰는 구분자.
_GATEWAY_TOOL_SEPARATOR = "___"

# 툴 목록 세션에 거는 상한. 앱 탐색(`_walk_app_tools`)은 모든 MCP 레코드에 세션을
# 열고 라이브 SSE 턴 안에서 돈다. 엔드포인트가 망가진 레코드(`/mcp` 경로가 빠진
# 게이트웨이 URL이 실제 사례)는 JSON-RPC가 아닌 응답을 주고 SDK가 영원히 기다려,
# 나쁜 레코드를 스킵하려던 try/except가 발동하지 못한 채 턴 전체가 무한로딩이 됐다.
# 정상 서버는 실측 1.5~2s에 응답하므로 넉넉하되, Next 프록시의 30s idle 상한과 walk가
# 여러 레코드를 순회하는 점을 고려해 레코드당 상한을 그 아래로 둔다.
_LIST_TOOLS_TIMEOUT_SECONDS = 10.0


def gateway_mcp_url(gateway_arn: str) -> Optional[str]:
    """게이트웨이 ARN에서 MCP 엔드포인트를 조합한다.

    형식이 결정적이다:
      arn:aws:bedrock-agentcore:{region}:{acct}:gateway/{id}
      -> https://{id}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp

    게이트웨이에서 자동 등록된 레코드는 remotes[].url 을 갖지만, 손으로 등록한 것은
    gatewayArn 만 있을 수 있다. 그런 레코드도 검사할 수 있어야 한다 — UI 가 이미
    "ARN으로 조합됩니다" 라고 말하고 있었고, 실제로 조합하는 곳이 여기다.

    형식이 어긋나면 None. GetGateway 로 확인하지 않는 이유는 이 값을 쓰는 경로가
    곧 세션을 열기 때문이다 — 조합이 틀렸다면 연결이 실패하면서 드러난다.
    """
    parts = gateway_arn.split(":")
    if len(parts) < 6 or not parts[5].startswith("gateway/"):
        return None
    region = parts[3]
    gateway_id = parts[5].split("/", 1)[1]
    if not region or not gateway_id:
        return None
    return f"https://{gateway_id}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp"


# AgentCore 게이트웨이 MCP 엔드포인트의 호스트 표식. 이 호스트는 항상 `/mcp` 로 끝난다.
_GATEWAY_HOST_MARKER = ".gateway.bedrock-agentcore."


def _normalize_gateway_endpoint(url: str) -> str:
    """게이트웨이 URL 의 `/mcp` 경로를 보장한다.

    자동 등록은 AWS 가 보고한 gatewayUrl 을 그대로 저장하는데, 어떤 게이트웨이는 그
    값에 `/mcp` 경로가 없다 (실측: bap-gateway). bare 호스트는 JSON-RPC 가 아닌 AWS
    에러 envelope 를 돌려주고, MCP SDK 는 그걸 파싱하지 못하면 예외가 아니라 hang 한다.
    게이트웨이 엔드포인트 형식은 결정적(`gateway_mcp_url`)이므로 canonical 경로를
    붙인다. 게이트웨이가 아닌 URL 은 손대지 않는다 — 일반 MCP 서버는 `/sse` 등 다른
    경로를 쓸 수 있어 `/mcp` 를 강제하면 안 된다.
    """
    if _GATEWAY_HOST_MARKER in url and not url.rstrip("/").endswith("/mcp"):
        return url.rstrip("/") + "/mcp"
    return url


class McpAppsRelay:
    def __init__(self, registry: Optional[RegistryService] = None):
        self._registry = registry or RegistryService()
        # ui:// URI -> 그것을 제공하는 record_id. 탐색이 모든 MCP 레코드에 세션을
        # 열기 때문에 필요하다 — 앱 호출마다 그 비용을 낼 수는 없다.
        self._record_by_resource: Dict[str, str] = {}
        # 툴 이름 -> (record_id, resourceUri). harness 경로가 쓴다.
        self._app_by_tool: Dict[str, tuple] = {}
        # 세션 개설이 실패한 엔드포인트 URL. 앱 없는 툴은 캐시되지 않아 `app_for_tool`이
        # 툴 호출마다 다시 순회하는데, 망가진 엔드포인트는 상한(실측 10초)까지 기다렸다
        # 실패하므로 매번 그 대기를 무는 것을 막는다. 프로세스 수명 동안만 유지한다 —
        # 긍정 캐시(`_app_by_tool`)와 같은 staleness 특성이고, 재시작이 리셋이다.
        self._bad_endpoints: set = set()
        self._lock = threading.Lock()

    def resolve_endpoint(self, record_id: str) -> str:
        """레지스트리 레코드에서 MCP 엔드포인트 URL을 얻는다.

        브라우저가 보낸 값을 쓰지 않는 것이 이 메서드의 존재 이유다.

        `record_id`는 브라우저가 보낸 값이므로 잘못된 형식이나 없는 레코드는
        클라이언트 오류(ValueError → 400)로 바꾼다. 그냥 두면 AWS의
        ValidationException/ResourceNotFound가 502로 나가고, AWS 내부 메시지가
        응답 본문에 그대로 노출된다 — 배포 환경에서 실제로 그랬다.
        """
        try:
            detail = self._registry.get_record(record_id)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("ValidationException", "ResourceNotFoundException"):
                raise ValueError(f"MCP record '{record_id}' 를 찾을 수 없습니다.")
            raise

        url = _mcp_url(detail.descriptor_content)
        if url:
            # A gateway record's stored URL can omit the canonical `/mcp` path;
            # normalize it so a bare host does not hang the MCP client. Keeps the
            # "stored URL wins" rule (see the test) — only the path is repaired.
            return _normalize_gateway_endpoint(url)

        # remotes[].url 이 없어도 게이트웨이 ARN 만 있으면 엔드포인트가 결정된다.
        # 손으로 등록한 게이트웨이 레코드가 그 모양이다 (자동 등록은 URL 을 넣는다).
        gateway_arn = gateway_arn_of(detail.descriptor_content)
        if gateway_arn:
            composed = gateway_mcp_url(gateway_arn)
            if composed:
                return composed

        raise ValueError(
            f"MCP record '{record_id}' has no endpoint URL and cannot serve apps."
        )

    def record_for_resource(self, uri: str) -> Optional[str]:
        """이 ui:// 리소스를 제공하는 MCP 레코드를 찾는다.

        `agent-runtime`은 레지스트리를 모른다 — MCP 서버에만 연결하므로 툴의
        `_meta.ui.resourceUri`까지만 알고 record_id는 알 수 없다. 반면 릴레이 라우트는
        record_id로만 엔드포인트를 해석한다(브라우저가 보낸 엔드포인트를 신뢰하지 않는
        것이 그 라우트의 존재 이유다). 그 사이를 메우는 것이 이 메서드다.

        키를 툴 이름이 아니라 `resourceUri`로 잡은 이유: 게이트웨이는 타깃 이름을
        접두사로 붙여(`platform-status-app___get_platform_status`) 툴 이름을 바꾼다.
        `resourceUri`는 원본 서버가 정한 값이라 경로 중간에서 바뀌지 않는다.

        찾지 못하면 None. 예외를 던지지 않는 이유는 호출자(스트림)가 이것 때문에
        대화를 중단하면 안 되기 때문이다.
        """
        cached = self._record_by_resource.get(uri)
        if cached:
            return cached

        for record_id, tool_name, resource_uri in self._walk_app_tools():
            if resource_uri == uri:
                return record_id

        return None

    def app_for_tool(self, tool_name: str) -> Optional[tuple]:
        """이 툴에 앱이 붙어 있으면 (record_id, resourceUri) 를 준다.

        `record_for_resource` 의 역방향이고, harness 에이전트를 위해 있다. 런타임
        에이전트는 `agent-runtime` 이 툴 객체의 `_meta` 를 직접 읽어 `mcpApp` 을
        발행하지만, harness 는 AWS 가 실행하므로 서버가 받는 것은 Converse 이벤트뿐이다
        — 툴 정의도 `_meta` 도 없다. 스트림에 남는 유일한 단서가 툴 이름이다.

        게이트웨이 접두사(`<target>___<tool>`)를 벗긴 이름으로도 맞춰본다. harness 는
        MCP 레코드를 게이트웨이 도구로 붙이므로 이 경로에서는 접두사가 기본값이다.

        찾지 못하면 None — 앱이 없는 툴이 대부분이고, 그것이 오류일 이유는 없다.
        """
        candidates = {tool_name}
        if _GATEWAY_TOOL_SEPARATOR in tool_name:
            candidates.add(tool_name.split(_GATEWAY_TOOL_SEPARATOR, 1)[1])

        cached = self._app_by_tool.get(tool_name)
        if cached:
            return cached

        for record_id, name, resource_uri in self._walk_app_tools():
            if name in candidates:
                found = (record_id, resource_uri)
                with self._lock:
                    self._app_by_tool[tool_name] = found
                return found

        return None

    def _walk_app_tools(self):
        """등록된 MCP 레코드의 앱 달린 툴을 (record_id, 툴 이름, resourceUri) 로 훑는다.

        찾은 것은 캐시에 넣는다 — 이 순회는 레코드마다 MCP 세션을 열기 때문에, 호출마다
        반복할 수 있는 비용이 아니다.
        """
        for summary in self._registry.list_records(descriptor_type=DESCRIPTOR_MCP):
            record_id = summary.record_id
            if not record_id:
                continue
            try:
                url = self.resolve_endpoint(record_id)
            except Exception as exc:
                # 게이트웨이만 등록된 레코드는 엔드포인트가 없어 ValueError가 된다.
                # 하나 때문에 탐색을 멈추면 앱은 영원히 렌더링되지 않는다.
                logger.debug("Skipping record %s while resolving apps: %s", record_id, exc)
                continue

            # An endpoint that failed once (a timeout on a malformed gateway URL is
            # the case this guards) is skipped without paying the bound again.
            if url in self._bad_endpoints:
                continue

            try:
                tools = self._session_list_tools(url)
            except Exception as exc:
                with self._lock:
                    self._bad_endpoints.add(url)
                logger.warning("Could not list tools of %s: %s", record_id, exc)
                continue

            for tool in tools:
                meta = getattr(tool, "meta", None)
                resource_uri = resource_uri_of(meta if isinstance(meta, dict) else None)
                if not resource_uri:
                    continue
                name = getattr(tool, "name", None)
                with self._lock:
                    self._record_by_resource.setdefault(resource_uri, record_id)
                yield record_id, name, resource_uri

    def list_tools(self, record_id: str) -> tuple:
        """(endpoint, 툴 정보 리스트). 레코드 하나를 검사하기 위한 목록이다.

        `_walk_app_tools`와 달리 앱이 달린 툴만 고르지 않고 전부 준다 — 호출자는
        인스펙터이고, 앱이 없는 툴도 검사 대상이다.

        Returns:
            (url, [{"name", "description", "input_schema", "meta"}, ...])
        """
        url = self.resolve_endpoint(record_id)
        return url, [self._tool_info(tool) for tool in self._session_list_tools(url)]

    @staticmethod
    def _tool_info(tool: Any) -> Dict[str, Any]:
        """MCP SDK Tool을 라우트가 그대로 반환할 수 있는 dict로 만든다.

        `inputSchema`(규격)와 `input_schema`(일부 서버) 둘 다 본다. `meta`는 MCP Apps의
        `_meta.ui`가 흐르는 곳이라 유지한다 — 이 필드가 없어서 앱이 조용히 유실된 적이 있다.
        """
        schema = getattr(tool, "inputSchema", None)
        if not isinstance(schema, dict):
            schema = getattr(tool, "input_schema", None)
        meta = getattr(tool, "meta", None)
        if not isinstance(meta, dict):
            meta = getattr(tool, "_meta", None)
        return {
            "name": getattr(tool, "name", "") or "",
            "description": getattr(tool, "description", None),
            "input_schema": schema if isinstance(schema, dict) else None,
            "meta": meta if isinstance(meta, dict) else None,
        }

    def read_ui_resource(self, record_id: str, uri: str) -> UiResourcePayload:
        url = self.resolve_endpoint(record_id)
        result = self._session_read(url, uri)
        return parse_resource_result(result, uri)

    def tool_meta_for(self, record_id: str, tool_name: str) -> Optional[Dict[str, Any]]:
        url = self.resolve_endpoint(record_id)
        for tool in self._session_list_tools(url):
            if getattr(tool, "name", None) == tool_name:
                meta = getattr(tool, "meta", None)
                return meta if isinstance(meta, dict) else None
        raise ValueError(f"'{tool_name}' 툴을 찾을 수 없습니다.")

    def describe_tool(self, record_id: str, tool_name: str) -> Dict[str, Any]:
        """앱을 띄운 툴의 정의(MCP `Tool`)를 돌려준다 — `hostContext.toolInfo` 용.

        규격의 `toolInfo.tool` 은 name·inputSchema 를 포함한 툴 정의다. 게이트웨이 경유
        이름(`<target>___<tool>`)은 원본 서버에 없으므로 접두사를 벗긴 이름으로도 찾되,
        돌려주는 `name` 은 호스트가 부른 이름 그대로 둔다 — 앱이 그 이름으로 다시
        `tools/call` 을 하면 릴레이가 같은 규칙으로 찾는다.

        Raises:
            ValueError: 그런 툴이 없다 (라우트가 400 으로 옮긴다).
        """
        url = self.resolve_endpoint(record_id)
        by_name = {
            getattr(tool, "name", None): tool for tool in self._session_list_tools(url)
        }
        resolved = tool_name
        if resolved not in by_name and _GATEWAY_TOOL_SEPARATOR in tool_name:
            bare = tool_name.split(_GATEWAY_TOOL_SEPARATOR, 1)[1]
            if bare in by_name:
                resolved = bare
        tool = by_name.get(resolved)
        if tool is None:
            raise ValueError(f"'{tool_name}' 툴을 찾을 수 없습니다.")

        if hasattr(tool, "model_dump"):
            described = tool.model_dump(by_alias=True, exclude_none=True)
        else:
            info = self._tool_info(tool)
            described = {
                "name": info["name"],
                "description": info["description"],
                "inputSchema": info["input_schema"],
                "_meta": info["meta"],
            }
            described = {k: v for k, v in described.items() if v is not None}
        described["name"] = tool_name
        described.setdefault("inputSchema", {"type": "object"})
        return described

    def call_tool(
        self, record_id: str, tool_name: str, arguments: Dict[str, Any]
    ) -> Any:
        url = self.resolve_endpoint(record_id)
        return self._session_call_tool(url, tool_name, arguments)

    def call_app_tool_checked(
        self, record_id: str, tool_name: str, arguments: Dict[str, Any]
    ) -> Any:
        """visibility를 확인하고 같은 세션에서 툴을 호출한다.

        tool_meta_for + call_tool을 따로 부르면 MCP 세션이 두 번 열린다 (각각 TCP
        연결과 initialize 왕복). 앱 버튼 클릭마다 그 비용이 들고, 두 세션 사이에
        서버가 visibility를 바꾸면 검사한 것과 호출한 것이 달라진다. 그래서 한
        세션 안에서 목록을 읽고 검사한 뒤 호출한다.

        `PermissionError`는 "앱에서 호출할 수 없는 툴"을 뜻한다 (라우트가 403으로 옮긴다).
        `ValueError`는 "그런 툴이 없다"는 뜻이다 (400).

        판정 결과를 세션 밖에서 예외로 바꾸는 이유: 세션 안에서 raise하면 anyio
        task group이 그것을 ExceptionGroup으로 감싸버려서 라우트의
        `except PermissionError`가 걸리지 않고 403이 502가 된다. 실제 MCP 서버로
        검증하다 발견했다 — 스텁 테스트에서는 드러나지 않는다.
        """
        url = self.resolve_endpoint(record_id)
        verdict, result = self._session_list_and_call(url, tool_name, arguments)

        if verdict == "not_found":
            raise ValueError(f"'{tool_name}' 툴을 찾을 수 없습니다.")
        if verdict == "forbidden":
            raise PermissionError(f"'{tool_name}' 툴은 앱에서 호출할 수 없습니다.")
        return result

    # 아래 네 메서드는 실제 MCP 세션을 연다. 테스트는 McpAppsRelay 전체를 스텁으로
    # 대체하므로, 여기서 네트워크를 타는 것은 통합 테스트에서만 확인한다.
    def _session_read(self, url: str, uri: str) -> Any:
        from mcp_core.app_session import open_session

        with open_session(url) as session:
            return session.read_resource(uri)

    def _session_list_tools(self, url: str) -> List[Any]:
        # Time-bounded: a record whose endpoint answers a non-JSON-RPC body makes
        # the SDK client wait forever, and this is reached from the live SSE turn
        # via `_walk_app_tools`. A bound turns that hang into a TimeoutError the
        # walk's `try/except` already skips. See `_LIST_TOOLS_TIMEOUT_SECONDS`.
        from mcp_core.app_session import list_tools

        return list_tools(url, timeout=_LIST_TOOLS_TIMEOUT_SECONDS)

    def _session_list_and_call(
        self, url: str, tool_name: str, arguments: Dict[str, Any]
    ) -> Any:
        """한 세션에서 visibility를 검사하고 호출한다.

        Returns:
            ("ok", result) | ("not_found", None) | ("forbidden", None)

        판정을 예외가 아니라 값으로 돌려준다. 세션 안에서 raise하면 anyio task
        group이 ExceptionGroup으로 감싸서 호출자의 except가 타입을 놓친다.
        """
        from mcp_core.app_session import open_session
        from mcp_core.ui_meta import is_app_callable

        with open_session(url) as session:
            by_name = {
                getattr(tool, "name", None): tool for tool in session.list_tools().tools
            }

            # 게이트웨이 경유 에이전트는 `<target>___<tool>`로 부른다. 원본 서버는 그
            # 이름을 모르므로 접두사를 벗긴 이름으로도 찾아본다 — 이 세션 안에서 해야
            # 목록을 두 번 읽지 않는다.
            resolved = tool_name
            if resolved not in by_name and _GATEWAY_TOOL_SEPARATOR in tool_name:
                bare = tool_name.split(_GATEWAY_TOOL_SEPARATOR, 1)[1]
                if bare in by_name:
                    resolved = bare

            tool = by_name.get(resolved)
            if tool is None:
                return "not_found", None

            candidate = getattr(tool, "meta", None)
            meta = candidate if isinstance(candidate, dict) else None

            # 규격 MUST: visibility에 "app"이 없으면 앱 발신 호출을 거부한다.
            if not is_app_callable(meta):
                return "forbidden", None

            result = session.call_tool(resolved, arguments)
            return "ok", (
                result.model_dump(by_alias=True, exclude_none=True)
                if hasattr(result, "model_dump")
                else result
            )

    def _session_call_tool(
        self, url: str, tool_name: str, arguments: Dict[str, Any]
    ) -> Any:
        from mcp_core.app_session import open_session

        with open_session(url) as session:
            result = session.call_tool(tool_name, arguments)
            return (
                result.model_dump(by_alias=True, exclude_none=True)
                if hasattr(result, "model_dump")
                else result
            )


# One instance process-wide: the resource→record cache and its boto client are worth
# sharing between the relay routes and the stream that stamps app events.
_shared = McpAppsRelay()


def shared_relay() -> McpAppsRelay:
    return _shared
