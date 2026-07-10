// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { adminAPI } from '@/lib/api-client';
import Link from 'next/link';
import { Suspense } from 'react';
import { SkeletonTable } from '@/components/common/SkeletonTable';
import { KeysTable } from '@/components/keys/KeysTable';
import type { VirtualKeyListItem } from '@/types/entities';

interface CursorPaginationMeta {
  cursor: string | null;
  limit: number;
  has_more: boolean;
}

interface KeyListResponse {
  items: VirtualKeyListItem[];
  pagination: CursorPaginationMeta;
}

const PAGE_LIMIT = 50;

interface KeysPageProps {
  searchParams?: { email?: string };
}

export default async function KeysPage({ searchParams }: KeysPageProps) {
  const email = searchParams?.email?.trim() ?? '';
  const listQuery: Record<string, string | number> = { limit: PAGE_LIMIT };
  if (email) listQuery.email = email;

  const keysData = await adminAPI
    .get<KeyListResponse>('/admin/keys', listQuery)
    .catch(() => ({
      items: [] as VirtualKeyListItem[],
      pagination: { cursor: null, limit: PAGE_LIMIT, has_more: false },
    }));

  return (
    <div>
      <div className="mb-6">
        <h1 className="text-2xl font-bold">API Keys</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          VK 는 Cognito 로그인 후 gateway-cli 로만 발급됩니다. 이 페이지에서는 조회 / 해지만 가능합니다.
        </p>
      </div>

      <form method="GET" className="flex items-center gap-2">
        <input
          type="search"
          name="email"
          defaultValue={email}
          placeholder="사용자 이메일로 검색 (부분 일치)"
          className="w-80 rounded-md border border-border bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        />
        <button
          type="submit"
          className="inline-flex items-center rounded-md border border-border bg-background px-3 py-2 text-sm font-medium text-foreground hover:bg-accent hover:text-accent-foreground transition-colors"
        >
          검색
        </button>
        {email && (
          <Link
            href="/keys"
            className="text-sm text-muted-foreground hover:text-foreground underline-offset-4 hover:underline"
          >
            초기화
          </Link>
        )}
      </form>

      <Suspense fallback={<SkeletonTable rows={10} columns={6} />}>
        <div className="mt-4">
          <KeysTable keys={keysData.items} />
          {keysData.pagination.has_more && (
            <p className="mt-4 text-sm text-muted-foreground text-center">
              더 많은 키가 있습니다. 검색어를 사용해 범위를 좁혀주세요.
            </p>
          )}
        </div>
      </Suspense>
    </div>
  );
}
