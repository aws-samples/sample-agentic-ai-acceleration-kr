// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

/**
 * 대시보드 / 디버깅 화면 (화면 6).
 *
 * 메트릭 요약 카드(CloudWatch GetMetricData)와 최근 세션 목록(AgentCore Runtime 로그 스트림)을
 * 보여주고, 세션을 클릭하면 이벤트 타임라인을 조회한다. 메트릭이 아직 발행되지 않은 환경에서도
 * 화면은 정상 렌더된다(값은 "—").
 */

import { useCallback, useEffect, useState } from 'react';
import { apiFetch, type SessionInfo } from '@/lib/client';
import type { MetricSummaryItem, TraceEvent, TraceSession } from '@/lib/types';
import { Alert, EmptyState, Section, formatTime } from './ui';

/** 숫자 표시 — null 은 "—", 소수는 소수점 1자리로 축약. */
function formatValue(value: number | null): string {
  if (value == null) return '—';
  if (Number.isInteger(value)) return value.toLocaleString('ko-KR');
  return value.toLocaleString('ko-KR', { maximumFractionDigits: 1 });
}

export function DashboardView({ session }: { session: SessionInfo }) {
  const [metrics, setMetrics] = useState<MetricSummaryItem[]>([]);
  const [metricNote, setMetricNote] = useState<string | null>(null);
  const [windowHours, setWindowHours] = useState(24);
  const [sessions, setSessions] = useState<TraceSession[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const [eventsLoading, setEventsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadMetrics = useCallback(
    async (hours: number) => {
      setError(null);
      try {
        const body = await apiFetch<{ items?: MetricSummaryItem[]; note?: string }>(
          `/api/metrics/summary?hours=${hours}`,
          { token: session.accessToken }
        );
        setMetrics(body.items ?? []);
        setMetricNote(body.note ?? null);
      } catch (caught) {
        setError((caught as Error).message);
        setMetrics([]);
      }
    },
    [session.accessToken]
  );

  const loadSessions = useCallback(async () => {
    setLoading(true);
    try {
      const body = await apiFetch<{ sessions?: TraceSession[] }>('/api/traces/sessions', {
        token: session.accessToken,
      });
      setSessions(body.sessions ?? []);
    } catch (caught) {
      setError((caught as Error).message);
      setSessions([]);
    } finally {
      setLoading(false);
    }
  }, [session.accessToken]);

  useEffect(() => {
    void loadMetrics(windowHours);
  }, [loadMetrics, windowHours]);

  useEffect(() => {
    void loadSessions();
  }, [loadSessions]);

  const openSession = async (id: string) => {
    setSelectedId(id);
    setEvents([]);
    setEventsLoading(true);
    setError(null);
    try {
      const body = await apiFetch<{ events?: TraceEvent[] }>(
        `/api/traces/${encodeURIComponent(id)}`,
        { token: session.accessToken }
      );
      setEvents(body.events ?? []);
    } catch (caught) {
      setError((caught as Error).message);
    } finally {
      setEventsLoading(false);
    }
  };

  return (
    <>
      <Section
        title="메트릭 요약"
        description="AgentCore Runtime 의 벤디드 메트릭(AWS/Bedrock-AgentCore)을 조회 창 전체로 집계한 값입니다."
      >
        <Alert kind="error" message={error} />
        <Alert kind="info" message={metricNote} />
        <div className="adm-row">
          <div className="adm-field">
            <label className="adm-label" htmlFor="adm-metric-window">
              조회 창
            </label>
            <select
              id="adm-metric-window"
              className="adm-select"
              value={windowHours}
              onChange={(e) => setWindowHours(Number(e.target.value))}
            >
              <option value={1}>최근 1시간</option>
              <option value={24}>최근 24시간</option>
              <option value={168}>최근 7일</option>
            </select>
          </div>
          <button className="adm-btn" type="button" onClick={() => void loadMetrics(windowHours)}>
            새로고침
          </button>
        </div>
        <div className="adm-cards">
          {metrics.map((item) => (
            <div className="adm-card" key={item.key}>
              <div className="adm-card-label">{item.label}</div>
              <div className="adm-card-value">
                {formatValue(item.value)}
                {item.unit && item.value != null ? (
                  <span className="adm-card-unit">{item.unit}</span>
                ) : null}
              </div>
            </div>
          ))}
        </div>
      </Section>

      <Section
        title="최근 세션 (디버깅)"
        description="Runtime 로그 스트림을 세션 단위로 나열합니다. 세션을 선택하면 이벤트 타임라인을 확인할 수 있습니다."
      >
        <div className="adm-row">
          <button
            className="adm-btn"
            type="button"
            onClick={() => void loadSessions()}
            disabled={loading}
          >
            {loading ? '불러오는 중…' : '새로고침'}
          </button>
        </div>
        <div className="adm-split">
          <div className="adm-table-wrap">
            <table className="adm-table">
              <thead>
                <tr>
                  <th>런타임</th>
                  <th>세션(스트림)</th>
                  <th>최근 이벤트</th>
                </tr>
              </thead>
              <tbody>
                {sessions.map((item) => (
                  <tr
                    key={item.id}
                    className={selectedId === item.id ? 'adm-row-selected' : undefined}
                  >
                    <td className="adm-mono">{item.runtime}</td>
                    <td>
                      <button
                        className="adm-btn-link adm-mono"
                        type="button"
                        onClick={() => void openSession(item.id)}
                      >
                        {item.id.split('|')[1] ?? item.id}
                      </button>
                    </td>
                    <td>{formatTime(item.last_event_at)}</td>
                  </tr>
                ))}
                {!sessions.length && !loading ? (
                  <tr>
                    <td colSpan={3}>
                      <EmptyState>표시할 세션이 없습니다.</EmptyState>
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>

          <div>
            <h3 style={{ margin: '0 0 10px', fontSize: 14 }}>이벤트 타임라인</h3>
            {eventsLoading ? (
              <EmptyState>불러오는 중…</EmptyState>
            ) : events.length ? (
              <div className="adm-timeline">
                {events.map((event, index) => (
                  <div className="adm-timeline-item" key={`${event.timestamp}-${index}`}>
                    <span className="adm-timeline-ts">{formatTime(event.timestamp)}</span>
                    <span className="adm-timeline-msg">{event.message}</span>
                  </div>
                ))}
              </div>
            ) : (
              <EmptyState>
                {selectedId ? '이벤트가 없습니다.' : '왼쪽에서 세션을 선택하세요.'}
              </EmptyState>
            )}
          </div>
        </div>
      </Section>
    </>
  );
}
