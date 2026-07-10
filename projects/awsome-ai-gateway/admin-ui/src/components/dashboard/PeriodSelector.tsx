'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useRouter, usePathname, useSearchParams } from 'next/navigation';
import {
  currentCalendarMonth,
  monthsAgo,
  toKoreanMonthLabel,
} from '@/lib/utils/period';

interface PeriodSelectorProps {
  periods: string[]; // 선택 가능한 월 (YYYY-MM), 최신순
  current: string;
}

/**
 * 기간 선택기 — 월이 계속 쌓여도 안 깨지는 상대 구조.
 *   [이번 달] [지난 달] [기간 선택 ▾]
 * 이번 달/지난 달은 항상 고정 버튼, 그 외 월은 드롭다운에서 선택.
 * 선택은 ?period=YYYY-MM URL 쿼리로 구동되어 서버 컴포넌트가 리렌더된다.
 */
export function PeriodSelector({ periods, current }: PeriodSelectorProps) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const thisMonth = currentCalendarMonth();
  const lastMonth = monthsAgo(1);

  function go(period: string) {
    if (period === current) return;
    // 기존 쿼리(예: ?client=)를 보존한 채 period 만 교체 — 월 변경 시 client 필터가
    // 풀리지 않도록.
    const sp = new URLSearchParams(searchParams.toString());
    sp.set("period", period);
    router.push(`${pathname}?${sp.toString()}`);
  }

  if (periods.length === 0) {
    return <span className="text-sm text-muted-foreground">표시할 기간이 없습니다</span>;
  }

  // 드롭다운 옵션 = 이번 달/지난 달을 제외한 나머지 월(보통 과거 데이터 월).
  // 이번/지난 달에 데이터가 없어도 버튼은 항상 보이므로 여기선 제외.
  const dropdownMonths = periods.filter((p) => p !== thisMonth && p !== lastMonth);

  // current 가 이번/지난 달 중 어느 것도 아니면 드롭다운이 활성(특정 과거 월 선택 상태).
  const dropdownActive = current !== thisMonth && current !== lastMonth;

  const btn = (active: boolean) =>
    [
      'pressable rounded-apple-sm px-3 py-1.5 text-sm font-medium transition-[background,color,box-shadow] duration-150',
      active
        ? 'bg-primary/10 text-primary font-semibold shadow-[inset_0_0_0_1px_hsl(var(--primary)/0.18)]'
        : 'text-muted-foreground interactive',
    ].join(' ');

  return (
    <div
      role="group"
      aria-label="기간 선택"
      className="glass inline-flex items-center gap-0.5 rounded-apple-md p-1"
    >
      <button
        type="button"
        onClick={() => go(thisMonth)}
        aria-pressed={current === thisMonth}
        className={btn(current === thisMonth)}
      >
        이번 달
      </button>
      <button
        type="button"
        onClick={() => go(lastMonth)}
        aria-pressed={current === lastMonth}
        className={btn(current === lastMonth)}
      >
        지난 달
      </button>

      {dropdownMonths.length > 0 && (
        <div className="relative">
          <select
            aria-label="기간 선택 (월)"
            value={dropdownActive ? current : ''}
            onChange={(e) => {
              if (e.target.value) go(e.target.value);
            }}
            className={[
              btn(dropdownActive),
              'appearance-none bg-transparent pr-7 cursor-pointer',
              // 드롭다운 화살표
              'bg-[length:14px] bg-no-repeat bg-[right_0.4rem_center]',
            ].join(' ')}
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
        </div>
      )}
    </div>
  );
}
