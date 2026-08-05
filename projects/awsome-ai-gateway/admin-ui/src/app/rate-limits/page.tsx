// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { getTranslations } from 'next-intl/server';
import { adminAPI } from '@/lib/api-client';
import type { RateLimitTreeNode } from '@/types/entities';
import { RateLimitTreeView } from '@/components/rate-limits/RateLimitTreeView';

export default async function RateLimitsPage() {
  const t = await getTranslations('rateLimits');
  const tree = await adminAPI
    .get<RateLimitTreeNode[]>('/admin/rate-limits/tree')
    .catch(() => [] as RateLimitTreeNode[]);

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">{t('title')}</h1>
      <RateLimitTreeView nodes={tree} />
    </div>
  );
}
