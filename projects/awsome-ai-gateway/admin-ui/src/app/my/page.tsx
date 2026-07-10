// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { Suspense } from 'react';
import { SkeletonCard } from '@/components/common/SkeletonCard';
import { fetchMyBudget, fetchMyUsage } from '@/lib/actions/my';
import { MyBudgetCard } from '@/components/my/MyBudgetCard';
import { MyUsageDashboard } from '@/components/my/MyUsageDashboard';

async function BudgetSection() {
  const data = await fetchMyBudget();
  return <MyBudgetCard data={data} />;
}

async function UsageSection({ period }: { period?: string }) {
  const data = await fetchMyUsage(period);
  return <MyUsageDashboard data={data} />;
}

interface MyPageProps {
  searchParams: { period?: string };
}

export default async function MyPage({ searchParams }: MyPageProps) {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">내 사용량</h1>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-1">
          <Suspense fallback={<SkeletonCard count={1} />}>
            <BudgetSection />
          </Suspense>
        </div>
        <div className="lg:col-span-2">
          <Suspense fallback={<SkeletonCard count={3} />}>
            <UsageSection period={searchParams.period} />
          </Suspense>
        </div>
      </div>
    </div>
  );
}
