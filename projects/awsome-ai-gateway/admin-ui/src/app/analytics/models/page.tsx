// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { Suspense } from 'react';
import { SkeletonCard } from '@/components/common/SkeletonCard';
import { fetchModelCostAnalytics } from '@/lib/actions/analytics-models';
import { ModelCostDetail } from '@/components/analytics/ModelCostDetail';

interface ModelCostPageProps {
  searchParams: { period?: string };
}

async function ModelCostSection({ period }: { period?: string }) {
  const data = await fetchModelCostAnalytics(period);
  return <ModelCostDetail data={data} />;
}

export default async function ModelCostPage({ searchParams }: ModelCostPageProps) {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">모델별 비용 추적</h1>

      <Suspense fallback={<SkeletonCard count={3} />}>
        <ModelCostSection period={searchParams.period} />
      </Suspense>
    </div>
  );
}
