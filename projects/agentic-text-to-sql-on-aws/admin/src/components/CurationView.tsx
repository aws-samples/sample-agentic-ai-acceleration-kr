// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

/**
 * Semantic 큐레이션 화면 (화면 2).
 *
 * 용어/동의어/관계(join)/few-shot 등 semantic 엔티티를 entity_type·status 로 필터해 조회하고,
 * payload 를 JSON 으로 편집해 저장한다. 저장은 항상 **candidate** 로 들어가고(승인 큐 경유),
 * 발행·회수는 publish/unpublish 버튼으로 수행한다. 버전(version)은 목록·폼에 함께 표시한다.
 *
 * 모든 쓰기는 서버 route → Gateway MCP(사용자 토큰 OBO) 경로다 — 브라우저는 DynamoDB 를 모른다.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { apiFetch, type SessionInfo } from '@/lib/client';
import { CURATION_TYPES, ENTITY_TYPE_LABEL, STATUSES, type SemanticEntity } from '@/lib/types';
import { Alert, EmptyState, Section, StatusBadge, formatTime } from './ui';

/** payload = 엔티티에서 메타 필드를 제외한 나머지 (편집 대상). */
const META_FIELDS = new Set([
  'pk',
  'sk',
  'entity_type',
  'entity_id',
  'status',
  'version',
  'updated_at',
  'updated_by',
]);

function toPayload(entity: SemanticEntity): Record<string, unknown> {
  const payload: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(entity)) {
    if (!META_FIELDS.has(key)) payload[key] = value;
  }
  return payload;
}

/** entity_type 별 신규 생성 시 제시할 payload 골격 (편집 진입 장벽 완화). */
const PAYLOAD_TEMPLATES: Record<string, Record<string, unknown>> = {
  term: { term: '', definition: '', synonyms: [], sql_fragment: '' },
  fewshot: { question: '', sql: '', note: '' },
  join: { from_table: '', to_table: '', join_sql: '', cardinality: '' },
  table: { table: '', description: '', ddl_snippet: '' },
  column: { table: '', column: '', description: '', data_type: '' },
};

export function CurationView({ session }: { session: SessionInfo }) {
  const [typeFilter, setTypeFilter] = useState<string>('term');
  const [statusFilter, setStatusFilter] = useState<string>('');
  const [entities, setEntities] = useState<SemanticEntity[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // 편집 폼 상태 — selectedId 가 null 이면 신규 생성 모드.
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [formType, setFormType] = useState<string>('term');
  const [formId, setFormId] = useState('');
  const [formPayload, setFormPayload] = useState('{}');
  const [formVersion, setFormVersion] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const query = new URLSearchParams();
      if (typeFilter) query.set('type', typeFilter);
      if (statusFilter) query.set('status', statusFilter);
      const body = await apiFetch<{ entities?: SemanticEntity[] }>(
        `/api/semantic/entities?${query.toString()}`,
        { token: session.accessToken }
      );
      setEntities(body.entities ?? []);
    } catch (caught) {
      setError((caught as Error).message);
      setEntities([]);
    } finally {
      setLoading(false);
    }
  }, [typeFilter, statusFilter, session.accessToken]);

  useEffect(() => {
    void load();
  }, [load]);

  const startCreate = () => {
    setSelectedId(null);
    setFormType(typeFilter || 'term');
    setFormId('');
    setFormPayload(JSON.stringify(PAYLOAD_TEMPLATES[typeFilter || 'term'] ?? {}, null, 2));
    setFormVersion(null);
    setNotice(null);
  };

  const startEdit = (entity: SemanticEntity) => {
    setSelectedId(entity.entity_id);
    setFormType(entity.entity_type);
    setFormId(entity.entity_id);
    setFormPayload(JSON.stringify(toPayload(entity), null, 2));
    setFormVersion(typeof entity.version === 'number' ? entity.version : null);
    setNotice(null);
    setError(null);
  };

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    setNotice(null);

    let payload: unknown;
    try {
      payload = JSON.parse(formPayload);
    } catch (caught) {
      setError(`payload JSON 형식 오류: ${(caught as Error).message}`);
      return;
    }
    if (payload == null || typeof payload !== 'object' || Array.isArray(payload)) {
      setError('payload 는 JSON 객체여야 합니다');
      return;
    }
    const entityId = formId.trim();
    if (!entityId) {
      setError('엔티티 ID 가 필요합니다');
      return;
    }

    setSaving(true);
    try {
      // 쓰기는 항상 candidate — 발행은 승인 단계에서 별도로 수행한다.
      await apiFetch(
        `/api/semantic/entities/${encodeURIComponent(formType)}/${encodeURIComponent(entityId)}`,
        {
          token: session.accessToken,
          method: 'PUT',
          body: JSON.stringify({ payload, status: 'candidate' }),
        }
      );
      setNotice(`저장되었습니다 (후보 상태) — ${entityId}`);
      setSelectedId(entityId);
      await load();
    } catch (caught) {
      setError((caught as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const changeStatus = async (entity: SemanticEntity, action: 'publish' | 'unpublish') => {
    setError(null);
    setNotice(null);
    try {
      await apiFetch(
        `/api/semantic/entities/${encodeURIComponent(entity.entity_type)}/` +
          `${encodeURIComponent(entity.entity_id)}/${action}`,
        { token: session.accessToken, method: 'POST' }
      );
      setNotice(
        action === 'publish'
          ? `발행되었습니다 — ${entity.entity_id} (검색 평면에 전파까지 수 초 소요)`
          : `발행이 회수되었습니다 — ${entity.entity_id}`
      );
      await load();
    } catch (caught) {
      setError((caught as Error).message);
    }
  };

  const typeOptions = useMemo(() => CURATION_TYPES, []);

  return (
    <Section
      title="Semantic 큐레이션"
      description="용어·동의어·관계(join)·few-shot 을 편집합니다. 저장은 후보(candidate) 상태로 기록되고, 발행하면 검색 평면(OpenSearch·Neptune)에 전파됩니다."
    >
      <Alert kind="error" message={error} />
      <Alert kind="ok" message={notice} />

      <div className="adm-row">
        <div className="adm-field">
          <label className="adm-label" htmlFor="adm-type-filter">
            엔티티 타입
          </label>
          <select
            id="adm-type-filter"
            className="adm-select"
            value={typeFilter}
            onChange={(e) => setTypeFilter(e.target.value)}
          >
            <option value="">전체</option>
            {typeOptions.map((type) => (
              <option key={type} value={type}>
                {ENTITY_TYPE_LABEL[type] ?? type}
              </option>
            ))}
          </select>
        </div>
        <div className="adm-field">
          <label className="adm-label" htmlFor="adm-status-filter">
            상태
          </label>
          <select
            id="adm-status-filter"
            className="adm-select"
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
          >
            <option value="">전체</option>
            {STATUSES.map((status) => (
              <option key={status} value={status}>
                {status === 'candidate' ? '후보' : '발행됨'}
              </option>
            ))}
          </select>
        </div>
        <button className="adm-btn" type="button" onClick={() => void load()} disabled={loading}>
          {loading ? '불러오는 중…' : '새로고침'}
        </button>
        <button className="adm-btn adm-btn-primary" type="button" onClick={startCreate}>
          새로 만들기
        </button>
      </div>

      <div className="adm-split">
        <div className="adm-table-wrap">
          <table className="adm-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>타입</th>
                <th>상태</th>
                <th>버전</th>
                <th>수정</th>
                <th>작업</th>
              </tr>
            </thead>
            <tbody>
              {entities.map((entity) => (
                <tr
                  key={`${entity.entity_type}#${entity.entity_id}`}
                  className={selectedId === entity.entity_id ? 'adm-row-selected' : undefined}
                >
                  <td>
                    <button
                      className="adm-btn-link adm-mono"
                      type="button"
                      onClick={() => startEdit(entity)}
                    >
                      {entity.entity_id}
                    </button>
                  </td>
                  <td>{ENTITY_TYPE_LABEL[entity.entity_type] ?? entity.entity_type}</td>
                  <td>
                    <StatusBadge status={entity.status} />
                  </td>
                  <td className="adm-mono">v{entity.version ?? '-'}</td>
                  <td>
                    <div>{formatTime(entity.updated_at)}</div>
                    <div className="adm-mono" style={{ color: 'var(--t2s-muted)' }}>
                      {entity.updated_by ?? '-'}
                    </div>
                  </td>
                  <td>
                    <div className="adm-actions">
                      {entity.status === 'published' ? (
                        <button
                          className="adm-btn adm-btn-sm"
                          type="button"
                          onClick={() => void changeStatus(entity, 'unpublish')}
                        >
                          발행 회수
                        </button>
                      ) : (
                        <button
                          className="adm-btn adm-btn-sm"
                          type="button"
                          onClick={() => void changeStatus(entity, 'publish')}
                        >
                          발행
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
              {!entities.length && !loading ? (
                <tr>
                  <td colSpan={6}>
                    <EmptyState>표시할 엔티티가 없습니다.</EmptyState>
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>

        <form className="adm-panel" onSubmit={save}>
          <h3 style={{ margin: '0 0 12px', fontSize: 14 }}>
            {selectedId ? `수정 — ${selectedId}` : '신규 생성'}
            {formVersion != null ? (
              <span className="adm-badge" style={{ marginLeft: 8 }}>
                v{formVersion}
              </span>
            ) : null}
          </h3>
          <div className="adm-row">
            <div className="adm-field">
              <label className="adm-label" htmlFor="adm-form-type">
                타입
              </label>
              <select
                id="adm-form-type"
                className="adm-select"
                value={formType}
                onChange={(e) => {
                  setFormType(e.target.value);
                  // 신규 생성 중이면 골격도 함께 교체 (기존 편집 내용은 보존).
                  if (!selectedId) {
                    setFormPayload(
                      JSON.stringify(PAYLOAD_TEMPLATES[e.target.value] ?? {}, null, 2)
                    );
                  }
                }}
                disabled={Boolean(selectedId)}
              >
                {typeOptions.map((type) => (
                  <option key={type} value={type}>
                    {ENTITY_TYPE_LABEL[type] ?? type}
                  </option>
                ))}
              </select>
            </div>
            <div className="adm-field adm-field-grow">
              <label className="adm-label" htmlFor="adm-form-id">
                엔티티 ID
              </label>
              <input
                id="adm-form-id"
                className="adm-input adm-mono"
                value={formId}
                onChange={(e) => setFormId(e.target.value)}
                disabled={Boolean(selectedId)}
                placeholder="예: 활성고객"
                required
              />
            </div>
          </div>
          <div className="adm-field">
            <label className="adm-label" htmlFor="adm-form-payload">
              payload (JSON)
            </label>
            <textarea
              id="adm-form-payload"
              className="adm-textarea"
              value={formPayload}
              onChange={(e) => setFormPayload(e.target.value)}
              spellCheck={false}
            />
          </div>
          <div className="adm-actions" style={{ marginTop: 12 }}>
            <button className="adm-btn adm-btn-primary" type="submit" disabled={saving}>
              {saving ? '저장 중…' : '후보로 저장'}
            </button>
            <button className="adm-btn" type="button" onClick={startCreate}>
              초기화
            </button>
          </div>
          <p className="adm-desc" style={{ marginTop: 10, marginBottom: 0 }}>
            저장은 항상 후보(candidate) 상태로 기록되며, 발행은 목록의 발행 버튼 또는 승인 큐에서
            수행합니다. 임베딩은 서버가 쓰기 시점에 계산합니다.
          </p>
        </form>
      </div>
    </Section>
  );
}
