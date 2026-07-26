// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

/**
 * 데이터 소스 화면 (화면 4).
 *
 * 등록(id/engine/config JSON) → 연결 테스트 → 스키마 크롤링. config 는 서버 route 를 거쳐
 * MCP 도구가 Secrets Manager 에 저장하며, 브라우저·admin web 상태에는 남기지 않는다.
 * 크롤 결과(table/column/join)는 candidate 로 적재되므로 승인 큐에서 발행해야 반영된다.
 */

import { useCallback, useEffect, useState } from 'react';
import { apiFetch, type SessionInfo } from '@/lib/client';
import { DATASOURCE_ENGINES, type SemanticEntity } from '@/lib/types';
import { Alert, EmptyState, Section, StatusBadge, formatTime } from './ui';

/** engine 별 config 골격 — 필요한 키를 화면에서 알려준다(자격증명은 시크릿으로 저장됨). */
const CONFIG_TEMPLATES: Record<string, Record<string, unknown>> = {
  'aurora-postgresql': {
    cluster_arn: 'arn:aws:rds:us-west-2:000000000000:cluster:...',
    database: 'appdb',
    username: 'agent_ro',
    password: '',
  },
  'redshift-serverless': {
    workgroup: 'agentic-t2sql-rs-wg',
    database: 'dev',
    username: 'agent_ro',
    password: '',
  },
};

export function DatasourcesView({ session }: { session: SessionInfo }) {
  const [items, setItems] = useState<SemanticEntity[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const [datasourceId, setDatasourceId] = useState('');
  const [engine, setEngine] = useState<string>(DATASOURCE_ENGINES[0]);
  const [configText, setConfigText] = useState(
    JSON.stringify(CONFIG_TEMPLATES[DATASOURCE_ENGINES[0]], null, 2)
  );
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const body = await apiFetch<{ entities?: SemanticEntity[] }>('/api/datasources', {
        token: session.accessToken,
      });
      setItems(body.entities ?? []);
    } catch (caught) {
      setError((caught as Error).message);
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [session.accessToken]);

  useEffect(() => {
    void load();
  }, [load]);

  const register = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    setNotice(null);

    let config: unknown;
    try {
      config = JSON.parse(configText);
    } catch (caught) {
      setError(`config JSON 형식 오류: ${(caught as Error).message}`);
      return;
    }
    if (config == null || typeof config !== 'object' || Array.isArray(config)) {
      setError('config 는 JSON 객체여야 합니다');
      return;
    }

    setSaving(true);
    try {
      const body = await apiFetch<{ secret_arn?: string }>('/api/datasources', {
        token: session.accessToken,
        method: 'POST',
        body: JSON.stringify({ datasource_id: datasourceId.trim(), engine, config }),
      });
      setNotice(
        `등록되었습니다 — ${datasourceId.trim()}` +
          (body.secret_arn ? ` (시크릿: ${body.secret_arn})` : '')
      );
      // 자격증명이 화면에 남지 않도록 폼을 골격으로 되돌린다.
      setConfigText(JSON.stringify(CONFIG_TEMPLATES[engine] ?? {}, null, 2));
      setDatasourceId('');
      await load();
    } catch (caught) {
      setError((caught as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const runAction = async (id: string, action: 'test' | 'crawl') => {
    setError(null);
    setNotice(null);
    setBusyId(`${id}:${action}`);
    try {
      const body = await apiFetch<{
        ok?: boolean;
        detail?: string;
        tables?: number;
        columns?: number;
        joins?: number;
      }>(`/api/datasources/${encodeURIComponent(id)}/${action}`, {
        token: session.accessToken,
        method: 'POST',
      });
      if (action === 'test') {
        setNotice(
          `연결 테스트 — ${id}: ${body.ok ? '성공' : '실패'}${body.detail ? ` (${body.detail})` : ''}`
        );
      } else {
        setNotice(
          `스키마 크롤 완료 — ${id}: 테이블 ${body.tables ?? 0} · 컬럼 ${body.columns ?? 0} · ` +
            `관계 ${body.joins ?? 0} (후보 상태로 적재됨 — 승인 큐에서 발행)`
        );
        await load();
      }
    } catch (caught) {
      setError((caught as Error).message);
    } finally {
      setBusyId(null);
    }
  };

  return (
    <Section
      title="데이터 소스"
      description="데이터 소스를 등록하고 연결을 테스트한 뒤 스키마를 크롤링합니다. 자격증명은 Secrets Manager 에 저장되며 화면·DynamoDB 에 남지 않습니다."
    >
      <Alert kind="error" message={error} />
      <Alert kind="ok" message={notice} />

      <div className="adm-split">
        <div>
          <div className="adm-row">
            <button
              className="adm-btn"
              type="button"
              onClick={() => void load()}
              disabled={loading}
            >
              {loading ? '불러오는 중…' : '새로고침'}
            </button>
          </div>
          <div className="adm-table-wrap">
            <table className="adm-table">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>엔진</th>
                  <th>상태</th>
                  <th>수정</th>
                  <th>작업</th>
                </tr>
              </thead>
              <tbody>
                {items.map((entity) => {
                  const id = entity.entity_id;
                  const engineName = typeof entity.engine === 'string' ? entity.engine : '-';
                  return (
                    <tr key={id}>
                      <td className="adm-mono">{id}</td>
                      <td>{engineName}</td>
                      <td>
                        <StatusBadge status={entity.status} />
                      </td>
                      <td>{formatTime(entity.updated_at)}</td>
                      <td>
                        <div className="adm-actions">
                          <button
                            className="adm-btn adm-btn-sm"
                            type="button"
                            onClick={() => void runAction(id, 'test')}
                            disabled={busyId === `${id}:test`}
                          >
                            {busyId === `${id}:test` ? '테스트 중…' : '연결 테스트'}
                          </button>
                          <button
                            className="adm-btn adm-btn-sm"
                            type="button"
                            onClick={() => void runAction(id, 'crawl')}
                            disabled={busyId === `${id}:crawl`}
                          >
                            {busyId === `${id}:crawl` ? '크롤 중…' : '스키마 크롤'}
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
                {!items.length && !loading ? (
                  <tr>
                    <td colSpan={5}>
                      <EmptyState>등록된 데이터 소스가 없습니다.</EmptyState>
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
        </div>

        <form className="adm-panel" onSubmit={register}>
          <h3 style={{ margin: '0 0 12px', fontSize: 14 }}>데이터 소스 등록</h3>
          <div className="adm-row">
            <div className="adm-field adm-field-grow">
              <label className="adm-label" htmlFor="adm-ds-id">
                데이터 소스 ID
              </label>
              <input
                id="adm-ds-id"
                className="adm-input adm-mono"
                value={datasourceId}
                onChange={(e) => setDatasourceId(e.target.value)}
                placeholder="예: sales-mart"
                required
              />
            </div>
            <div className="adm-field">
              <label className="adm-label" htmlFor="adm-ds-engine">
                엔진
              </label>
              <select
                id="adm-ds-engine"
                className="adm-select"
                value={engine}
                onChange={(e) => {
                  setEngine(e.target.value);
                  setConfigText(JSON.stringify(CONFIG_TEMPLATES[e.target.value] ?? {}, null, 2));
                }}
              >
                {DATASOURCE_ENGINES.map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <div className="adm-field">
            <label className="adm-label" htmlFor="adm-ds-config">
              config (JSON)
            </label>
            <textarea
              id="adm-ds-config"
              className="adm-textarea"
              value={configText}
              onChange={(e) => setConfigText(e.target.value)}
              spellCheck={false}
            />
          </div>
          <div className="adm-actions" style={{ marginTop: 12 }}>
            <button className="adm-btn adm-btn-primary" type="submit" disabled={saving}>
              {saving ? '등록 중…' : '등록'}
            </button>
          </div>
          <p className="adm-desc" style={{ marginTop: 10, marginBottom: 0 }}>
            config 의 자격증명은 Secrets Manager{' '}
            <span className="adm-mono">agentic-t2sql/datasource/&lt;id&gt;</span> 에 저장되고,
            DynamoDB 에는 자격증명을 제외한 연결 메타만 후보로 기록됩니다.
          </p>
        </form>
      </div>
    </Section>
  );
}
