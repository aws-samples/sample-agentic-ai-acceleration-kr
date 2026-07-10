'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useRouter } from 'next/navigation';
import { RefreshCw } from 'lucide-react';

export function RefreshButton() {
  const router = useRouter();

  return (
    <button
      onClick={() => router.refresh()}
      className={[
        'inline-flex items-center gap-1.5 rounded-md px-3 py-2 text-sm font-medium transition-colors',
        'border border-border bg-background hover:bg-muted',
        'focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring',
      ].join(' ')}
      aria-label="페이지 새로고침"
    >
      <RefreshCw size={14} aria-hidden="true" />
      새로고침
    </button>
  );
}