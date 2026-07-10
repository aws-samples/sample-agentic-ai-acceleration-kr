// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

/**
 * 공통 데이터 테이블 — shadcn 8-파트 합성 패턴을 우리 디자인 토큰으로 박아 통일(§60).
 *
 * 왜: 12개+ 테이블이 헤더(bg-muted vs uppercase tracking)·패딩(px-2/px-4)·보더가
 * 제각각이라 "짜임새 없다"는 인상의 원인이었다. 단일 정답 스타일을 컴포넌트 기본값으로
 * 박고, 변형은 소수 prop(numeric, density, clickable, selected)으로만 노출한다.
 *
 * 디자인 원칙: "헤더는 조용하게, 숫자는 또렷하게, 카드가 곧 컨테이너."
 *  - 외곽 보더 없음(.glass 카드가 담당) — 내부 가로 구분선만.
 *  - 헤더 = text-xs/font-medium/muted-foreground(본문보다 한 단계 가라앉힘).
 *  - 숫자 셀(numeric) = 우측정렬 + tabular-nums(.num) 자동 — 자릿수 세로 정렬.
 *  - 색은 토큰 위임 → 다크/라이트 자동 전환(순백 보더·텍스트 금지).
 *
 * 사용:
 *   <Table>
 *     <THead><Tr><Th>이름</Th><Th numeric>비용</Th></Tr></THead>
 *     <TBody>
 *       <Tr><Td>user00</Td><Td numeric>$27.98</Td></Tr>
 *     </TBody>
 *   </Table>
 */

import {
  forwardRef,
  createContext,
  useContext,
  type HTMLAttributes,
  type TdHTMLAttributes,
  type ThHTMLAttributes,
} from 'react';
import { cn } from '@/lib/utils/cn';

type Density = 'comfortable' | 'compact';
const DensityContext = createContext<Density>('comfortable');

interface TableProps extends HTMLAttributes<HTMLTableElement> {
  /** comfortable(기본, 행≈44px) | compact(행≈32px, 로그/와이드 테이블용). */
  density?: Density;
  /** 가로 스크롤 래퍼 className(드물게 조정). */
  wrapperClassName?: string;
}

export const Table = forwardRef<HTMLTableElement, TableProps>(
  ({ className, density = 'comfortable', wrapperClassName, ...props }, ref) => (
    <DensityContext.Provider value={density}>
      <div className={cn('relative w-full overflow-x-auto', wrapperClassName)}>
        <table ref={ref} className={cn('w-full caption-bottom text-sm', className)} {...props} />
      </div>
    </DensityContext.Provider>
  ),
);
Table.displayName = 'Table';

export const THead = forwardRef<HTMLTableSectionElement, HTMLAttributes<HTMLTableSectionElement>>(
  ({ className, ...props }, ref) => (
    <thead
      ref={ref}
      className={cn('[&_tr]:border-b [&_tr]:border-border', className)}
      {...props}
    />
  ),
);
THead.displayName = 'THead';

export const TBody = forwardRef<HTMLTableSectionElement, HTMLAttributes<HTMLTableSectionElement>>(
  ({ className, ...props }, ref) => (
    // 마지막 행 구분선 자동 제거(산발적 last:border-b-0 흡수).
    <tbody ref={ref} className={cn('[&_tr:last-child]:border-0', className)} {...props} />
  ),
);
TBody.displayName = 'TBody';

export const TFoot = forwardRef<HTMLTableSectionElement, HTMLAttributes<HTMLTableSectionElement>>(
  ({ className, ...props }, ref) => (
    // 합계행: 색칠 대신 상단보더+표면+굵기로 강조(Refactoring UI).
    <tfoot
      ref={ref}
      className={cn(
        'border-t border-border bg-muted/50 dark:bg-white/[0.04] font-semibold [&>tr]:border-0',
        className,
      )}
      {...props}
    />
  ),
);
TFoot.displayName = 'TFoot';

interface TrProps extends HTMLAttributes<HTMLTableRowElement> {
  /** 행 클릭→상세 등 인터랙티브 행. teal 틴트 hover + cursor-pointer. */
  clickable?: boolean;
  /** 선택 상태. */
  selected?: boolean;
}

export const Tr = forwardRef<HTMLTableRowElement, TrProps>(
  ({ className, clickable, selected, ...props }, ref) => (
    <tr
      ref={ref}
      data-state={selected ? 'selected' : undefined}
      className={cn(
        'border-b border-[--table-divider] transition-colors',
        clickable
          ? 'cursor-pointer hover:bg-primary/[0.06] dark:hover:bg-primary/[0.08]'
          : 'hover:bg-[--table-row-hover]',
        'data-[state=selected]:bg-muted dark:data-[state=selected]:bg-white/[0.07]',
        className,
      )}
      {...props}
    />
  ),
);
Tr.displayName = 'Tr';

interface ThProps extends ThHTMLAttributes<HTMLTableCellElement> {
  /** 숫자 컬럼 — 우측정렬(헤더-숫자 정렬축 일치). */
  numeric?: boolean;
}

export const Th = forwardRef<HTMLTableCellElement, ThProps>(
  ({ className, numeric, ...props }, ref) => (
    <th
      ref={ref}
      scope="col"
      className={cn(
        'h-10 px-3 align-middle text-xs font-medium text-muted-foreground whitespace-nowrap',
        numeric ? 'text-right' : 'text-left',
        className,
      )}
      {...props}
    />
  ),
);
Th.displayName = 'Th';

interface TdProps extends TdHTMLAttributes<HTMLTableCellElement> {
  /** 숫자 셀 — 우측정렬 + tabular-nums(자릿수 세로 정렬). */
  numeric?: boolean;
  /** 1차 식별 컬럼(이름/alias) 살짝 강조. */
  emphasis?: boolean;
}

export const Td = forwardRef<HTMLTableCellElement, TdProps>(
  ({ className, numeric, emphasis, ...props }, ref) => {
    const density = useContext(DensityContext);
    return (
      <td
        ref={ref}
        className={cn(
          'px-3 align-middle text-sm',
          density === 'compact' ? 'py-1.5' : 'py-2.5',
          numeric && 'text-right num',
          emphasis && 'font-medium',
          className,
        )}
        {...props}
      />
    );
  },
);
Td.displayName = 'Td';

/** 빈 상태 행 — colSpan 전체에 중앙 정렬 안내 문구. */
export function TEmpty({ colSpan, children }: { colSpan: number; children: React.ReactNode }) {
  return (
    <tr>
      <td colSpan={colSpan} className="px-3 py-12 text-center text-sm text-muted-foreground">
        {children}
      </td>
    </tr>
  );
}
