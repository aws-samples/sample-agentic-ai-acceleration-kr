/**
 * `_meta.ui.csp`에서 CSP 헤더를 만든다.
 *
 * 이 값은 신뢰할 수 없는 MCP 서버가 보낸 것이다. 검증 없이 헤더에 넣으면 악성 서버가
 * 세미콜론으로 새 디렉티브를 열거나 `'unsafe-eval'`을 주입할 수 있다.
 *
 * CSP는 반드시 HTTP 헤더로 나간다. `<meta>` 태그는 문서 안에 있어 콘텐츠가 변조할 수
 * 있으므로 규격이 헤더를 요구한다.
 */

export type McpUiCsp = {
  connectDomains?: string[];
  resourceDomains?: string[];
  frameDomains?: string[];
  baseUriDomains?: string[];
};

/** 호스트가 인정하는 Permission Policy 기능. 규격이 정한 네 개뿐이다. */
const PERMISSION_FEATURES: Record<string, string> = {
  camera: "camera",
  microphone: "microphone",
  geolocation: "geolocation",
  clipboardWrite: "clipboard-write",
};

/**
 * CSP 소스로 쓸 수 있는 문자만 허용한다.
 *
 * 차단 목록 방식은 위험하다: JS의 `\s`는 U+0000과 U+0085를 포함하지 않아서, 그
 * 두 글자가 헤더 값에 그대로 들어갔다. 헤더 안의 제어 문자는 Node·프록시·브라우저가
 * 서로 다르게 해석해 스머글링으로 이어진다. 그래서 "허용할 문자"를 정한다.
 *
 * 스킴, 호스트, 와일드카드 서브도메인, 포트, 경로까지 커버한다:
 *   https://cdn.example.com, https://*.example.com, wss://rt.example.com:8443
 */
const CSP_SOURCE = /^[A-Za-z0-9._:/*?=&+~%-]+$/;

/**
 * CSP 소스 목록에 넣어도 안전한 항목만 남긴다.
 *
 * 허용 목록을 사용해 제어 문자(NUL, NEL) 및 특수 분리자(세미콜론, 따옴표, 공백,
 * 쉼표)가 헤더에 들어가지 않게 한다.
 */
export function sanitizeCspDomains(domains?: string[]): string[] {
  if (!Array.isArray(domains)) return [];
  return domains.filter((d) => typeof d === "string" && d.length > 0 && CSP_SOURCE.test(d));
}

export function buildCspHeader(csp?: McpUiCsp): string {
  const resource = sanitizeCspDomains(csp?.resourceDomains).join(" ");
  const connect = sanitizeCspDomains(csp?.connectDomains).join(" ");
  const frame = sanitizeCspDomains(csp?.frameDomains).join(" ");
  const baseUri = sanitizeCspDomains(csp?.baseUriDomains).join(" ");

  const withResource = (directive: string, extra = "") =>
    `${directive} 'self'${extra ? ` ${extra}` : ""}${resource ? ` ${resource}` : ""}`;

  // 선언되지 않은 것은 전부 막는 것이 기본값이다 (규격 "Restrictive Default").
  const directives = [
    "default-src 'none'",
    withResource("script-src", "'unsafe-inline'"),
    withResource("style-src", "'unsafe-inline'"),
    withResource("img-src", "data:"),
    withResource("media-src", "data:"),
    withResource("font-src"),
    `connect-src ${connect ? `'self' ${connect}` : "'none'"}`,
    `frame-src ${frame || "'none'"}`,
    "object-src 'none'",
    `base-uri ${baseUri || "'self'"}`,
  ];

  // 한 줄로 합친다 — 개행이 남으면 HTTP 헤더 주입이 된다.
  return directives.join("; ");
}

export function buildAllowAttribute(
  permissions?: Record<string, unknown>,
): string {
  if (!permissions || typeof permissions !== "object") return "";
  return Object.keys(PERMISSION_FEATURES)
    .filter((key) => key in permissions)
    .map((key) => PERMISSION_FEATURES[key])
    .join("; ");
}
