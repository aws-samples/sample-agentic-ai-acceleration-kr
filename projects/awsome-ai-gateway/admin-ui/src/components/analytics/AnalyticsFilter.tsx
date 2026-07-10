'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useRouter, usePathname } from 'next/navigation';
import type { AnalyticsFilterForm } from '@/types/api';
import type { GroupByType } from '@/types/enums';
import { currentCalendarMonth, monthsAgo, toKoreanMonthLabel } from '@/lib/utils/period';

interface AnalyticsFilterProps {
  defaultValue: AnalyticsFilterForm;
  /** 데이터가 있는 월 목록 (최신순). 월 선택기 옵션. */
  periods: string[];
  /** 현재 적용 중인 월 (YYYY-MM). custom 모드면 무시. */
  currentMonth: string;
}

const GROUP_BY_OPTIONS: { value: GroupByType; label: string }[] = [
  { value: 'model', label: '모델별' },
  { value: 'team', label: '팀별' },
  { value: 'user', label: '사용자별' },
];

export function AnalyticsFilter({ defaultValue, periods, currentMonth }: AnalyticsFilterProps) {
  const router = useRouter();
  const pathname = usePathname();
  const isCustom = defaultValue.period === 'custom';
  const thisMonth = currentCalendarMonth();
  const lastMonth = monthsAgo(1);

  function buildSearchParams(update: Partial<AnalyticsFilterForm>): string {
    const merged: AnalyticsFilterForm = { ...defaultValue, ...update };
    const entries = Object.entries(merged).filter(([, v]) => v != null && v !== '') as [
      string,
      string,
    ][];
    return new URLSearchParams(entries).toString();
  }

  // 월 선택 — period 에 YYYY-MM 을 직접 실어 보냄. custom 날짜는 비움.
  function handleMonthChange(month: string) {
    if (!isCustom && month === currentMonth) return;
    router.push(
      `${pathname}?${buildSearchParams({ period: month as never, start_date: null, end_date: null })}`,
    );
  }

  // 직접 입력(custom) 토글
  function enableCustom() {
    if (isCustom) return;
    router.push(`${pathname}?${buildSearchParams({ period: 'custom' })}`);
  }

  function handleGroupByChange(group_by: GroupByType) {
    router.push(`${pathname}?${buildSearchParams({ group_by })}`);
  }

  function handleStartDateChange(start_date: string) {
    router.push(`${pathname}?${buildSearchParams({ start_date })}`);
  }

  function handleEndDateChange(end_date: string) {
    router.push(`${pathname}?${buildSearchParams({ end_date })}`);
  }

  // 드롭다운 = 이번/지난 달 제외한 과거 데이터 월.
  const dropdownMonths = periods.filter((p) => p !== thisMonth && p !== lastMonth);
  const dropdownActive = !isCustom && currentMonth !== thisMonth && currentMonth !== lastMonth;

  const btn = (active: boolean) =>
    [
      'pressable rounded-apple-sm px-3 py-1.5 text-sm font-medium transition-[background,color,box-shadow] duration-150',
      active
        ? 'bg-primary/10 text-primary font-semibold shadow-[inset_0_0_0_1px_hsl(var(--primary)/0.18)]'
        : 'text-muted-foreground interactive',
    ].join(' ');

  return (
    <div className="flex flex-wrap items-end gap-4 glass rounded-apple p-4">
      {/* 기간 선택 — [이번 달][지난 달][기간선택 ▾][직접 입력] */}
      <div className="flex flex-col gap-1.5">
        <label className="text-xs font-medium text-muted-foreground">기간</label>
        <div className="glass inline-flex items-center gap-0.5 rounded-apple-md p-1">
          <button
            type="button"
            onClick={() => handleMonthChange(thisMonth)}
            aria-pressed={!isCustom && currentMonth === thisMonth}
            className={btn(!isCustom && currentMonth === thisMonth)}
          >
            이번 달
          </button>
          <button
            type="button"
            onClick={() => handleMonthChange(lastMonth)}
            aria-pressed={!isCustom && currentMonth === lastMonth}
            className={btn(!isCustom && currentMonth === lastMonth)}
          >
            지난 달
          </button>
          {dropdownMonths.length > 0 && (
            <select
              aria-label="기간 선택 (월)"
              value={dropdownActive ? currentMonth : ''}
              onChange={(e) => {
                if (e.target.value) handleMonthChange(e.target.value);
              }}
              className={[btn(dropdownActive), 'appearance-none bg-transparent pr-7 cursor-pointer bg-[length:14px] bg-no-repeat bg-[right_0.4rem_center]'].join(' ')}
              style={{
                backgroundImage:
                  "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'/%3E%3C/svg%3E\")",
              }}
            >
              <option value="" disabled>
                기간 선택
              </option>
              {dropdownMonths.map((p) => (
                <option key={p} value={p}>
                  {toKoreanMonthLabel(p)}
                </option>
              ))}
            </select>
          )}
          <button
            type="button"
            onClick={enableCustom}
            aria-pressed={isCustom}
            className={btn(isCustom)}
          >
            직접 입력
          </button>
        </div>
      </div>

      {/* 커스텀 날짜 범위 */}
      {isCustom && (
        <div className="flex items-end gap-2">
          <div className="flex flex-col gap-1.5">
            <label htmlFor="start_date" className="text-xs font-medium text-muted-foreground">
              시작일
            </label>
            <input
              id="start_date"
              type="date"
              defaultValue={defaultValue.start_date ?? ''}
              onChange={(e) => handleStartDateChange(e.target.value)}
              className="rounded-apple-md border border-input bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-ring"
            />
          </div>
          <span className="mb-2 text-muted-foreground">~</span>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="end_date" className="text-xs font-medium text-muted-foreground">
              종료일
            </label>
            <input
              id="end_date"
              type="date"
              defaultValue={defaultValue.end_date ?? ''}
              onChange={(e) => handleEndDateChange(e.target.value)}
              className="rounded-apple-md border border-input bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-ring"
            />
          </div>
        </div>
      )}

      {/* 그룹 기준 */}
      <div className="flex flex-col gap-1.5">
        <label htmlFor="group_by" className="text-xs font-medium text-muted-foreground">
          그룹 기준
        </label>
        <select
          id="group_by"
          defaultValue={defaultValue.group_by}
          onChange={(e) => handleGroupByChange(e.target.value as GroupByType)}
          className="rounded-apple-md border border-input bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-ring"
        >
          {GROUP_BY_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}
