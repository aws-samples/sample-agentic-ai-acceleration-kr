'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import Link from 'next/link';
import { usePathname } from 'next/navigation';

/**
 * Tool Gateway 세그먼트 공용 레이아웃.
 * 사이드바는 /tools 단일 진입점만 링크하므로, 하위 세 페이지(카탈로그/메트릭/Trace)
 * 사이를 오가는 탭 스트립을 여기서 제공한다. Next.js 세그먼트 레이아웃이라
 * /tools · /tools/observability · /tools/traces 가 이 탭을 자동 공유한다.
 * 활성 탭은 usePathname 정확 매칭으로 표시(세 경로는 형제 leaf 라우트).
 */
const TABS = [
  { label: 'Tool 카탈로그', href: '/tools' },
  { label: '메트릭', href: '/tools/observability' },
  { label: 'Trace', href: '/tools/traces' },
] as const;

export default function ToolsLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();

  const tab = (active: boolean) =>
    [
      'pressable rounded-apple-sm px-3 py-1.5 text-sm font-medium transition-[background,color,box-shadow] duration-150',
      active
        ? 'bg-primary/10 text-primary font-semibold shadow-[inset_0_0_0_1px_hsl(var(--primary)/0.18)]'
        : 'text-muted-foreground interactive',
    ].join(' ');

  return (
    <div className="space-y-6">
      <nav
        role="tablist"
        aria-label="Tool Gateway 페이지"
        className="glass inline-flex items-center gap-0.5 rounded-apple-md p-1"
      >
        {TABS.map((t) => {
          const active = pathname === t.href;
          return (
            <Link
              key={t.href}
              href={t.href}
              role="tab"
              aria-selected={active}
              className={tab(active)}
            >
              {t.label}
            </Link>
          );
        })}
      </nav>
      {children}
    </div>
  );
}
