import { type NextRequest } from "next/server";

/**
 * 백엔드(FastAPI) 프록시.
 *
 * 예전에는 `src/middleware.ts`의 `NextResponse.rewrite()`로 프록시했는데, 그 경로는
 * 브라우저 fetch 로 오는 SSE 응답 본문을 스트리밍하지 못하고 버퍼링했다(계측: 서버·curl
 * 은 델타가 수 초에 걸쳐 흐르는데 실제 브라우저만 막판에 한 번에 flood 로 받음). Route
 * Handler 에서 `fetch(upstream)` 후 `upstream.body`(ReadableStream)를 그대로 반환하면
 * 청크가 브라우저까지 버퍼링 없이 흘러간다 — 이게 이 파일이 존재하는 이유다.
 *
 * BACKEND_ORIGIN 은 런타임(ECS)에 결정되므로 nodejs 런타임에서 process.env 로 읽는다.
 * 빌드 시점 인라인(next.config rewrites)은 localhost:8000 으로 굳어 배포에서 500 이 난다.
 */
const BACKEND_ORIGIN = process.env.BACKEND_ORIGIN || "http://localhost:8000";

// 업스트림으로 넘기면 안 되는 요청 헤더. accept-encoding 을 떼는 이유: 업스트림이
// 압축하면 스트림이 청크 단위로 버퍼링돼 다시 끊겨 보인다(SSE 는 비압축이어야 한다).
const STRIP_REQUEST_HEADERS = new Set([
  "host",
  "connection",
  "content-length",
  // We buffer the body (arrayBuffer) and let the upstream fetch recompute the
  // length, so a `transfer-encoding: chunked` from a proxy/tunnel that re-chunked
  // the request must not ride along — a buffered body plus a chunked header is a
  // contradiction node's fetch rejects with a 500, which is what broke multipart
  // uploads (skill validate/upload) for browsers behind such a hop.
  "transfer-encoding",
  "accept-encoding",
]);

// 홉-바이-홉 / Next 가 다시 계산하는 응답 헤더. 그대로 흘리면 프레이밍이 깨진다.
const STRIP_RESPONSE_HEADERS = new Set([
  "content-encoding",
  "content-length",
  "transfer-encoding",
  "connection",
]);

export async function proxyToBackend(
  request: NextRequest,
  pathSegments: string[]
): Promise<Response> {
  const { search } = new URL(request.url);
  const target = `${BACKEND_ORIGIN}/${pathSegments.join("/")}${search}`;

  const headers = new Headers();
  request.headers.forEach((value, key) => {
    if (!STRIP_REQUEST_HEADERS.has(key.toLowerCase())) headers.set(key, value);
  });

  const method = request.method;
  const init: RequestInit = { method, headers, redirect: "manual" };
  // Small JSON bodies — buffer them so we don't need fetch's half-duplex streaming.
  if (method !== "GET" && method !== "HEAD") {
    init.body = await request.arrayBuffer();
  }

  const upstream = await fetch(target, init);

  const responseHeaders = new Headers();
  upstream.headers.forEach((value, key) => {
    if (!STRIP_RESPONSE_HEADERS.has(key.toLowerCase())) responseHeaders.set(key, value);
  });

  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: responseHeaders,
  });
}
