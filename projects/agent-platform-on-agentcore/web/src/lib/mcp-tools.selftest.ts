/**
 * MCP 툴 스키마 → 폼 필드 → 인자 변환 자체 검증.
 *
 * web에는 테스트 러너가 없으므로 노드로 직접 실행한다:
 *   cd web && npx tsx src/lib/mcp-tools.selftest.ts
 *
 * 실패하면 0이 아닌 코드로 종료한다.
 */
import assert from "node:assert/strict";
import {
  buildArgumentFields,
  buildArguments,
  countArguments,
  looksLikeMarkdown,
  resultIsError,
  resultView,
  textBlocks,
} from "./mcp-tools";

const fieldsOf = (schema: Record<string, any> | undefined) =>
  buildArgumentFields(schema).map((f) => f.key);

const checks: Array<[string, () => void]> = [
  ["표준 JSON Schema에서 속성과 required를 읽는다", () => {
    const fields = buildArgumentFields({
      type: "object",
      properties: { q: { type: "string" }, limit: { type: "integer" } },
      required: ["q"],
    });
    assert.deepEqual(fields.map((f) => f.key), ["q", "limit"]);
    assert.equal(fields[0].schema.required, true);
    assert.equal(fields[1].schema.required, false);
  }],
  ["type이 없는 평면 속성 맵도 폼이 된다", () => {
    // 이 모양을 놓치면 인자를 받는 툴에 빈 폼이 뜬다.
    assert.deepEqual(fieldsOf({ city: { type: "string" } }), ["city"]);
  }],
  ["parameters 아래 중첩된 형태도 읽는다", () => {
    assert.deepEqual(
      fieldsOf({ parameters: { path: { type: "string" } } }),
      ["path"],
    );
  }],
  ["스키마가 없으면 필드도 없다", () => {
    assert.deepEqual(buildArgumentFields(undefined), []);
    assert.deepEqual(buildArgumentFields({ type: "object" }), []);
  }],
  ["속성처럼 보이지 않는 객체는 폼으로 만들지 않는다", () => {
    // 값이 전부 스칼라면 스키마 맵이 아니라 다른 무엇이다.
    assert.deepEqual(buildArgumentFields({ title: "hello", count: 3 }), []);
  }],
  ["boolean은 false, 그 외는 빈 문자열로 시작한다", () => {
    const fields = buildArgumentFields({
      properties: { on: { type: "boolean" }, name: { type: "string" } },
    });
    assert.equal(fields[0].value, false);
    assert.equal(fields[1].value, "");
  }],
  ["default가 있으면 그것이 초기값이다", () => {
    const fields = buildArgumentFields({ properties: { n: { type: "integer", default: 5 } } });
    assert.equal(fields[0].value, 5);
  }],

  ["빈 선택 인자는 보내지 않는다", () => {
    // ""를 보내면 없음과 빈 값을 구분하는 서버가 사용자가 타이핑하지 않은 값을 받는다.
    const fields = buildArgumentFields({
      properties: { q: { type: "string" }, opt: { type: "string" } },
      required: ["q"],
    });
    fields[0].value = "hi";
    assert.deepEqual(buildArguments(fields), { q: "hi" });
  }],
  ["빈 필수 인자는 그대로 보내 서버가 검증하게 한다", () => {
    const fields = buildArgumentFields({
      properties: { q: { type: "string" } },
      required: ["q"],
    });
    assert.deepEqual(buildArguments(fields), { q: "" });
  }],
  ["숫자 문자열은 숫자로 변환한다", () => {
    const fields = buildArgumentFields({
      properties: { i: { type: "integer" }, f: { type: "number" } },
      required: ["i", "f"],
    });
    fields[0].value = "42";
    fields[1].value = "1.5";
    assert.deepEqual(buildArguments(fields), { i: 42, f: 1.5 });
  }],
  ["숫자가 아닌 문자열은 원본을 유지한다", () => {
    // 조용히 NaN을 보내면 서버 오류 메시지가 원인을 가리키지 못한다.
    const fields = buildArgumentFields({
      properties: { i: { type: "integer" } },
      required: ["i"],
    });
    fields[0].value = "abc";
    assert.deepEqual(buildArguments(fields), { i: "abc" });
  }],
  ["array 필드의 JSON은 파싱하고, 아니면 한 항목으로 싼다", () => {
    const fields = buildArgumentFields({
      properties: { a: { type: "array" }, b: { type: "array" } },
      required: ["a", "b"],
    });
    fields[0].value = '["x", "y"]';
    fields[1].value = "solo";
    assert.deepEqual(buildArguments(fields), { a: ["x", "y"], b: ["solo"] });
  }],
  ["object 필드의 JSON을 파싱한다", () => {
    const fields = buildArgumentFields({
      properties: { o: { type: "object" } },
      required: ["o"],
    });
    fields[0].value = '{"k": 1}';
    assert.deepEqual(buildArguments(fields), { o: { k: 1 } });
  }],
  ["boolean은 항상 보낸다 (false도 값이다)", () => {
    // false를 빈 값으로 취급해 버리면 토글을 끈 것이 전달되지 않는다.
    const fields = buildArgumentFields({ properties: { on: { type: "boolean" } } });
    assert.deepEqual(buildArguments(fields), { on: false });
  }],

  ["countArguments는 폼 필드 수와 일치한다", () => {
    const schema = { properties: { a: { type: "string" }, b: { type: "string" } } };
    assert.equal(countArguments(schema), buildArgumentFields(schema).length);
    assert.equal(countArguments(undefined), 0);
  }],

  ["textBlocks는 text 블록만 순서대로 모은다", () => {
    assert.deepEqual(
      textBlocks({ content: [{ type: "text", text: "a" }, { type: "image" }, { type: "text", text: "b" }] }),
      ["a", "b"],
    );
  }],
  ["text 블록이 없으면 빈 배열", () => {
    assert.deepEqual(textBlocks({ content: [{ type: "image" }] }), []);
    assert.deepEqual(textBlocks({ structuredContent: { a: 1 } }), []);
    assert.deepEqual(textBlocks(null), []);
  }],

  ["structuredContent가 있으면 그것을 트리로 본다", () => {
    const view = resultView({
      structuredContent: { ok: true },
      content: [{ type: "text", text: '{"ok": true}' }],
    });
    assert.equal(view.kind, "json");
    assert.deepEqual((view as any).value, { ok: true });
    // raw는 서버가 실제로 보낸 텍스트여야 한다 (재직렬화가 아니라).
    assert.equal((view as any).raw, '{"ok": true}');
  }],
  ["JSON 문자열이 담긴 text 블록을 풀어서 트리로 본다", () => {
    // MCP 툴 대부분이 이 모양이다 — 이것을 산문으로 취급하면 한 줄 JSON 덩어리가 뜬다.
    const view = resultView({
      content: [{ type: "text", text: '{"city": "Seoul", "temp": 21}' }],
    });
    assert.equal(view.kind, "json");
    assert.deepEqual((view as any).value, { city: "Seoul", temp: 21 });
  }],
  ["JSON text 블록이 여러 개면 배열로 묶는다", () => {
    const view = resultView({
      content: [
        { type: "text", text: '{"a": 1}' },
        { type: "text", text: '{"b": 2}' },
      ],
    });
    assert.deepEqual((view as any).value, [{ a: 1 }, { b: 2 }]);
  }],
  ["진짜 산문은 텍스트로 남는다", () => {
    const view = resultView({ content: [{ type: "text", text: "작업 완료" }] });
    assert.equal(view.kind, "text");
    assert.equal((view as any).text, "작업 완료");
  }],
  ["일부만 JSON이면 전체를 텍스트로 본다", () => {
    // 반쪽만 트리로 만들면 나머지가 사라진 것처럼 보인다.
    const view = resultView({
      content: [
        { type: "text", text: '{"a": 1}' },
        { type: "text", text: "그리고 메모" },
      ],
    });
    assert.equal(view.kind, "text");
  }],
  ["스칼라 JSON은 트리로 만들지 않는다", () => {
    // 42나 "ok"에 트리를 씌우면 한 줄을 읽으려고 접힌 노드를 펴게 된다.
    assert.equal(resultView({ content: [{ type: "text", text: "42" }] }).kind, "text");
    assert.equal(resultView({ content: [{ type: "text", text: '"ok"' }] }).kind, "text");
  }],
  ["text 블록이 없으면 결과 객체 전체를 보여준다", () => {
    // 이미지/리소스 블록만 있는 결과가 조용히 사라지지 않게 한다.
    const view = resultView({ content: [{ type: "image", data: "..." }] });
    assert.equal(view.kind, "json");
    assert.deepEqual((view as any).value, { content: [{ type: "image", data: "..." }] });
  }],
  ["반환값이 없으면 empty", () => {
    assert.equal(resultView(null).kind, "empty");
    assert.equal(resultView(undefined).kind, "empty");
  }],
  ["빈 text 블록은 없는 것으로 본다", () => {
    assert.equal(resultView({ content: [{ type: "text", text: "   " }] }).kind, "json");
  }],

  ["채팅이 받는 평문 JSON 문자열도 트리로 본다", () => {
    // 채팅의 툴 결과는 CallToolResult가 아니라 문자열 하나다: harness 경로가
    // toolResult 델타를 합쳐 json.dumps한 결과가 그대로 온다. 이걸 놓치면
    // 채팅에서만 한 줄 JSON 덩어리가 뜬다.
    const view = resultView('{"city": "Seoul", "temp": 21}');
    assert.equal(view.kind, "json");
    assert.deepEqual((view as any).value, { city: "Seoul", temp: 21 });
    // raw는 서버가 보낸 바이트 그대로여야 한다.
    assert.equal((view as any).raw, '{"city": "Seoul", "temp": 21}');
  }],
  ["JSON 배열 문자열도 트리로 본다", () => {
    const view = resultView('[{"a": 1}, {"b": 2}]');
    assert.equal(view.kind, "json");
    assert.deepEqual((view as any).value, [{ a: 1 }, { b: 2 }]);
  }],
  ["JSON이 아닌 문자열은 텍스트로 남는다", () => {
    // 툴 실패 메시지 대부분이 이 모양이다.
    const view = resultView("Error: connection refused");
    assert.equal(view.kind, "text");
    assert.equal((view as any).text, "Error: connection refused");
  }],
  ["스칼라 문자열에는 트리를 씌우지 않는다", () => {
    assert.equal(resultView("1048576").kind, "text");
    assert.equal(resultView("true").kind, "text");
    assert.equal(resultView("작업을 완료했습니다").kind, "text");
  }],
  ["스트리밍 중 잘린 JSON은 텍스트로 보여준다", () => {
    // 결과가 델타로 도착하는 동안 문자열은 파싱되지 않는다. 그 사이에는 받은
    // 만큼을 그대로 보여주고, 완성되면 트리로 바뀐다 — 던지지 않는 것이 요점이다.
    const view = resultView('{"results": [{"title": "절반만 도착한');
    assert.equal(view.kind, "text");
    assert.equal((view as any).text, '{"results": [{"title": "절반만 도착한');
  }],
  ["빈 문자열은 결과가 없는 것으로 본다", () => {
    // 문자열을 블록처럼 다루면서 ""가 json으로 새는 일이 없어야 한다.
    assert.equal(resultView("").kind, "empty");
    assert.equal(resultView("   ").kind, "empty");
  }],

  ["제목·코드펜스·표는 마크다운으로 본다", () => {
    // 이걸 놓치면 리포트를 반환하는 툴의 결과가 `## 요약`과 파이프 문자
    // 그대로 고정폭으로 찍힌다.
    assert.equal(looksLikeMarkdown("## 요약\n\n한 문장."), true);
    assert.equal(looksLikeMarkdown("설명\n\n```py\nprint(1)\n```"), true);
    assert.equal(
      looksLikeMarkdown("| 이름 | 값 |\n| --- | --- |\n| a | 1 |"),
      true,
    );
  }],
  ["약한 신호는 두 개 이상일 때만 마크다운으로 본다", () => {
    assert.equal(looksLikeMarkdown("- 첫째\n- 둘째"), true);
    assert.equal(looksLikeMarkdown("1. 하나\n2. 둘"), true);
    assert.equal(looksLikeMarkdown("**완료**"), false);
    assert.equal(looksLikeMarkdown("- 항목 하나뿐"), false);
  }],
  ["줄 구조가 중요한 평문은 마크다운으로 보지 않는다", () => {
    // 오판의 대가가 비대칭이다: 마크다운으로 렌더하면 한 줄 개행이 한 단락으로
    // 합쳐져서, 로그와 트레이스백을 읽게 해주던 줄 구조가 사라진다.
    assert.equal(looksLikeMarkdown("Error: connection refused"), false);
    assert.equal(
      looksLikeMarkdown(
        'Traceback (most recent call last):\n  File "a.py", line 2, in <module>\n    raise ValueError(1)\nValueError: 1',
      ),
      false,
    );
    assert.equal(
      looksLikeMarkdown("-rw-r--r-- 1 ubuntu ubuntu 12 Aug 15 01:00 a.txt"),
      false,
    );
    assert.equal(looksLikeMarkdown("1048576"), false);
    assert.equal(looksLikeMarkdown(""), false);
  }],

  ["isError는 툴이 실행되고 실패했음을 뜻한다", () => {
    assert.equal(resultIsError({ isError: true, content: [] }), true);
    assert.equal(resultIsError({ content: [] }), false);
    assert.equal(resultIsError(null), false);
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
