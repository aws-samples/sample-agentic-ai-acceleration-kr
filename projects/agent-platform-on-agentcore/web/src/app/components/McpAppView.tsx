"use client";

/**
 * MCP App 하나를 렌더링하는 호스트.
 *
 * 라이프사이클(규격 SEP-1865):
 *   sandbox-proxy-ready → sandbox-resource-ready(html) → View의 ui/initialize
 *   → initialized → [ui/notifications/tool-input-partial …]
 *   → ui/notifications/tool-input → ui/notifications/tool-result | tool-cancelled
 *
 * `initialized`를 받기 전에는 View로 아무것도 보내지 않는다 (규격 MUST NOT).
 */
import {
  AppBridge,
  PostMessageTransport,
  type McpUiHostContext,
} from "@modelcontextprotocol/ext-apps/app-bridge";
import React, { useCallback, useEffect, useRef, useState } from "react";

import { buildAllowAttribute } from "@/lib/mcp-apps/csp";
import { callAppTool, describeAppTool, readUiResource } from "@/lib/mcp-apps/client";
import {
  FALLBACK_FRAME_HEIGHT,
  MAX_FRAME_VIEWPORT_RATIO,
  clampFrameHeight,
  messageText,
  parsePartialJson,
} from "@/lib/mcp-apps/frame";
import { readHostStyles } from "@/lib/mcp-apps/host-styles";

/**
 * 런타임에 ECS/ALB에서 결정되는 샌드박스 출처를 가져온다.
 * 빌드 시점 고정이 아니므로 /client-config 엔드포인트에서 런타임에 조회한다.
 *
 * 응답: { sandboxOrigin: string }
 *   - 배포 환경: process.env.SANDBOX_ORIGIN 값 (ALB 리스너 dns:8081)
 *   - 로컬: run.sh의 SANDBOX_PORT (기본 3001)
 *   - 미설정: 빈 문자열 (fallback 단계에서 처리)
 *
 * 메모이제이션: 모듈 수준의 Promise로 한 번만 fetch해서 재사용한다.
 * 같은 페이지 수명 내에 여러 컴포넌트가 이 값을 필요로 할 수 있기 때문.
 */
let sandboxOriginPromise: Promise<string> | null = null;

async function fetchRuntimeSandboxOrigin(): Promise<string> {
  if (sandboxOriginPromise) return sandboxOriginPromise;

  sandboxOriginPromise = (async () => {
    try {
      const response = await fetch("/client-config", {
        method: "GET",
        cache: "no-store",
      });
      if (!response.ok) {
        throw new Error(`/client-config 요청 실패: ${response.status}`);
      }
      const data = (await response.json()) as { sandboxOrigin?: string };
      return data.sandboxOrigin ?? "";
    } catch {
      // fetch 실패 시 빈 문자열 반환. fallback 단계에서 로컬 판정.
      return "";
    }
  })();

  return sandboxOriginPromise;
}

/**
 * 툴 결과를 규격의 CallToolResult 형태로 만든다.
 *
 * 스트림의 `toolResult.result`는 UI 전체에서 문자열이다(`ToolCall.result`도 string).
 * 앱은 `structuredContent`나 `content[]`를 읽으므로 문자열을 그대로 보내면 결과를
 * 해석하지 못한다 — 실측에서 앱이 "표시할 데이터가 없습니다"로 남았다.
 *
 * 문자열이 JSON이면 `structuredContent`까지 채운다. 앱이 그쪽을 먼저 보고, 그게
 * 없으면 text 블록을 JSON으로 파싱하는 것이 규격 권장 순서다.
 */
function asCallToolResult(result: unknown): unknown {
  if (result !== null && typeof result === "object") return result;

  const text = typeof result === "string" ? result : String(result ?? "");
  const wrapped: Record<string, unknown> = {
    content: [{ type: "text", text }],
  };

  try {
    const parsed = JSON.parse(text);
    if (parsed !== null && typeof parsed === "object") {
      wrapped.structuredContent = parsed;
    }
  } catch {
    // 평문 결과는 text 블록만으로 충분하다. 앱이 그것을 message로 읽는다.
  }

  return wrapped;
}

type ToolStatus = "pending" | "completed" | "error" | "interrupted";

interface McpAppViewProps {
  recordId: string;
  resourceUri: string;
  toolName: string;
  /** 앱을 띄운 tools/call 의 id. 규격 hostContext.toolInfo.id 로 나간다. */
  toolCallId?: string;
  /**
   * 스트림의 툴 인자. 인자가 아직 흘러들어오는 동안(`toolStatus === "pending"`)은
   * 불완전한 JSON **문자열**이고, 블록이 끝나면 객체가 된다.
   */
  toolInput?: Record<string, unknown> | string;
  toolStatus?: ToolStatus;
  toolResult?: unknown;
  /** 사용자가 스트림을 끊어 이 툴 호출이 취소됐다. */
  toolCancelled?: boolean;
  /** 앱이 ui/message로 대화에 넣는 텍스트. true면 들어갔다. 없으면 호스트가 거부한다. */
  onAppMessage?: (text: string) => boolean;
  /** 앱이 ui/update-model-context로 보내는 보조 컨텍스트. 매번 덮어쓴다. */
  onModelContext?: (context: Record<string, unknown>) => void;
}

export const McpAppView: React.FC<McpAppViewProps> = ({
  recordId,
  resourceUri,
  toolName,
  toolCallId,
  toolInput,
  toolStatus,
  toolResult,
  toolCancelled,
  onAppMessage,
  onModelContext,
}) => {
  const frameRef = useRef<HTMLIFrameElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const bridgeRef = useRef<AppBridge | null>(null);
  const transportRef = useRef<PostMessageTransport | null>(null);
  const readyRef = useRef(false);
  const pendingRef = useRef<{
    partial?: Record<string, unknown>;
    input?: Record<string, unknown>;
    result?: unknown;
    cancelled?: boolean;
  }>({});
  const flushRef = useRef<(() => void) | null>(null);
  const [error, setError] = useState<string | null>(null);
  // View가 size-changed로 보고한 높이(px). null이면 아직 보고가 없다.
  const [frameHeight, setFrameHeight] = useState<number | null>(null);
  const [csp, setCsp] = useState<string | null>(null);
  // 규격 `_meta.ui.prefersBorder`: 앱이 테두리와 배경을 원할 때만 그린다.
  const [prefersBorder, setPrefersBorder] = useState(false);
  const [sandboxOrigin, setSandboxOrigin] = useState<string | null>(null);
  const htmlRef = useRef<string | null>(null);
  const permissionsRef = useRef<string>("");
  // hostContext.toolInfo 에 넣을 툴 정의. 릴레이에서 비동기로 오므로 늦으면 갱신으로 보낸다.
  const toolInfoRef = useRef<Record<string, unknown> | null>(null);
  // 폴백 fetch가 한 번만 돌게 하는 표시와, 그때 쓸 최신 인자.
  const fetchedResultRef = useRef(false);
  // tool-input을 한 번이라도 보냈는가 (규격 순서 보장용).
  const sentInputRef = useRef(false);
  const toolInputRef = useRef<Record<string, unknown> | string | undefined>(toolInput);
  toolInputRef.current = toolInput;
  const observerRef = useRef<MutationObserver | null>(null);
  // 콜백은 ref 로 읽는다. 부모의 콜백은 `isLoading` 같은 상태에 따라 신원이 바뀌는데,
  // 그것을 bridge effect 의 의존성에 넣으면 턴마다 앱이 teardown·재마운트된다.
  const onAppMessageRef = useRef(onAppMessage);
  onAppMessageRef.current = onAppMessage;
  const onModelContextRef = useRef(onModelContext);
  onModelContextRef.current = onModelContext;

  // 규격 hostContext: View가 호스트 테마에 맞춰 렌더링할 수 있게 한다.
  // 이 앱은 .dark 클래스로 테마를 표현한다. `styles.variables`는 규격이 정한
  // 이름(`--color-background-primary` 등)으로 우리 디자인 토큰을 옮긴 것이다 —
  // 앱은 SDK의 applyHostStyleVariables로 그대로 적용한다.
  const readHostContext = useCallback((): McpUiHostContext => {
    const isDark = document.documentElement.classList.contains("dark");
    const ctx: McpUiHostContext = {
      theme: isDark ? "dark" : "light",
      displayMode: "inline",
      availableDisplayModes: ["inline"],
      styles: { variables: readHostStyles() },
      containerDimensions: readContainerDimensions(containerRef.current),
      locale: typeof navigator !== "undefined" ? navigator.language : undefined,
      timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    };
    if (toolInfoRef.current) {
      ctx.toolInfo = { id: toolCallId, tool: toolInfoRef.current as never };
    }
    return ctx;
  }, [toolCallId]);

  // Flush pending tool input and result when View initializes.
  // 규격 MUST NOT: initialized 받기 전에 View로 데이터 전송.
  // 규격 MUST: tool-input이 tool-result보다 먼저 나가야 한다.
  const flush = useCallback(() => {
    if (!readyRef.current || !bridgeRef.current) return;
    const bridge = bridgeRef.current;

    // 인자가 아직 흘러드는 중이면 부분 인자만 보낸다. 최종 input 은 블록이 끝나야 온다.
    if (pendingRef.current.partial !== undefined && !sentInputRef.current) {
      bridge.sendToolInputPartial({ arguments: pendingRef.current.partial });
      pendingRef.current.partial = undefined;
    }

    // 결과(또는 취소)를 보내기 전에 입력이 한 번은 나가야 한다 (규격 MUST). 인자
    // 없는 툴은 스트림에서 `args`가 비어 오거나 아예 오지 않으므로, 그때는 `{}`를
    // 보낸다 — 그러지 않으면 순서를 지키려다 결과를 영원히 붙잡게 된다.
    const closing =
      pendingRef.current.result !== undefined || pendingRef.current.cancelled === true;
    if (pendingRef.current.input !== undefined || (closing && !sentInputRef.current)) {
      bridge.sendToolInput({ arguments: pendingRef.current.input ?? {} });
      pendingRef.current.input = undefined;
      sentInputRef.current = true;
    }

    // 규격: tool-input이 tool-result보다 반드시 먼저 나가야 한다. 위에서 입력을 먼저
    // 보냈으므로 이 지점에서는 순서가 이미 지켜졌다.
    //
    // 여기서 결과를 보류하지 않는 이유: 입력이 끝내 오지 않는 경우가 정상 경로다.
    // 스트림은 툴 인자를 조각으로만 싣고, 인자 없는 툴(`get_platform_status`)은
    // `args`가 `{}`로 끝난다. 보류하면 그 뒤로 flush를 다시 부르는 사람이 없어 앱이
    // 영원히 결과를 못 받는다 — 실측에서 컨트롤이 비활성으로 남았다.
    if (pendingRef.current.result !== undefined) {
      bridge.sendToolResult(pendingRef.current.result as never);
      pendingRef.current.result = undefined;
      pendingRef.current.cancelled = undefined;
    } else if (pendingRef.current.cancelled) {
      bridge.sendToolCancelled({ reason: "user action" });
      pendingRef.current.cancelled = undefined;
    }
  }, []);

  // 0) 샌드박스 출처를 런타임에 가져온다 (빌드 시점 고정 불가).
  // 이 fetch는 ui:// 리소스 fetch보다 먼저 완료되어야 한다 (CSP 파라미터 완성 전).
  // sandboxOrigin === null은 로딩 중, "" (빈 문자열)은 fetch 실패/미설정.
  useEffect(() => {
    let cancelled = false;
    fetchRuntimeSandboxOrigin()
      .then((origin) => {
        if (cancelled) return;
        setSandboxOrigin(origin);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // 1) ui:// 리소스를 가져온다. CSP가 정해져야 샌드박스 URL을 만들 수 있다.
  useEffect(() => {
    let cancelled = false;
    readUiResource(recordId, resourceUri)
      .then(({ text, uiMeta }) => {
        if (cancelled) return;
        htmlRef.current = text;
        permissionsRef.current = buildAllowAttribute(
          uiMeta.permissions as Record<string, unknown> | undefined,
        );
        setPrefersBorder(uiMeta.prefersBorder === true);
        setCsp(encodeURIComponent(JSON.stringify(uiMeta.csp ?? {})));
      })
      .catch((e: Error) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [recordId, resourceUri]);

  // 1b) 앱을 띄운 툴의 정의 → hostContext.toolInfo. 리소스와 병렬로 가져오고, 실패해도
  // 앱은 뜬다. bridge 가 이미 만들어진 뒤에 도착하면 부분 갱신으로 보낸다.
  useEffect(() => {
    let cancelled = false;
    describeAppTool(recordId, toolName)
      .then((tool) => {
        if (cancelled) return;
        toolInfoRef.current = tool;
        bridgeRef.current?.setHostContext({
          toolInfo: { id: toolCallId, tool: tool as never },
        });
      })
      .catch((e: Error) => {
        console.warn("Could not describe the tool behind this app:", e.message);
      });
    return () => {
      cancelled = true;
    };
  }, [recordId, toolName, toolCallId]);

  // 2) 샌드박스가 준비되면 AppBridge를 생성하고 설정한다.
  useEffect(() => {
    const frame = frameRef.current;
    if (!frame || csp === null || sandboxOrigin === null) return;

    // sandboxOrigin이 빈 문자열이고 로컬이 아니면 에러 표시.
    // 로컬 판정: localhost, 127.0.0.1, 또는 포트 3000/8000 (run.sh 기본값).
    if (!sandboxOrigin) {
      const isLocalhost =
        window.location.hostname === "localhost" ||
        window.location.hostname === "127.0.0.1" ||
        window.location.hostname.endsWith(".local");
      const localPort = [3000, 8000, 3001, 8001].includes(
        parseInt(window.location.port, 10),
      );
      if (!isLocalhost || !localPort) {
        setError(
          "샌드박스 출처를 구성할 수 없습니다. 환경 변수 SANDBOX_ORIGIN을 확인하세요.",
        );
        return;
      }
      // 로컬 환경에서 빈 문자열인 경우 기본값으로 폴백.
      setSandboxOrigin("http://localhost:3001");
      return;
    }

    const bridge = new AppBridge(
      null, // no MCP client; we handle requests manually
      {
        name: "bap-host",
        version: "1.0.0",
      },
      {
        openLinks: {},
        serverTools: {},
        logging: {},
      },
      {
        hostContext: readHostContext(),
      }
    );
    bridgeRef.current = bridge;

    // 핸들러들을 등록한다. 이 호스트는 tools/call, resources/read, ui/open-link,
    // ui/message, ui/request-display-mode, ui/update-model-context를 지원한다.

    // oncalltool — 앱이 MCP 서버 도구를 호출한다.
    //
    // 릴레이는 서버의 CallToolResult를 그대로 돌려준다(content + structuredContent).
    // 그것을 다시 content[text]로 감싸면 앱이 래퍼 전체를 데이터로 읽어 표에
    // "content | [object Object]"가 찍힌다 — 라이브에서 새로 고침을 누른 뒤 그랬다.
    // 문자열 결과만 규격 형태로 만든다.
    bridge.oncalltool = async (params: any) => {
      const result = await callAppTool(recordId, params.name, params.arguments ?? {});
      return asCallToolResult(result) as never;
    };

    // onlistresources — 앱이 리소스를 나열한다
    bridge.onlistresources = async () => ({ resources: [] });

    // onreadresource — 앱이 같은 서버의 다른 리소스를 읽는 경로.
    bridge.onreadresource = async (params: any) => {
      const { text } = await readUiResource(recordId, params.uri);
      return { contents: [{ uri: params.uri, text }] };
    };

    // onopenlink — 새 탭으로 연다. noopener를 붙이지 않으면 열린 페이지가
    // window.opener로 이 호스트를 조작할 수 있다.
    bridge.onopenlink = async (params: any) => {
      const url = new URL(params.url);
      if (url.protocol !== "https:" && url.protocol !== "http:") {
        throw new Error("http(s) URL만 열 수 있습니다.");
      }
      window.open(url.href, "_blank", "noopener,noreferrer");
      return {};
    };

    // onmessage — 앱이 대화에 메시지를 넣는다 (규격 SHOULD). 호스트가 받을 수 없는
    // 상태(응답 중)면 isError 로 알린다 — 예외로 던지면 앱이 원인을 구분하지 못한다.
    bridge.onmessage = async (params: any) => {
      const handler = onAppMessageRef.current;
      if (!handler) throw new Error("이 호스트는 앱 메시지를 지원하지 않습니다.");
      const accepted = handler(messageText(params?.content));
      return accepted ? {} : { isError: true };
    };

    // size-changed — View가 본문 크기를 보고하면 컨테이너를 맞춘다 (규격 SHOULD).
    // 너비는 무시한다: inline 모드에서 앱은 메시지 열 너비를 그대로 쓴다.
    bridge.addEventListener("sizechange", ({ height }) => {
      const next = clampFrameHeight(height, window.innerHeight);
      if (next !== null) setFrameHeight(next);
    });

    // onrequestdisplaymode — inline만 지원한다고 선언했으므로 요청 모드를
    // 그대로 돌려준다.
    bridge.onrequestdisplaymode = async () => ({
      mode: "inline",
    });

    // onupdatemodelcontext — 다음 턴에 모델에게 전달될 보조 컨텍스트 (규격 SHOULD).
    // 규격은 매 요청이 이전 것을 덮어쓰도록 정한다 — useChat 이 마지막 것만 싣는다.
    bridge.onupdatemodelcontext = async (params: any) => {
      onModelContextRef.current?.(params ?? {});
      return {};
    };

    // Message handler for sandbox proxy ready notification
    const onMessage = (event: MessageEvent) => {
      // 샌드박스 출처와의 메시지만 처리한다 (CORS 정책에 따른 보안).
      // 출처는 스킴+호스트+포트까지 비교한다 (경로 제외).
      const expectedOrigin = new URL(sandboxOrigin).origin;
      if (event.origin !== expectedOrigin) return;
      if (event.data?.method === "ui/notifications/sandbox-proxy-ready") {
        frame.contentWindow?.postMessage(
          {
            jsonrpc: "2.0",
            method: "ui/notifications/sandbox-resource-ready",
            params: {
              html: htmlRef.current,
              sandbox: "allow-scripts allow-same-origin allow-forms",
              permissions: permissionsRef.current,
            },
          },
          sandboxOrigin,
        );
      }
    };

    // Connect the bridge to the iframe
    const contentWindow = frame.contentWindow;
    if (!contentWindow) return;

    const transport = new PostMessageTransport(contentWindow, contentWindow);
    transportRef.current = transport;

    // Store flush in ref so toolInput/toolResult effects can call it
    flushRef.current = flush;

    // Transition to ready when View completes initialization.
    // Use oninitialized property (typed), not addEventListener (unverified).
    bridge.oninitialized = () => {
      readyRef.current = true;
      flush();
    };

    // 테마 토글 시 ui/notifications/host-context-changed로 부분 갱신을 보낸다.
    const observer = new MutationObserver(() => {
      bridge.setHostContext(readHostContext());
    });
    observerRef.current = observer;
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class"],
    });

    // 컨테이너 크기가 바뀌면 hostContext.containerDimensions 를 갱신한다. 열 너비가
    // 바뀌는 순간(사이드바 접기 등) 앱이 레이아웃을 다시 잡을 수 있게.
    let resizeObserver: ResizeObserver | null = null;
    if (typeof ResizeObserver !== "undefined" && containerRef.current) {
      resizeObserver = new ResizeObserver(() => {
        bridge.setHostContext({
          containerDimensions: readContainerDimensions(containerRef.current),
        });
      });
      resizeObserver.observe(containerRef.current);
    }

    window.addEventListener("message", onMessage);

    // Connect transport
    bridge.connect(transport).catch((err: Error) => {
      setError(`Failed to connect bridge: ${err.message}`);
    });

    // Cleanup function
    return () => {
      window.removeEventListener("message", onMessage);
      if (observerRef.current) {
        observerRef.current.disconnect();
        observerRef.current = null;
      }
      resizeObserver?.disconnect();
      // 규격 SHOULD: teardown 응답을 기다린 뒤 정리한다 (데이터 유실 방지).
      // teardownResource는 Promise를 반환한다 - 기다린다.
      bridge
        .teardownResource({})
        .catch((err: Error) => {
          console.error("Teardown error:", err);
        });
      bridgeRef.current = null;
      transportRef.current = null;
      readyRef.current = false;
      // 새 bridge는 핸드셰이크를 처음부터 다시 하므로, 다음 View에도 tool-input을
      // 다시 보내야 한다.
      sentInputRef.current = false;
    };
  }, [csp, sandboxOrigin, recordId, flush, readHostContext]);

  // 3) 툴 입력을 pending에 큐잉하고 flush를 호출한다.
  // 규격 MUST NOT: initialized 받기 전에 전송 금지.
  // 규격 MUST: tool-input이 tool-result보다 먼저 나가야 함 (flush 내에서 강제).
  //
  // 인자가 아직 흘러드는 동안(`pending`)은 문자열이다. 그때는 부분 JSON 으로 복원해
  // `tool-input-partial` 로 보낸다 — 앱 신호가 블록 시작 시점에 오므로 이 구간이 실제로
  // 존재한다. 블록이 끝나 객체가 되면 최종 `tool-input` 을 보낸다.
  useEffect(() => {
    if (toolInput === undefined) return;
    if (typeof toolInput === "string") {
      if (toolStatus === "pending") {
        const partial = parsePartialJson(toolInput);
        if (partial) {
          pendingRef.current.partial = partial;
          flushRef.current?.();
        }
      }
      // pending 이 아닌데도 문자열이면 파싱에 실패한 인자다. 보낼 형태가 없다.
      return;
    }
    pendingRef.current.input = toolInput;
    pendingRef.current.partial = undefined;
    flushRef.current?.();
  }, [toolInput, toolStatus]);

  useEffect(() => {
    if (toolResult !== undefined) {
      pendingRef.current.result = asCallToolResult(toolResult);
      flushRef.current?.();
    }
  }, [toolResult]);

  // 사용자가 스트림을 끊었다 → 결과 대신 `tool-cancelled`. 폴백 fetch 도 하지 않는다:
  // 취소한 툴을 호스트가 몰래 다시 실행하는 것이 되기 때문이다.
  useEffect(() => {
    if (!toolCancelled || toolResult !== undefined) return;
    pendingRef.current.cancelled = true;
    fetchedResultRef.current = true;
    flushRef.current?.();
  }, [toolCancelled, toolResult]);

  /**
   * 스트림이 툴 결과를 주지 않을 때 호스트가 직접 가져온다.
   *
   * 규격은 호스트가 `ui/notifications/tool-result`를 보내야 한다고 정하는데, Strands
   * 런타임의 스트림에는 툴 결과가 아예 없다 — 실측한 이벤트 종류는 messageStart /
   * contentBlockDelta / contentBlockStart / contentBlockStop / messageStop /
   * metadata 뿐이다. 결과는 모델이 요약한 다음 턴의 텍스트로만 남는다. 그래서
   * `toolResult`가 오지 않으면 앱은 데이터를 영원히 못 받고 컨트롤이 비활성으로
   * 남는다 — 실측 화면이 그랬다.
   *
   * 호스트가 릴레이로 같은 툴을 한 번 호출해 그 결과를 보낸다. 이 툴은 모델이 이미
   * 호출한 것이고 앱을 띄우는 진입점이므로 읽기 동작이다.
   *
   * 인자가 아직 흘러드는 중(`pending`)이면 기다린다 — 불완전한 인자로 호출하면 안 된다.
   * 스트림이 결과를 싣게 되면 위 effect가 먼저 채우고 이 경로는 건너뛴다.
   */
  useEffect(() => {
    if (toolResult !== undefined || toolCancelled) return;
    if (!recordId || !toolName) return;
    if (toolStatus === "pending") return;
    // 한 앱 인스턴스당 한 번만 가져온다. `toolInput`은 객체라 렌더마다 신원이 바뀌고,
    // 그것을 의존성에 넣으면 스트리밍 중 매 렌더가 이전 요청을 cleanup으로 취소해
    // 결과가 끝까지 도착하지 않는다 — 실측에서 앱이 tool-input만 세 번 받고 컨트롤이
    // 비활성으로 남았다. 인자는 ref에서 읽어 최신값을 쓴다.
    if (fetchedResultRef.current) return;
    fetchedResultRef.current = true;

    let cancelled = false;
    const args = toolInputRef.current;
    callAppTool(recordId, toolName, typeof args === "object" && args ? args : {})
      .then((result) => {
        if (cancelled) return;
        pendingRef.current.result = asCallToolResult(result);
        flushRef.current?.();
      })
      .catch((e: Error) => {
        // 앱 자체는 이미 떠 있다. 결과를 못 가져온 것으로 화면을 지우지는 않는다.
        console.error("Could not fetch the tool result for this app:", e);
      });
    return () => {
      cancelled = true;
      // 가드를 되돌린다. 이 effect는 취소된 요청의 결과를 버리므로, 되돌리지 않으면
      // 다음 실행이 "이미 가져왔다"고 판단해 아무것도 하지 않고 앱은 결과를 못 받는다
      // (React 개발 모드의 이중 마운트가 정확히 이 경로를 밟는다).
      fetchedResultRef.current = false;
    };
  }, [recordId, toolName, toolResult, toolStatus, toolCancelled]);

  if (error) {
    return (
      <div className="my-4 rounded-md border border-border p-3 text-sm text-muted-foreground">
        앱을 불러오지 못했습니다: {error}
      </div>
    );
  }
  if (csp === null || sandboxOrigin === null) {
    return (
      <div className="my-4 text-sm text-muted-foreground">앱을 불러오는 중…</div>
    );
  }

  return (
    <div
      ref={containerRef}
      className={
        prefersBorder
          ? "my-4 overflow-hidden rounded-md border border-border bg-card"
          : "my-4 overflow-hidden rounded-md"
      }
      style={{
        height: frameHeight === null ? FALLBACK_FRAME_HEIGHT : `${frameHeight}px`,
        transition: "height 120ms ease",
      }}
    >
      <iframe
        ref={frameRef}
        title={`MCP App: ${toolName}`}
        src={`${sandboxOrigin}/sandbox?csp=${csp}`}
        sandbox="allow-scripts allow-same-origin"
        style={{ width: "100%", height: "100%", border: "none" }}
      />
    </div>
  );
};

/**
 * 규격 `containerDimensions`: 너비는 고정(열 너비), 높이는 상한만 준다 — 높이는 View 가
 * size-changed 로 정하기 때문이다. 컨테이너가 아직 없으면 화면 기준으로 어림한다.
 */
function readContainerDimensions(el: HTMLElement | null): McpUiHostContext["containerDimensions"] {
  const width = el ? Math.round(el.getBoundingClientRect().width) : undefined;
  const maxHeight = Math.round(window.innerHeight * MAX_FRAME_VIEWPORT_RATIO);
  return width && width > 0 ? { width, maxHeight } : { maxHeight };
}
