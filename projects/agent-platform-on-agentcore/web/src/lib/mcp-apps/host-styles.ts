/**
 * 호스트 디자인 토큰 → 규격 `hostContext.styles.variables` 매핑.
 *
 * MCP Apps 규격(SEP-1865)은 호스트가 `--color-background-primary`, `--font-sans`
 * 같은 정해진 이름의 CSS 변수를 View에 내려주도록 한다. 앱은 그 변수로 호스트
 * 테마에 녹아든다(SDK의 `applyHostStyleVariables`). 이 파일은 `globals.css`의
 * 시맨틱 토큰(`--background`, `--border`, …)을 그 이름들로 옮긴다.
 *
 * 토큰 형식: 저장소 토큰은 Tailwind `hsl(var(--x) / <alpha>)`를 위해 맨 HSL
 * 삼중항("211 24% 91%")으로 정의돼 있다. iframe 안의 앱은 그 관례를 모르므로 여기서
 * `hsl(...)`로 감싼 완성된 색을 보낸다. 크기는 px로 보낸다 — rem은 View 문서의
 * 루트 글꼴 크기에 좌우되고, 호스트는 그것을 통제하지 못한다.
 */
import type { McpUiStyles } from "@modelcontextprotocol/ext-apps/app-bridge";

/** 이름으로 CSS 변수 원문을 돌려준다. 정의돼 있지 않으면 빈 문자열. */
export type TokenResolver = (name: string) => string;

export type HostFontFallbacks = {
  /** 본문의 계산된 font-family. `--font-sans`가 비어 있을 때 쓴다. */
  sans?: string;
  mono?: string;
};

const HSL_TRIPLE = /^-?[\d.]+(deg)?\s+[\d.]+%\s+[\d.]+%$/;

/**
 * 토큰 원문을 앱이 그대로 쓸 수 있는 색으로 만든다.
 * HSL 삼중항은 `hsl()`로 감싸고, 이미 색 함수·키워드·hex면 그대로 둔다.
 */
export function asColor(raw: string | undefined): string | undefined {
  const value = raw?.trim();
  if (!value) return undefined;
  return HSL_TRIPLE.test(value) ? `hsl(${value})` : value;
}

/** "0.5rem" / "8px" / "8" 을 px 숫자로. 못 읽으면 undefined. */
export function toPx(raw: string | undefined, remBase = 16): number | undefined {
  const value = raw?.trim();
  if (!value) return undefined;
  const match = /^(-?[\d.]+)(rem|px)?$/.exec(value);
  if (!match) return undefined;
  const n = Number(match[1]);
  if (!Number.isFinite(n)) return undefined;
  return match[2] === "rem" ? n * remBase : n;
}

const px = (n: number) => `${Math.round(n * 100) / 100}px`;

/**
 * 규격 변수 전부를 채운 객체를 만든다. 정의되지 않은 토큰은 `undefined`로 남겨
 * SDK가 건너뛰게 한다(있는 값만 `setProperty` 한다).
 */
export function buildHostStyles(
  resolve: TokenResolver,
  fonts: HostFontFallbacks = {},
): McpUiStyles {
  const color = (name: string) => asColor(resolve(name));

  // 반경: 저장소의 Tailwind 매핑(lg = --radius, md = lg-2px, sm = lg-4px)을 따른다.
  const radiusLg = toPx(resolve("--radius")) ?? 8;

  // 그림자: tailwind.config의 값. 색은 neutral-950 위에 알파를 얹은 것이라
  // 여기서 완성된 hsl()로 푼다.
  const shadowBase = resolve("--neutral-950").trim() || "220 7% 4%";
  const shade = (alpha: number) => `hsl(${shadowBase} / ${alpha})`;

  const sans = resolve("--font-sans").trim() || fonts.sans || undefined;
  const mono = resolve("--font-mono").trim() || fonts.mono || undefined;

  return {
    // 배경. primary는 페이지, secondary는 카드(한 단계 위), tertiary는 muted.
    "--color-background-primary": color("--background"),
    "--color-background-secondary": color("--card"),
    "--color-background-tertiary": color("--muted"),
    "--color-background-inverse": color("--foreground"),
    "--color-background-ghost": "transparent",
    "--color-background-info": color("--info"),
    "--color-background-danger": color("--destructive"),
    "--color-background-success": color("--success"),
    "--color-background-warning": color("--warning"),
    "--color-background-disabled": color("--muted"),

    // 글자. `--*-foreground`는 "그 배경 위의 글자색"이지 "그 색의 글자"가 아니므로
    // 상태색 글자에는 상태색 자체를 쓴다.
    "--color-text-primary": color("--foreground"),
    "--color-text-secondary": color("--muted-foreground"),
    "--color-text-tertiary": color("--muted-foreground"),
    "--color-text-inverse": color("--background"),
    "--color-text-ghost": color("--muted-foreground"),
    "--color-text-info": color("--info"),
    "--color-text-danger": color("--destructive"),
    "--color-text-success": color("--success"),
    "--color-text-warning": color("--warning"),
    "--color-text-disabled": color("--muted-foreground"),

    "--color-border-primary": color("--border"),
    "--color-border-secondary": color("--border-strong"),
    "--color-border-tertiary": color("--input"),
    "--color-border-inverse": color("--foreground"),
    "--color-border-ghost": "transparent",
    "--color-border-info": color("--info"),
    "--color-border-danger": color("--destructive"),
    "--color-border-success": color("--success"),
    "--color-border-warning": color("--warning"),
    "--color-border-disabled": color("--border"),

    "--color-ring-primary": color("--ring"),
    "--color-ring-secondary": color("--border-strong"),
    "--color-ring-inverse": color("--foreground"),
    "--color-ring-info": color("--info"),
    "--color-ring-danger": color("--destructive"),
    "--color-ring-success": color("--success"),
    "--color-ring-warning": color("--warning"),

    "--font-sans": sans,
    "--font-mono": mono,
    "--font-weight-normal": "400",
    "--font-weight-medium": "500",
    "--font-weight-semibold": "600",
    "--font-weight-bold": "700",

    // tailwind.config의 fontSize 스케일(xs/sm/base/lg → text, lg/xl/2xl/3xl… → heading).
    "--font-text-xs-size": px(12),
    "--font-text-sm-size": px(13),
    "--font-text-md-size": px(15),
    "--font-text-lg-size": px(17),
    "--font-heading-xs-size": px(17),
    "--font-heading-sm-size": px(20),
    "--font-heading-md-size": px(24),
    "--font-heading-lg-size": px(30),
    "--font-heading-xl-size": px(36),
    "--font-heading-2xl-size": px(48),
    "--font-heading-3xl-size": px(60),
    "--font-text-xs-line-height": px(17),
    "--font-text-sm-line-height": px(19),
    "--font-text-md-line-height": px(23.2),
    "--font-text-lg-line-height": px(24),
    "--font-heading-xs-line-height": px(24),
    "--font-heading-sm-line-height": px(28),
    "--font-heading-md-line-height": px(32),
    "--font-heading-lg-line-height": px(36),
    "--font-heading-xl-line-height": px(40),
    "--font-heading-2xl-line-height": px(48),
    "--font-heading-3xl-line-height": px(60),

    "--border-radius-xs": px(3),
    "--border-radius-sm": px(Math.max(radiusLg - 4, 0)),
    "--border-radius-md": px(Math.max(radiusLg - 2, 0)),
    "--border-radius-lg": px(radiusLg),
    "--border-radius-xl": px(radiusLg + 4),
    "--border-radius-full": px(9999),
    "--border-width-regular": px(1),

    "--shadow-hairline": `0 0 0 1px ${shade(0.06)}`,
    "--shadow-sm": `0 1px 2px 0 ${shade(0.05)}`,
    "--shadow-md": `0 2px 4px -1px ${shade(0.06)}, 0 1px 2px -1px ${shade(0.04)}`,
    "--shadow-lg": `0 8px 24px -6px ${shade(0.12)}, 0 2px 6px -2px ${shade(0.06)}`,
  };
}

/**
 * 현재 문서에서 토큰을 읽어 규격 변수로 만든다.
 *
 * `body`의 계산 스타일에서 읽는다: 토큰은 `:root`/`.dark`에 정의되고 CSS 변수는
 * 상속되므로 body에서 전부 보이고, next/font가 붙이는 `--font-*`도 함께 잡힌다.
 * 테마가 `.dark` 클래스로 바뀌면 값이 바뀌므로 매번 다시 읽어야 한다.
 */
export function readHostStyles(doc: Document = document): McpUiStyles {
  const computed = doc.defaultView?.getComputedStyle(doc.body);
  if (!computed) return buildHostStyles(() => "");
  return buildHostStyles((name) => computed.getPropertyValue(name), {
    sans: computed.fontFamily || undefined,
  });
}
