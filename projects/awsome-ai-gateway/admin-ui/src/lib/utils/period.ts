// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import type { AnalyticsFilterForm } from '@/types/api';

/** 현재 달력월 'YYYY-MM' (로컬 시간 기준). */
export function currentCalendarMonth(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
}

/** N개월 전 달 'YYYY-MM' (0=이번 달, 1=지난 달). 로컬 시간 기준. */
export function monthsAgo(n: number): string {
  const now = new Date();
  const d = new Date(now.getFullYear(), now.getMonth() - n, 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
}

/** 문자열이 'YYYY-MM' 월 형식인지. */
export function isMonth(value: string | null | undefined): value is string {
  return !!value && /^\d{4}-\d{2}$/.test(value);
}

/** 'YYYY-MM' → 'YYYY년 M월'. 순수 문자열 split (new Date 금지 — TZ drift). */
export function toKoreanMonthLabel(p: string): string {
  const [y, m] = p.split('-');
  return `${y}년 ${Number(m)}월`;
}

/**
 * 절대 월을 상대 레이블로. 이번 달/지난 달이면 그 레이블, 아니면 'YYYY년 M월'.
 * 시간이 흘러도(7월·8월…) 안정적인 표기.
 */
export function relativeMonthLabel(month: string): string {
  if (month === currentCalendarMonth()) return '이번 달';
  if (month === monthsAgo(1)) return '지난 달';
  return toKoreanMonthLabel(month);
}

/**
 * analytics 필터를 백엔드가 기대하는 단일 월(YYYY-MM)로 환산.
 *
 * 백엔드 /admin/analytics 는 to_char(requested_at,'YYYY-MM')==period 로
 * 단일 월만 필터한다. 필터의 7d/30d/90d 상대 기간은 월 단위로 환산되며,
 * custom 이 아닐 때 "현재 달력월" 대신 latestWithData(데이터 있는 최근 월)를
 * 기본으로 써서 월이 바뀌어도 빈 화면을 피한다.
 *
 * @param filter         analytics 필터 폼
 * @param latestWithData 데이터가 있는 가장 최근 월. 미지정 시 현재 달력월.
 */
export function resolveMonth(filter: AnalyticsFilterForm, latestWithData?: string): string {
  // 명시적 YYYY-MM 월이 흘러들어오면 존중(월 선택기 경로).
  if (isMonth(filter.period as unknown as string)) {
    return filter.period as unknown as string;
  }
  if (filter.period === 'custom' && filter.start_date) {
    return filter.start_date.slice(0, 7);
  }
  return latestWithData ?? currentCalendarMonth();
}
