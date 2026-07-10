'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useRouter, usePathname, useSearchParams } from 'next/navigation';

interface ClientFilterProps {
  current: string; // 'all' | 'claude-code' | 'cowork' | 'codex' | 'other'
}

const OPTIONS: Array<{ value: string; label: string }> = [
  { value: 'all', label: '전체' },
  { value: 'claude-code', label: 'Claude Code' },
  { value: 'cowork', label: 'Cowork' },
  { value: 'codex', label: 'Codex' },
  { value: 'other', label: '기타' },
];

/**
 * 앱(client) 필터 — ?client=all|claude-code|cowork|codex|other URL 쿼리로 구동.
 * period 등 기존 쿼리는 보존한 채 client 만 교체한다.
 */
export function ClientFilter({ current }: ClientFilterProps) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  function go(client: string) {
    if (client === current) return;
    const sp = new URLSearchParams(searchParams.toString());
    if (client === 'all') sp.delete('client');
    else sp.set('client', client);
    router.push(`${pathname}?${sp.toString()}`);
  }

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
      aria-label="앱 필터"
      className="glass inline-flex items-center gap-0.5 rounded-apple-md p-1"
    >
      {OPTIONS.map((o) => (
        <button
          key={o.value}
          type="button"
          onClick={() => go(o.value)}
          aria-pressed={current === o.value}
          className={btn(current === o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}
