// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

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
