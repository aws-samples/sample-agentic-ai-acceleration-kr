'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useState, useTransition } from 'react';
import { useTranslations } from 'next-intl';
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

export function EventLog({ data: initialData }: { data: MonitoringEventsResponse }) {
  const t = useTranslations('monitoring');
  const [filter, setFilter] = useState<MonitoringEventTypeFilter>('all');
  const [data, setData] = useState<MonitoringEventsResponse>(initialData);
  const [isPending, startTransition] = useTransition();

  const EVENT_LABELS: Record<string, string> = {
    ERROR: t('events.types.ERROR'),
    TIMEOUT: t('events.types.TIMEOUT'),
    SLOW_REQUEST: t('events.types.SLOW_REQUEST'),
    SUCCESS: t('events.types.SUCCESS'),
  };

  const FILTER_OPTIONS: { value: MonitoringEventTypeFilter; label: string }[] = [
    { value: 'all', label: t('events.filters.all') },
    { value: 'success', label: t('events.filters.success') },
    { value: 'error', label: t('events.filters.error') },
    { value: 'timeout', label: t('events.filters.timeout') },
    { value: 'slow', label: t('events.filters.slow') },
    { value: 'abnormal', label: t('events.filters.abnormal') },
  ];

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
        <h3 className="text-sm font-semibold">{t('events.title')}</h3>
        <div className="flex items-center gap-2">
          <label htmlFor="event-type-filter" className="text-xs text-muted-foreground">
            {t('events.typeLabel')}
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
          <p className="text-sm text-muted-foreground">{t('events.empty')}</p>
        </div>
      ) : (
        <Table density="compact">
          <THead>
            <Tr>
              <Th>{t('events.colTime')}</Th>
              <Th>{t('events.colType')}</Th>
              <Th>{t('events.colModel')}</Th>
              <Th>{t('events.colUser')}</Th>
              <Th>{t('events.colDetail')}</Th>
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
