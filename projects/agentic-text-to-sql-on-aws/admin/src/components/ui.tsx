// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

/** 화면 간 공유하는 최소 프리미티브 (외부 UI 킷 미사용). */

import { STATUS_LABEL } from '@/lib/types';

/** 오류/성공/안내 알림. message 가 없으면 렌더하지 않는다. */
export function Alert({
  kind,
  message,
}: {
  kind: 'error' | 'ok' | 'info';
  message?: string | null;
}) {
  if (!message) return null;
  return <div className={`adm-alert adm-alert-${kind}`}>{message}</div>;
}

/** candidate/published 상태 배지. */
export function StatusBadge({ status }: { status?: string }) {
  const label = status ? (STATUS_LABEL[status] ?? status) : '-';
  const modifier =
    status === 'published'
      ? 'adm-badge-published'
      : status === 'candidate'
        ? 'adm-badge-candidate'
        : '';
  return <span className={`adm-badge ${modifier}`}>{label}</span>;
}

/** 섹션 제목 + 설명 래퍼. */
export function Section({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="adm-section">
      <h2>{title}</h2>
      {description ? <p className="adm-desc">{description}</p> : null}
      {children}
    </section>
  );
}

/** 목록이 비었을 때의 안내 행. */
export function EmptyState({ children }: { children: React.ReactNode }) {
  return <div className="adm-empty">{children}</div>;
}

/** ISO 타임스탬프를 로컬 표기로 (빈 값은 '-'). */
export function formatTime(iso?: string): string {
  if (!iso) return '-';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString('ko-KR', { hour12: false });
}
