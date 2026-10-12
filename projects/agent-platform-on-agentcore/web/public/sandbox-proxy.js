/**
 * 이중 iframe 샌드박스의 외부 프록시.
 *
 * 규격(SEP-1865): "If the Host is a web page, it MUST wrap the View and communicate
 * with it through an intermediate Sandbox proxy. The Host and the Sandbox MUST have
 * different origins."
 *
 * 구조: 호스트(상위 창) ↔ 이 프록시(외부 iframe) ↔ View(내부 iframe)
 * 호스트와 내부 iframe은 출처가 달라 직접 통신할 수 없으므로 여기서 양방향 중계한다.
 *
 * `ui/notifications/sandbox-*` 만 여기서 처리하고 나머지는 그대로 흘린다.
 */

const SANDBOX_PREFIX = "ui/notifications/sandbox-";
const PROXY_READY = "ui/notifications/sandbox-proxy-ready";
const RESOURCE_READY = "ui/notifications/sandbox-resource-ready";

if (window.self === window.top) {
  throw new Error("이 문서는 iframe 안에서만 쓸 수 있습니다.");
}

if (!document.referrer) {
  throw new Error("referrer가 없어 임베딩한 출처를 확인할 수 없습니다.");
}

const HOST_ORIGIN = new URL(document.referrer).origin;
const OWN_ORIGIN = window.location.origin;

if (HOST_ORIGIN === OWN_ORIGIN) {
  // 같은 출처면 격리가 성립하지 않는다. 규격이 다른 출처를 요구하는 이유다.
  throw new Error(
    `샌드박스가 호스트와 같은 출처입니다(${OWN_ORIGIN}). 격리가 되지 않습니다.`,
  );
}

// 격리 자체 검증: window.top 접근은 SecurityError로 반드시 실패해야 한다.
// 성공하면 샌드박스 구성이 깨진 것이고, 신뢰할 수 없는 콘텐츠가 호스트를 조작할 수 있다.
try {
  void (window.top).location.href;
  throw new Error("SANDBOX_NOT_ISOLATED");
} catch (error) {
  if (error instanceof Error && error.message === "SANDBOX_NOT_ISOLATED") {
    throw new Error("샌드박스가 안전하게 구성되지 않았습니다.");
  }
  // SecurityError — 정상.
}

const inner = document.createElement("iframe");
inner.setAttribute("sandbox", "allow-scripts allow-same-origin allow-forms");
inner.style.cssText = "width:100%;height:100%;border:none;";
document.body.appendChild(inner);

window.addEventListener("message", (event) => {
  if (event.source === window.parent) {
    if (event.origin !== HOST_ORIGIN) {
      console.error("[Sandbox] 예상 밖 출처의 메시지를 버립니다:", event.origin);
      return;
    }

    const method = event.data?.method;
    if (typeof method === "string" && method.startsWith(SANDBOX_PREFIX)) {
      if (method === RESOURCE_READY) {
        const { html, sandbox, permissions } = event.data.params ?? {};
        if (typeof sandbox === "string") inner.setAttribute("sandbox", sandbox);
        if (typeof permissions === "string" && permissions) {
          inner.setAttribute("allow", permissions);
        }
        if (typeof html === "string") {
          // srcdoc 대신 document.write를 쓴다: srcdoc은 일부 라이브러리가 동작하지
          // 않고, 이 방식이면 내부 문서가 이 응답의 CSP 헤더를 상속한다.
          const doc = inner.contentDocument;
          if (doc) {
            doc.open();
            doc.write(html);
            doc.close();
          } else {
            inner.srcdoc = html;
          }
        }
      }
      // sandbox-* 는 호스트↔프록시 전용이다. 절대 View로 내려보내지 않는다.
      return;
    }

    inner.contentWindow?.postMessage(event.data, "*");
    return;
  }

  if (event.source === inner.contentWindow) {
    if (event.origin !== OWN_ORIGIN && event.origin !== "null") {
      console.error("[Sandbox] 내부 iframe의 출처가 예상과 다릅니다:", event.origin);
      return;
    }
    // "*" 가 아니라 구체 출처로 보낸다 — 메시지 가로채기를 막는다.
    window.parent.postMessage(event.data, HOST_ORIGIN);
  }
});

window.parent.postMessage(
  { jsonrpc: "2.0", method: PROXY_READY, params: {} },
  HOST_ORIGIN,
);
