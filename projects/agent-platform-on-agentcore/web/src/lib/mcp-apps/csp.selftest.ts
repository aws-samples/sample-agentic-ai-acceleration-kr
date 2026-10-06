/**
 * CSP 구성 자체 검증.
 *
 * web에는 테스트 러너가 없으므로 노드로 직접 실행한다:
 *   cd web && npx tsx src/lib/mcp-apps/csp.selftest.ts
 *
 * 실패하면 0이 아닌 코드로 종료한다.
 */
import assert from "node:assert/strict";
import { buildAllowAttribute, buildCspHeader, sanitizeCspDomains } from "./csp";

const checks: Array<[string, () => void]> = [
  ["세미콜론이 든 도메인은 버린다", () => {
    // 허용하면 새 CSP 디렉티브를 주입할 수 있다.
    assert.deepEqual(sanitizeCspDomains(["https://ok.example", "x;script-src *"]),
      ["https://ok.example"]);
  }],
  ["따옴표가 든 도메인은 버린다", () => {
    // 'unsafe-eval' 같은 CSP 키워드 주입을 막는다.
    assert.deepEqual(sanitizeCspDomains(["'unsafe-eval'"]), []);
    assert.deepEqual(sanitizeCspDomains(['"x"']), []);
  }],
  ["공백이 든 도메인은 버린다", () => {
    assert.deepEqual(sanitizeCspDomains(["https://a.example https://b.example"]), []);
  }],
  ["개행이 든 도메인은 버린다", () => {
    assert.deepEqual(sanitizeCspDomains(["https://a.example\nscript-src *"]), []);
    assert.deepEqual(sanitizeCspDomains(["https://a.example\r\nx"]), []);
  }],
  ["문자열이 아닌 항목은 버린다", () => {
    assert.deepEqual(sanitizeCspDomains([null as never, 1 as never, "https://ok.example"]),
      ["https://ok.example"]);
  }],
  ["undefined는 빈 배열", () => {
    assert.deepEqual(sanitizeCspDomains(undefined), []);
  }],

  ["csp가 없으면 제한적 기본값", () => {
    const header = buildCspHeader(undefined);
    assert.match(header, /default-src 'none'/);
    assert.match(header, /connect-src 'none'/);
    assert.match(header, /frame-src 'none'/);
    assert.match(header, /object-src 'none'/);
    assert.match(header, /base-uri 'self'/);
  }],
  ["선언된 connect 도메인이 반영된다", () => {
    const header = buildCspHeader({ connectDomains: ["https://api.example.com"] });
    assert.match(header, /connect-src 'self' https:\/\/api\.example\.com/);
  }],
  ["resource 도메인은 script/style/img/font/media에 함께 붙는다", () => {
    const header = buildCspHeader({ resourceDomains: ["https://cdn.example.com"] });
    for (const directive of ["script-src", "style-src", "img-src", "font-src", "media-src"]) {
      assert.ok(
        new RegExp(`${directive}[^;]*https://cdn\\.example\\.com`).test(header),
        `${directive}에 cdn 도메인이 없다`,
      );
    }
  }],
  ["frameDomains가 없으면 frame-src none", () => {
    assert.match(buildCspHeader({}), /frame-src 'none'/);
  }],
  ["frameDomains가 있으면 반영된다", () => {
    const header = buildCspHeader({ frameDomains: ["https://www.youtube.com"] });
    assert.match(header, /frame-src https:\/\/www\.youtube\.com/);
  }],
  ["악성 도메인이 헤더에 새 디렉티브를 만들지 못한다", () => {
    const header = buildCspHeader({ connectDomains: ["x;default-src *"] });
    assert.ok(!header.includes("default-src *"), "주입이 통과했다");
  }],
  ["헤더에 개행이 남지 않는다", () => {
    // 개행이 남으면 HTTP 헤더 주입이 된다.
    const header = buildCspHeader({ connectDomains: ["https://a.example"] });
    assert.ok(!/[\r\n]/.test(header), "헤더에 개행이 있다");
  }],

  ["permissions가 allow 속성으로 변환된다", () => {
    assert.equal(buildAllowAttribute({ camera: {}, microphone: {} }), "camera; microphone");
  }],
  ["permissions가 없으면 빈 문자열", () => {
    assert.equal(buildAllowAttribute(undefined), "");
    assert.equal(buildAllowAttribute({}), "");
  }],
  ["모르는 permission은 무시한다", () => {
    assert.equal(buildAllowAttribute({ camera: {}, evil: {} }), "camera");
  }],

  ["NUL 문자가 든 도메인은 버린다", () => {
    // JS의 \s가 U+0000을 포함하지 않아 실제로 헤더까지 새어나갔다.
    assert.deepEqual(sanitizeCspDomains(["a" + String.fromCharCode(0) + "b"]), []);
  }],
  ["NEL(U+0085) 문자가 든 도메인은 버린다", () => {
    assert.deepEqual(sanitizeCspDomains(["a" + String.fromCharCode(0x85) + "b"]), []);
  }],
  ["쉼표가 든 도메인은 버린다", () => {
    assert.deepEqual(sanitizeCspDomains(["a,b"]), []);
  }],
  ["와일드카드 서브도메인은 살린다", () => {
    // 규격이 명시적으로 허용한다 — 허용 목록이 이걸 막으면 안 된다.
    assert.deepEqual(sanitizeCspDomains(["https://*.example.com"]), ["https://*.example.com"]);
  }],
  ["포트와 경로가 있는 소스는 살린다", () => {
    assert.deepEqual(
      sanitizeCspDomains(["wss://rt.example.com:8443", "https://cdn.example.com/assets/"]),
      ["wss://rt.example.com:8443", "https://cdn.example.com/assets/"],
    );
  }],
];

let failed = 0;
for (const [name, fn] of checks) {
  try {
    fn();
    console.log(`  ok  ${name}`);
  } catch (error) {
    failed += 1;
    console.error(`FAIL  ${name}\n      ${(error as Error).message}`);
  }
}
console.log(`\n${checks.length - failed}/${checks.length} passed`);
process.exit(failed === 0 ? 0 : 1);
