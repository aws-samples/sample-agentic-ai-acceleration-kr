// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

import { useEffect, useState } from 'react';
import { useTheme } from 'next-themes';

/**
 * Soft-but-vivid categorical palette — 파스텔과 비비드 사이의 중간 채도.
 * 테두리 없이도 또렷이 구분되도록 충분히 채도를 주되 네온은 아님.
 * 틸/스카이/핑크 주축, hue 간격을 넓혀 인접 세그먼트도 구분. 라이트/다크
 * 카드 양쪽에서 가독성 확보. Chart.js 는 CSS 변수를 못 읽어 hex 직접.
 */
// 선/테두리 없이도 인접 세그먼트가 구분되도록 '이웃끼리 hue 가 최대한 다르게'
// 배치(teal→pink→amber→sky→… 교차). 같은 계열(teal/sky)이 붙지 않게 함.
export const CATEGORICAL_PALETTE = [
  '#2dd4bf', // teal-400   (주색)
  '#f472b6', // pink-400
  '#fbbf24', // amber-400
  '#38bdf8', // sky-400
  '#a78bfa', // violet-400
  '#4ade80', // green-400
  '#fb7185', // rose-400
  '#818cf8', // indigo-400
] as const;

/** 단색 강조(라인/단일 바)에 쓰는 주색 — 틸. */
export const PRIMARY_SERIES = CATEGORICAL_PALETTE[0];

export interface ChartTheme {
  /** 축 눈금/제목 텍스트 색 (foreground 기반) */
  text: string;
  /** 보조 텍스트 (muted-foreground) */
  textMuted: string;
  /** 그리드 선 색 (낮은 대비) */
  grid: string;
  /** 카드 표면색 — 도넛 세그먼트 사이 간격 등에 사용 */
  surface: string;
  /** 카테고리 팔레트 */
  palette: readonly string[];
  /** 다크 여부 */
  isDark: boolean;
}

/** 'H S% L%' (globals.css HSL 토큰) → 'hsl(H S% L%)' 문자열. 빈 값이면 fallback. */
function hslToken(varName: string, fallback: string): string {
  if (typeof window === 'undefined') return fallback;
  const raw = getComputedStyle(document.documentElement).getPropertyValue(varName).trim();
  return raw ? `hsl(${raw})` : fallback;
}

/**
 * 현재 테마에 맞는 Chart.js 색을 CSS 변수에서 해석해 반환.
 * resolvedTheme 가 바뀌면 다시 읽어 라이트/다크에 적응.
 *
 * 주의: Chart.js canvas 컨텍스트는 'hsl(var(--x))' 를 파싱 못 해 black 으로
 * 떨어진다(도넛 검은 테두리 버그의 원인). 반드시 여기서 실제 색 문자열로
 * 해석해 전달할 것.
 */
export function useChartTheme(): ChartTheme {
  const { resolvedTheme } = useTheme();
  const [theme, setThemeState] = useState<ChartTheme>(() => computeTheme(resolvedTheme === 'dark'));

  useEffect(() => {
    // 마운트/테마변경 후 실제 computed style 로 재계산 (SSR 기본값 보정).
    setThemeState(computeTheme(document.documentElement.classList.contains('dark')));
  }, [resolvedTheme]);

  return theme;
}

function computeTheme(isDark: boolean): ChartTheme {
  return {
    text: hslToken('--foreground', isDark ? '#f4f5f6' : '#0f1115'),
    textMuted: hslToken('--muted-foreground', isDark ? '#9a9fa8' : '#5e6673'),
    grid: isDark ? 'rgba(255,255,255,0.08)' : 'rgba(15,23,42,0.08)',
    surface: hslToken('--card', isDark ? '#121316' : '#ffffff'),
    palette: CATEGORICAL_PALETTE,
    isDark,
  };
}
