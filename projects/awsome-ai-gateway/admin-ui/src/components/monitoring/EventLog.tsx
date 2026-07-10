'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useState, useTransition } from 'react';
import {
  fetchMonitoringEvents,
  type MonitoringEventsResponse,
  type MonitoringEventTypeFilter,
} from '@/lib/actions/monitoring';
import type { BadgeTone } from '@/components/common/Badge';
import { Table, THead, TBody, Tr, Th, Td } from '@/components/common/Table';

function eventTone(type: string): BadgeTone {
  switch (type) {
    case 'ERROR':
      return 'pink';
    case 'TIMEOUT':
    case 'SLOW_REQUEST':
      return 'amber';
    case 'SUCCESS':
      return 'teal';
    default:
      return 'neutral';
  }
}

const EVENT_LABELS: Record<string, string> = {
  ERROR: '에러',
  TIMEOUT: '타임아웃',
  SLOW_REQUEST: '지연',
  SUCCESS: '정상',
};

const FILTER_OPTIONS: { value: MonitoringEventTypeFilter; label: string }[] = [
  { value: 'all', label: '전체' },
  { value: 'success', label: '정상' },
  { value: 'error', label: '에러' },
  { value: 'timeout', label: '타임아웃' },
  { value: 'slow', label: '지연 (첫 응답 3s 초과)' },
  { value: 'abnormal', label: '이상 (에러/타임아웃/지연)' },
];

export function EventLog({ data: initialData }: { data: MonitoringEventsResponse }) {
  const [filter, setFilter] = useState<MonitoringEventTypeFilter>('all');
  const [data, setData] = useState<MonitoringEventsResponse>(initialData);
  const [isPending, startTransition] = useTransition();

  const handleFilterChange = (next: MonitoringEventTypeFilter) => {
    setFilter(next);
    startTransition(async () => {
      const fresh = await fetchMonitoringEvents(50, next);
      setData(fresh);
    });
  };

  return (
    <div className="glass rounded-apple overflow-hidden">
      <div className="px-4 py-3 border-b border-border flex items-center justify-between gap-3 flex-wrap">
        <h3 className="text-sm font-semibold">사용 로그 (최근 24시간)</h3>
        <div className="flex items-center gap-2">
          <label htmlFor="event-type-filter" className="text-xs text-muted-foreground">
            유형
          </label>
          <select
            id="event-type-filter"
            value={filter}
            onChange={(e) => handleFilterChange(e.target.value as MonitoringEventTypeFilter)}
            disabled={isPending}
            className="text-xs border border-border rounded px-2 py-1 bg-background"
          >
            {FILTER_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      {data.events.length === 0 ? (
        <div className="p-6">
          <p className="text-sm text-muted-foreground">조건에 맞는 이벤트가 없습니다.</p>
        </div>
      ) : (
        <Table density="compact">
          <THead>
            <Tr>
              <Th>시각</Th>
              <Th>유형</Th>
              <Th>모델</Th>
              <Th>사용자</Th>
              <Th>상세</Th>
            </Tr>
          </THead>
          <TBody>
            {data.events.map((ev, i) => (
              <Tr key={`${ev.timestamp}-${i}`}>
                <Td className="text-muted-foreground whitespace-nowrap num">
                  {new Date(ev.timestamp).toLocaleString('ko-KR', {
                    month: '2-digit',
                    day: '2-digit',
                    hour: '2-digit',
                    minute: '2-digit',
                    second: '2-digit',
                  })}
                </Td>
                <Td>
                  <span className={`badge badge-${eventTone(ev.event_type)}`}>
                    {EVENT_LABELS[ev.event_type] ?? ev.event_type}
                  </span>
                </Td>
                <Td emphasis>
                  {ev.downgraded_from ? (
                    <span className="inline-flex items-center gap-1">
                      <span className="text-muted-foreground line-through text-xs font-mono mono-id">
                        {ev.downgraded_from}
                      </span>
                      <span className="text-muted-foreground">→</span>
                      <span className="font-mono mono-id text-xs">{ev.model_alias}</span>
                    </span>
                  ) : (
                    <span className="font-mono mono-id text-xs">{ev.model_alias}</span>
                  )}
                </Td>
                <Td className="text-muted-foreground font-mono mono-id text-xs">
                  {ev.user_id.slice(0, 8)}...
                </Td>
                <Td className="text-muted-foreground">{ev.detail}</Td>
              </Tr>
            ))}
          </TBody>
        </Table>
      )}
    </div>
  );
}
