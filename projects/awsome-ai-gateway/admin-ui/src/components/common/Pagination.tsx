'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { ChevronLeft, ChevronRight } from 'lucide-react';

interface PaginationProps {
  currentPage: number;
  totalPages: number;
  basePath: string; // e.g. '/keys' — page query param is appended as ?page=N
}

function buildHref(basePath: string, page: number): string {
  const separator = basePath.includes('?') ? '&' : '?';
  return `${basePath}${separator}page=${page}`;
}

function getPageRange(current: number, total: number, maxVisible = 5): number[] {
  if (total <= maxVisible) {
    return Array.from({ length: total }, (_, i) => i + 1);
  }

  const half = Math.floor(maxVisible / 2);
  let start = current - half;
  let end = current + half;

  if (start < 1) {
    start = 1;
    end = maxVisible;
  }
  if (end > total) {
    end = total;
    start = total - maxVisible + 1;
  }

  return Array.from({ length: end - start + 1 }, (_, i) => start + i);
}

export function Pagination({ currentPage, totalPages, basePath }: PaginationProps) {
  const t = useTranslations('common.pagination');

  if (totalPages <= 1) return null;

  const pages = getPageRange(currentPage, totalPages);
  const hasPrev = currentPage > 1;
  const hasNext = currentPage < totalPages;

  const buttonBase =
    'inline-flex items-center justify-center rounded-md text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring h-9 min-w-9 px-2';
  const activeClass = 'bg-primary text-primary-foreground shadow';
  const inactiveClass = 'bg-background text-foreground border border-border hover:bg-accent hover:text-accent-foreground';
  const disabledClass = 'opacity-40 pointer-events-none border border-border bg-background text-muted-foreground';

  return (
    <nav
      aria-label={t('nav')}
      className="flex items-center justify-center gap-1 mt-4"
    >
      {/* Previous */}
      {hasPrev ? (
        <Link
          href={buildHref(basePath, currentPage - 1)}
          className={`${buttonBase} ${inactiveClass}`}
          aria-label={t('previous')}
        >
          <ChevronLeft size={16} aria-hidden="true" />
        </Link>
      ) : (
        <span className={`${buttonBase} ${disabledClass}`} aria-disabled="true" aria-label={t('previousNone')}>
          <ChevronLeft size={16} aria-hidden="true" />
        </span>
      )}

      {/* First page + ellipsis */}
      {pages[0] > 1 && (
        <>
          <Link href={buildHref(basePath, 1)} className={`${buttonBase} ${inactiveClass}`} aria-label={t('gotoPage', { page: 1 })}>
            1
          </Link>
          {pages[0] > 2 && (
            <span className={`${buttonBase} text-muted-foreground`} aria-hidden="true">
              …
            </span>
          )}
        </>
      )}

      {/* Page numbers */}
      {pages.map((page) => (
        <Link
          key={page}
          href={buildHref(basePath, page)}
          className={`${buttonBase} ${page === currentPage ? activeClass : inactiveClass}`}
          aria-label={t('gotoPage', { page })}
          aria-current={page === currentPage ? 'page' : undefined}
        >
          {page}
        </Link>
      ))}

      {/* Last page + ellipsis */}
      {pages[pages.length - 1] < totalPages && (
        <>
          {pages[pages.length - 1] < totalPages - 1 && (
            <span className={`${buttonBase} text-muted-foreground`} aria-hidden="true">
              …
            </span>
          )}
          <Link href={buildHref(basePath, totalPages)} className={`${buttonBase} ${inactiveClass}`} aria-label={t('gotoPage', { page: totalPages })}>
            {totalPages}
          </Link>
        </>
      )}

      {/* Next */}
      {hasNext ? (
        <Link
          href={buildHref(basePath, currentPage + 1)}
          className={`${buttonBase} ${inactiveClass}`}
          aria-label={t('next')}
        >
          <ChevronRight size={16} aria-hidden="true" />
        </Link>
      ) : (
        <span className={`${buttonBase} ${disabledClass}`} aria-disabled="true" aria-label={t('nextNone')}>
          <ChevronRight size={16} aria-hidden="true" />
        </span>
      )}
    </nav>
  );
}
