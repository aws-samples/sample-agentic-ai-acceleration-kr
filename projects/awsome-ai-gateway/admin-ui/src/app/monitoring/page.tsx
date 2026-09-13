// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { Suspense } from 'react';
import { getTranslations } from 'next-intl/server';
import { SkeletonCard } from '@/components/common/SkeletonCard';
import {
  fetchMonitoringOverview,
  fetchMonitoringModels,
  fetchMonitoringEvents,
  fetchMonitoringUsers,
} from '@/lib/actions/monitoring';
import { getBodyLoggingAction } from '@/lib/actions/settings';
import { BodyLoggingToggle } from '@/components/monitoring/BodyLoggingToggle';
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

async function BodyLoggingSection() {
  const result = await getBodyLoggingAction();
  // 읽기 실패 시 OFF 로 렌더한다 — 상태를 모를 때 "켜져 있다" 고 보여 주는 것이
  // 더 나쁘다(운영자가 수집되고 있다고 믿는다). 실제 상태는 백엔드가 갖고 있고,
  // 토글을 조작하면 그 응답으로 다시 맞춰진다.
  const enabled = result.success ? result.data.enabled : false;
  return <BodyLoggingToggle initialEnabled={enabled} />;
}

export default async function MonitoringPage() {
  const t = await getTranslations('monitoring');
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">{t('title')}</h1>

      {/* 본문 로깅 토글을 맨 위에 둔다 — 켜져 있으면 사용자 프롬프트가 durable
          저장소로 나가는 상태이므로, 이 화면을 열자마자 보여야 하는 정보다. */}
      <Suspense fallback={<SkeletonCard count={1} />}>
        <BodyLoggingSection />
      </Suspense>

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
