/**
 * McpAppView 순수 함수 자체 검증.
 *
 *   cd web && npx tsx src/lib/mcp-apps/frame.selftest.ts
 */
import assert from "node:assert/strict";
import { clampFrameHeight, messageText, parsePartialJson } from "./frame";

const checks: Array<[string, () => void]> = [
  ["보고된 높이를 그대로 쓴다 (정수로 올림)", () => {
    assert.equal(clampFrameHeight(240.2, 1000), 241);
  }],
  ["너무 작은 값은 최소 높이로", () => {
    assert.equal(clampFrameHeight(1, 1000), 64);
  }],
  ["화면의 85%를 넘지 않는다 — 100vh 콘텐츠의 되먹임을 끊는다", () => {
    assert.equal(clampFrameHeight(5000, 1000), 850);
  }],
  ["숫자가 아니거나 0 이하면 무시한다", () => {
    assert.equal(clampFrameHeight(undefined, 1000), null);
    assert.equal(clampFrameHeight("300", 1000), null);
    assert.equal(clampFrameHeight(NaN, 1000), null);
    assert.equal(clampFrameHeight(0, 1000), null);
    assert.equal(clampFrameHeight(-5, 1000), null);
  }],
  ["규격 형태: ContentBlock 배열의 텍스트를 이어 붙인다", () => {
    assert.equal(
      messageText([
        { type: "text", text: "첫 줄" },
        { type: "image", data: "…", mimeType: "image/png" },
        { type: "text", text: "둘째 줄" },
      ]),
      "첫 줄\n둘째 줄",
    );
  }],
  ["예전 데모 앱의 단일 객체도 받아 준다", () => {
    assert.equal(messageText({ type: "text", text: "요약" }), "요약");
  }],
  ["텍스트가 없으면 빈 문자열", () => {
    assert.equal(messageText(undefined), "");
    assert.equal(messageText([]), "");
    assert.equal(messageText([{ type: "text", text: 3 }]), "");
  }],
  ["완전한 JSON 은 그대로", () => {
    assert.deepEqual(parsePartialJson('{"a": 1}'), { a: 1 });
  }],
  ["열린 객체·배열·문자열을 닫는다", () => {
    assert.deepEqual(parsePartialJson('{"loc": "New Yo'), { loc: "New Yo" });
    assert.deepEqual(parsePartialJson('{"ids": [1, 2'), { ids: [1, 2] });
    assert.deepEqual(parsePartialJson('{"a": {"b": "c"'), { a: { b: "c" } });
  }],
  ["값이 오기 전에 끊긴 키와 끝 콤마는 버린다", () => {
    assert.deepEqual(parsePartialJson('{"a": 1, "b":'), { a: 1 });
    assert.deepEqual(parsePartialJson('{"a": 1, "b"'), { a: 1 });
    assert.deepEqual(parsePartialJson('{"a": 1,'), { a: 1 });
    assert.deepEqual(parsePartialJson('{"a'), {});
  }],
  ["이스케이프 도중 끊겨도 깨지지 않는다", () => {
    assert.deepEqual(parsePartialJson('{"a": "x\\'), { a: "x" });
  }],
  ["객체가 아니거나 빈 조각은 null", () => {
    assert.equal(parsePartialJson(""), null);
    assert.equal(parsePartialJson("[1,2"), null);
    assert.equal(parsePartialJson("not json"), null);
  }],
];

let failed = 0;
for (const [name, fn] of checks) {
  try {
    fn();
    console.log(`ok   ${name}`);
  } catch (e) {
    failed += 1;
    console.error(`FAIL ${name}\n     ${(e as Error).message}`);
  }
}
if (failed) {
  console.error(`\n${failed}개 실패`);
  process.exit(1);
}
console.log(`\n${checks.length}개 통과`);
