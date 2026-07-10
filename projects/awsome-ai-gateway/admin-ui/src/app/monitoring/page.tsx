// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { Suspense } from 'react';
import { SkeletonCard } from '@/components/common/SkeletonCard';
import {
  fetchMonitoringOverview,
  fetchMonitoringModels,
  fetchMonitoringEvents,
  fetchMonitoringUsers,
} from '@/lib/actions/monitoring';
import { MonitoringOverview } from '@/components/monitoring/MonitoringOverview';
import { ModelHealthTable } from '@/components/monitoring/ModelHealthTable';
import { UserTopTable } from '@/components/monitoring/UserTopTable';
import { EventLog } from '@/components/monitoring/EventLog';
import { RegisterScreenContext } from '@/components/chat/RegisterScreenContext';

async function OverviewSection() {
  const data = await fetchMonitoringOverview();
  return (
    <>
      {/* 퀵챗 화면 컨텍스트 등록 — "지금 보는 모니터링 화면(최근 1시간 집계)".
          PII 없는 집계 수치만. 사용자가 "이 에러율 왜 높아?" 물으면 agent 가
          이 맥락 + query_db 로 답한다. */}
      <RegisterScreenContext
        page="실시간 모니터링"
        period="최근 1시간"
        data={{ last_1h: data.last_1h, active_models: data.active_models }}
      />
      <MonitoringOverview data={data} />
    </>
  );
}

async function ModelsSection() {
  const data = await fetchMonitoringModels();
  return <ModelHealthTable data={data} />;
}

async function UsersSection() {
  const data = await fetchMonitoringUsers(10);
  return <UserTopTable data={data} />;
}

async function EventsSection() {
  const data = await fetchMonitoringEvents();
  return <EventLog data={data} />;
}

export default async function MonitoringPage() {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">실시간 모니터링</h1>

      <Suspense fallback={<SkeletonCard count={6} />}>
        <OverviewSection />
      </Suspense>

      <Suspense fallback={<SkeletonCard count={1} />}>
        <ModelsSection />
      </Suspense>

      <Suspense fallback={<SkeletonCard count={1} />}>
        <UsersSection />
      </Suspense>

      <Suspense fallback={<SkeletonCard count={1} />}>
        <EventsSection />
      </Suspense>
    </div>
  );
}
