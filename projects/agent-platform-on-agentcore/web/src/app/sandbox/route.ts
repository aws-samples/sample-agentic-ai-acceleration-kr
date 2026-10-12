/**
 * 샌드박스 문서를 CSP 헤더와 함께 서빙한다.
 *
 * 라우트 핸들러인 이유: CSP는 요청마다 `_meta.ui.csp`에서 계산되고, 반드시 HTTP
 * 헤더로 나가야 한다. 정적 파일이나 `<meta>` 태그로는 둘 다 만족할 수 없다.
 *
 * 이 응답은 호스트와 **다른 출처**로 서빙돼야 한다 (규격 MUST). 배포에서는 별도
 * 포트의 리스너가, 로컬에서는 두 번째 dev 서버가 그 역할을 한다.
 */
import { NextResponse, type NextRequest } from "next/server";

import { buildCspHeader, sanitizeCspDomains, type McpUiCsp } from "@/lib/mcp-apps/csp";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function parseCsp(raw: string | null): McpUiCsp | undefined {
  if (!raw) return undefined;
  try {
    const parsed = JSON.parse(raw);
    return typeof parsed === "object" && parsed !== null ? parsed : undefined;
  } catch {
    // 파싱 실패는 제한적 기본값으로 떨어진다 — 넓은 CSP로 열어주면 안 된다.
    return undefined;
  }
}

/**
 * 샌드박스 문서에는 frame-src에 'self'가 있어야 한다.
 *
 * buildCspHeader의 기본값은 frame-src 'none'인데, 그것은 **앱**이 중첩 iframe을
 * 못 만들게 하려는 규칙이다. 이 헤더가 적용되는 문서는 샌드박스 자신이고, 샌드박스는
 * 내부 iframe을 만드는 것이 존재 이유다 — 'none'이면 내부 프레임이 차단되고
 * contentDocument에 접근할 수 없어 앱이 아예 로드되지 않는다.
 */
function sandboxCspHeader(csp?: McpUiCsp): string {
  const frames = ["'self'", ...sanitizeCspDomains(csp?.frameDomains)].join(" ");
  const base = buildCspHeader(csp);
  return base.replace(/frame-src [^;]+/, `frame-src ${frames}`);
}

const DOCUMENT = `<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="color-scheme" content="light dark" />
    <title>MCP App Sandbox</title>
    <style>
      html, body { margin: 0; height: 100vh; width: 100vw; background: transparent; }
      body { display: flex; flex-direction: column; }
      iframe { flex-grow: 1; border: 0; background: transparent; color-scheme: inherit; }
    </style>
  </head>
  <body>
    <script type="module" src="/sandbox-proxy.js"></script>
  </body>
</html>`;

export async function GET(request: NextRequest) {
  const csp = parseCsp(request.nextUrl.searchParams.get("csp"));
  const cspHeader = sandboxCspHeader(csp);

  // 검증: 이 문서는 내부 iframe을 만들어야 하므로 frame-src 'self'가 있어야 한다.
  if (!cspHeader.includes("frame-src 'self'")) {
    throw new Error("Sandbox CSP must include frame-src 'self'");
  }

  return new NextResponse(DOCUMENT, {
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Content-Security-Policy": cspHeader,
      // 이 문서는 앱마다 CSP가 다르므로 캐시하지 않는다.
      "Cache-Control": "no-store",
      // X-Frame-Options는 설정하지 않는다. 이 문서는 다른 출처에 임베드되어야 한다 (규격 MUST).
      // X-Frame-Options: SAMEORIGIN은 그것을 불가능하게 한다. 임베드 제어는 호스트의 CSP frame-src에 속한다.
    },
  });
}
