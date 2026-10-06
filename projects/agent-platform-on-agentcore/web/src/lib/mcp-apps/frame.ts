/**
 * McpAppView가 쓰는 순수 함수. 컴포넌트 파일에서 분리한 이유는 노드로 자체 검증을
 * 돌리기 위해서다(`frame.selftest.ts`) — 컴포넌트 파일은 React 없이 import할 수 없다.
 */

/**
 * View가 크기를 보고하기 전까지의 높이. SDK 없이 만든 앱은 끝까지 보고하지 않을 수
 * 있으므로 그때도 쓸 만한 값이어야 한다.
 */
export const FALLBACK_FRAME_HEIGHT = "50vh";
export const MIN_FRAME_HEIGHT = 64;
export const MAX_FRAME_VIEWPORT_RATIO = 0.85;

/**
 * View가 `ui/notifications/size-changed`로 보낸 높이를 컨테이너에 쓸 수 있는 값으로
 * 자른다. 앱이 보고하는 값은 자기 문서의 scrollHeight라서 신뢰할 수 없다 — 아래로는
 * 컨트롤 하나가 들어갈 만큼, 위로는 화면의 85%까지만 허용한다. 위 한도가 없으면
 * `height: 100vh` 콘텐츠가 매번 조금씩 더 큰 값을 보고하는 되먹임에 빠진다.
 */
export function clampFrameHeight(height: unknown, viewportHeight: number): number | null {
  if (typeof height !== "number" || !Number.isFinite(height) || height <= 0) return null;
  const max = Math.max(MIN_FRAME_HEIGHT, Math.round(viewportHeight * MAX_FRAME_VIEWPORT_RATIO));
  return Math.min(Math.max(Math.ceil(height), MIN_FRAME_HEIGHT), max);
}

/**
 * `ui/message`의 content는 규격상 ContentBlock 배열이다. 텍스트 블록만 이어 붙인다.
 * 예전 데모 앱이 단일 객체를 보냈으므로 그 형태도 받아 준다.
 */
export function messageText(content: unknown): string {
  const blocks: unknown[] = Array.isArray(content) ? content : content ? [content] : [];
  const texts: string[] = [];
  for (const block of blocks) {
    if (!block || typeof block !== "object") continue;
    const b = block as { type?: unknown; text?: unknown };
    if (b.type === "text" && typeof b.text === "string") texts.push(b.text);
  }
  return texts.join("\n");
}

/**
 * 스트리밍 중인 툴 인자(불완전한 JSON 문자열)를 `ui/notifications/tool-input-partial`
 * 에 실을 객체로 만든다. 열린 문자열·배열·객체를 닫아서 파싱을 시도하고, 끝에 걸린
 * 콤마·콜론은 버린다. 못 만들면 null — 부분 알림은 생략해도 되는 것이라 그대로 넘긴다.
 *
 * 마지막 값이 잘린 채로 닫히면(`"loc": "Ne`) 그 값은 잘린 문자열로 나간다. 규격이
 * 부분 인자를 "incomplete, may change" 로 정의하므로 그 정도는 의도된 동작이다.
 */
export function parsePartialJson(text: string): Record<string, unknown> | null {
  const trimmed = text.trim();
  if (!trimmed.startsWith("{")) return null;

  const tryParse = (s: string): Record<string, unknown> | null => {
    try {
      const v = JSON.parse(s);
      return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
    } catch {
      return null;
    }
  };

  const whole = tryParse(trimmed);
  if (whole) return whole;

  // 열린 구조를 추적한다.
  const stack: string[] = [];
  let inString = false;
  let escaped = false;
  for (const ch of trimmed) {
    if (inString) {
      if (escaped) escaped = false;
      else if (ch === "\\") escaped = true;
      else if (ch === '"') inString = false;
      continue;
    }
    if (ch === '"') inString = true;
    else if (ch === "{") stack.push("}");
    else if (ch === "[") stack.push("]");
    else if (ch === "}" || ch === "]") stack.pop();
  }

  let repaired = trimmed;
  if (escaped) repaired = repaired.slice(0, -1);
  if (inString) repaired += '"';
  // 값이 오기 전에 끊긴 키(`"a":` / `"a"`)나 끝에 남은 콤마는 버린다.
  repaired = repaired.replace(/,\s*$/, "");
  repaired = repaired.replace(/("(?:[^"\\]|\\.)*")\s*:?\s*$/, (_m, key, offset: number) => {
    // 키 앞이 `{` 또는 `,` 이면 값 없는 키다. 아니면 값이므로 남긴다.
    const before = repaired.slice(0, offset).replace(/\s+$/, "");
    return before.endsWith("{") || before.endsWith(",") ? "" : key;
  });
  repaired = repaired.replace(/,\s*$/, "");
  repaired += stack.reverse().join("");

  return tryParse(repaired);
}
