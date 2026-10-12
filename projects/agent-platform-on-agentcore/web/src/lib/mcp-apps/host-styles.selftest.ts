/**
 * hostContext.styles 매핑 자체 검증.
 *
 * web에는 테스트 러너가 없으므로 노드로 직접 실행한다:
 *   cd web && npx tsx src/lib/mcp-apps/host-styles.selftest.ts
 *
 * 실패하면 0이 아닌 코드로 종료한다.
 */
import assert from "node:assert/strict";
import { asColor, buildHostStyles, toPx } from "./host-styles";

const tokens: Record<string, string> = {
  "--background": " 220 3% 96% ",
  "--card": "0 0% 100%",
  "--foreground": "220 5% 10%",
  "--border": "220 3% 90%",
  "--destructive": "4 42% 44%",
  "--radius": "0.5rem",
  "--neutral-950": "220 7% 4%",
  "--font-mono": "'JetBrains Mono', 'JetBrains Mono Fallback'",
};
const resolve = (name: string) => tokens[name] ?? "";

const checks: Array<[string, () => void]> = [
  ["HSL 삼중항은 hsl()로 감싼다", () => {
    // 저장소 토큰은 Tailwind용 맨 삼중항이다. iframe 안의 앱은 그 관례를 모른다.
    assert.equal(asColor("211 24% 91%"), "hsl(211 24% 91%)");
    assert.equal(asColor(" 220 3% 96% "), "hsl(220 3% 96%)");
  }],
  ["완성된 색은 그대로 둔다", () => {
    assert.equal(asColor("#3F5266"), "#3F5266");
    assert.equal(asColor("transparent"), "transparent");
    assert.equal(asColor("hsl(211 24% 91%)"), "hsl(211 24% 91%)");
    assert.equal(asColor("rgb(1 2 3 / 50%)"), "rgb(1 2 3 / 50%)");
  }],
  ["빈 토큰은 undefined — SDK가 건너뛴다", () => {
    assert.equal(asColor(""), undefined);
    assert.equal(asColor(undefined), undefined);
    assert.equal(asColor("   "), undefined);
  }],
  ["rem은 16px 기준으로, px는 그대로", () => {
    assert.equal(toPx("0.5rem"), 8);
    assert.equal(toPx("12px"), 12);
    assert.equal(toPx("3"), 3);
    assert.equal(toPx("calc(1rem - 2px)"), undefined);
    assert.equal(toPx(""), undefined);
  }],
  ["시맨틱 토큰이 규격 이름으로 옮겨진다", () => {
    const s = buildHostStyles(resolve);
    assert.equal(s["--color-background-primary"], "hsl(220 3% 96%)");
    assert.equal(s["--color-background-secondary"], "hsl(0 0% 100%)");
    assert.equal(s["--color-text-primary"], "hsl(220 5% 10%)");
    assert.equal(s["--color-border-primary"], "hsl(220 3% 90%)");
    // 상태색 글자는 상태색 자체다. `--destructive-foreground`(흰색)가 아니다.
    assert.equal(s["--color-text-danger"], "hsl(4 42% 44%)");
    assert.equal(s["--color-background-ghost"], "transparent");
  }],
  ["정의되지 않은 토큰은 undefined로 남는다", () => {
    const s = buildHostStyles(resolve);
    assert.equal(s["--color-background-info"], undefined);
    assert.equal(s["--color-ring-primary"], undefined);
  }],
  ["반경은 --radius에서 Tailwind 매핑대로 파생된다", () => {
    const s = buildHostStyles(resolve);
    assert.equal(s["--border-radius-lg"], "8px");
    assert.equal(s["--border-radius-md"], "6px");
    assert.equal(s["--border-radius-sm"], "4px");
    assert.equal(s["--border-radius-xl"], "12px");
    // --radius가 없으면 8px 기본값.
    assert.equal(buildHostStyles(() => "")["--border-radius-lg"], "8px");
  }],
  ["글꼴: --font-* 우선, 없으면 계산된 font-family", () => {
    const s = buildHostStyles(resolve, { sans: "'IBM Plex Sans KR', system-ui" });
    assert.equal(s["--font-sans"], "'IBM Plex Sans KR', system-ui");
    assert.equal(s["--font-mono"], "'JetBrains Mono', 'JetBrains Mono Fallback'");
    assert.equal(buildHostStyles(() => "")["--font-sans"], undefined);
  }],
  ["그림자 색은 neutral-950 위에 알파를 얹은 완성형이다", () => {
    const s = buildHostStyles(resolve);
    assert.equal(s["--shadow-sm"], "0 1px 2px 0 hsl(220 7% 4% / 0.05)");
  }],
  ["크기는 전부 px 문자열이다", () => {
    const s = buildHostStyles(resolve);
    for (const [k, v] of Object.entries(s)) {
      if (/^--font-(text|heading)-|^--border-/.test(k)) {
        assert.match(String(v), /^\d+(\.\d+)?px$/, `${k} = ${v}`);
      }
    }
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
