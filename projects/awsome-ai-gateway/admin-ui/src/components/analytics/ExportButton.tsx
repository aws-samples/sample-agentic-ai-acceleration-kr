'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useState, useRef, useEffect } from 'react';
import { Download, ChevronDown } from 'lucide-react';
import type { AnalyticsFilterForm } from '@/types/api';
import { resolveMonth } from '@/lib/utils/period';

interface ExportButtonProps {
  filter: AnalyticsFilterForm;
  /** 화면 차트와 동일하게 환산된 적용 월 (YYYY-MM). */
  latestMonth?: string;
}

type ExportFormat = 'csv' | 'json';

export function ExportButton({ filter, latestMonth }: ExportButtonProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  // 메뉴 외부 클릭 시 닫기
  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setIsOpen(false);
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  async function handleExport(format: ExportFormat) {
    setIsOpen(false);
    setIsLoading(true);

    try {
      // 화면 차트와 동일한 월(resolveMonth)을 보냄 — 상대기간/잘못된 period 가
      // 빈 CSV 를 만들지 않도록. custom 이면 start/end 도 함께 전달.
      const month = resolveMonth(filter, latestMonth);
      const exportParams: Record<string, string> = {
        period: month,
        group_by: filter.group_by,
        format,
      };
      if (filter.scope) exportParams.scope = filter.scope;
      if (filter.period === 'custom') {
        if (filter.start_date) exportParams.start_date = filter.start_date;
        if (filter.end_date) exportParams.end_date = filter.end_date;
      }
      const params = new URLSearchParams(exportParams).toString();

      const response = await fetch(`/api/analytics-export?${params}`);
      if (!response.ok) {
        throw new Error(`내보내기 실패: ${response.status}`);
      }

      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `analytics.${format}`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error('Export error:', err);
    } finally {
      setIsLoading(false);
    }
  }

  return (
    <div className="relative" ref={menuRef}>
      <button
        onClick={() => setIsOpen((prev) => !prev)}
        disabled={isLoading}
        aria-haspopup="true"
        aria-expanded={isOpen}
        className={[
          'inline-flex items-center gap-1.5 rounded-md px-3 py-2 text-sm font-medium transition-colors',
          'border border-border bg-background hover:bg-muted',
          'focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring',
          'disabled:pointer-events-none disabled:opacity-50',
        ].join(' ')}
      >
        <Download size={14} aria-hidden="true" />
        {isLoading ? '내보내는 중...' : '내보내기'}
        <ChevronDown
          size={14}
          aria-hidden="true"
          className={`transition-transform ${isOpen ? 'rotate-180' : ''}`}
        />
      </button>

      {isOpen && (
        <div
          role="menu"
          className="absolute right-0 mt-1 w-36 rounded-md border border-border bg-background shadow-md z-10"
        >
          {(['csv', 'json'] as ExportFormat[]).map((fmt) => (
            <button
              key={fmt}
              role="menuitem"
              onClick={() => handleExport(fmt)}
              className="block w-full px-4 py-2 text-left text-sm hover:bg-muted transition-colors first:rounded-t-md last:rounded-b-md"
            >
              {fmt.toUpperCase()}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}