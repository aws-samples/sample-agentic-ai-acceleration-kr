// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

/**
 * 승인 큐 화면 (화면 3) — Manager 이상.
 *
 * candidate 엔티티를 목록으로 보고, 상세(payload)를 확인한 뒤 **승인(publish)** 하거나
 * 사유를 남겨 **반려(reject)** 한다 (status 가 `rejected` 로 전환).
 * 반려된 항목은 승인 큐에서 사라지고 파생 저장소(OpenSearch·Neptune)에도 노출되지 않으며,
 * "반려 목록" 탭에서 이력을 확인하고 필요하면 다시 승인(발행)해 되살릴 수 있다.
 */

import { useCallback, useEffect, useState } from 'react';
import { apiFetch, type SessionInfo } from '@/lib/client';
import { ENTITY_TYPE_LABEL, type SemanticEntity } from '@/lib/types';
import { Alert, EmptyState, Section, StatusBadge, formatTime } from './ui';

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

function payloadOf(entity: SemanticEntity): Record<string, unknown> {
  const payload: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(entity)) {
    if (!META_FIELDS.has(key)) payload[key] = value;
  }
  return payload;
}

/** 목록 탭 — 승인 대기(candidate) / 반려 이력(rejected). */
type QueueTab = 'candidate' | 'rejected';

const TAB_LABEL: Record<QueueTab, string> = {
  candidate: '승인 대기',
  rejected: '반려 목록',
};

export function ApprovalsView({ session }: { session: SessionInfo }) {
  const [tab, setTab] = useState<QueueTab>('candidate');
  const [items, setItems] = useState<SemanticEntity[]>([]);
  const [selected, setSelected] = useState<SemanticEntity | null>(null);
  const [reason, setReason] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // 승인 대기는 워크플로 전용 경로, 반려 목록은 list_entities(status=rejected) 를 쓴다.
      const path =
        tab === 'candidate' ? '/api/approvals' : '/api/semantic/entities?status=rejected';
      const body = await apiFetch<{ entities?: SemanticEntity[] }>(path, {
        token: session.accessToken,
      });
      const entities = body.entities ?? [];
      setItems(entities);
      // 선택 항목이 목록에서 사라졌으면(승인·반려 완료) 상세를 닫는다.
      setSelected((current) =>
        current
          ? (entities.find(
              (e) => e.entity_type === current.entity_type && e.entity_id === current.entity_id
            ) ?? null)
          : null
      );
    } catch (caught) {
      setError((caught as Error).message);
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [session.accessToken, tab]);

  useEffect(() => {
    void load();
  }, [load]);

  const entityPath = (entity: SemanticEntity, action: 'publish' | 'reject') =>
    `/api/semantic/entities/${encodeURIComponent(entity.entity_type)}/` +
    `${encodeURIComponent(entity.entity_id)}/${action}`;

  const approve = async (entity: SemanticEntity) => {
    setError(null);
    setNotice(null);
    try {
      await apiFetch(entityPath(entity, 'publish'), {
        token: session.accessToken,
        method: 'POST',
      });
      setNotice(
        tab === 'rejected'
          ? `반려 항목을 재승인(발행)했습니다 — ${entity.entity_id}`
          : `승인(발행) 완료 — ${entity.entity_id}`
      );
      await load();
    } catch (caught) {
      setError((caught as Error).message);
    }
  };

  const reject = async (entity: SemanticEntity) => {
    setError(null);
    setNotice(null);
    try {
      await apiFetch(entityPath(entity, 'reject'), {
        token: session.accessToken,
        method: 'POST',
        body: JSON.stringify({ reason: reason.trim() }),
      });
      setNotice(`반려 완료 — ${entity.entity_id} (반려 목록에서 확인)`);
      setReason('');
      await load();
    } catch (caught) {
      setError((caught as Error).message);
    }
  };

  const isRejectedTab = tab === 'rejected';

  return (
    <Section
      title="승인 큐"
      description="후보(candidate) 상태 엔티티를 검토해 승인(발행)하거나, 사유를 남겨 반려합니다. 반려된 항목은 rejected 상태로 큐에서 빠지고 파생 저장소에도 노출되지 않으며, 반려 목록에서 다시 승인해 되살릴 수 있습니다."
    >
      <Alert kind="error" message={error} />
      <Alert kind="ok" message={notice} />

      <div className="adm-row">
        <div className="adm-tabs" role="tablist">
          {(['candidate', 'rejected'] as QueueTab[]).map((key) => (
            <button
              key={key}
              type="button"
              role="tab"
              aria-selected={tab === key}
              className={`adm-tab ${tab === key ? 'adm-tab-active' : ''}`}
              onClick={() => {
                setTab(key);
                setSelected(null);
                setReason('');
              }}
            >
              {TAB_LABEL[key]}
            </button>
          ))}
        </div>
        <span className="adm-badge">
          {isRejectedTab ? '반려' : '대기'} {items.length}건
        </span>
        <button className="adm-btn" type="button" onClick={() => void load()} disabled={loading}>
          {loading ? '불러오는 중…' : '새로고침'}
        </button>
      </div>

      <div className="adm-split">
        <div className="adm-table-wrap">
          <table className="adm-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>타입</th>
                <th>버전</th>
                <th>{isRejectedTab ? '반려' : '제출'}</th>
                <th>작업</th>
              </tr>
            </thead>
            <tbody>
              {items.map((entity) => (
                <tr
                  key={`${entity.entity_type}#${entity.entity_id}`}
                  className={
                    selected?.entity_id === entity.entity_id &&
                    selected?.entity_type === entity.entity_type
                      ? 'adm-row-selected'
                      : undefined
                  }
                >
                  <td>
                    <button
                      className="adm-btn-link adm-mono"
                      type="button"
                      onClick={() => setSelected(entity)}
                    >
                      {entity.entity_id}
                    </button>
                  </td>
                  <td>{ENTITY_TYPE_LABEL[entity.entity_type] ?? entity.entity_type}</td>
                  <td className="adm-mono">v{entity.version ?? '-'}</td>
                  <td>
                    <div>{formatTime(entity.updated_at)}</div>
                    <div className="adm-mono" style={{ color: 'var(--t2s-muted)' }}>
                      {entity.updated_by ?? '-'}
                    </div>
                  </td>
                  <td>
                    <div className="adm-actions">
                      <button
                        className="adm-btn adm-btn-sm adm-btn-primary"
                        type="button"
                        onClick={() => void approve(entity)}
                      >
                        {isRejectedTab ? '재승인' : '승인'}
                      </button>
                      {isRejectedTab ? null : (
                        <button
                          className="adm-btn adm-btn-sm"
                          type="button"
                          onClick={() => void reject(entity)}
                        >
                          반려
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
              {!items.length && !loading ? (
                <tr>
                  <td colSpan={5}>
                    <EmptyState>
                      {isRejectedTab
                        ? '반려된 항목이 없습니다.'
                        : '승인 대기 중인 항목이 없습니다.'}
                    </EmptyState>
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>

        <div className="adm-panel">
          <h3 style={{ margin: '0 0 12px', fontSize: 14 }}>상세</h3>
          {selected ? (
            <>
              <div className="adm-desc" style={{ marginBottom: 10 }}>
                {ENTITY_TYPE_LABEL[selected.entity_type] ?? selected.entity_type} ·{' '}
                <span className="adm-mono">{selected.entity_id}</span> · v{selected.version ?? '-'}{' '}
                <StatusBadge status={selected.status} />
              </div>
              <pre className="adm-code">{JSON.stringify(payloadOf(selected), null, 2)}</pre>

              {isRejectedTab ? null : (
                <div className="adm-field">
                  <label className="adm-label" htmlFor="adm-reject-reason">
                    반려 사유 (선택)
                  </label>
                  <input
                    id="adm-reject-reason"
                    className="adm-input"
                    value={reason}
                    onChange={(event) => setReason(event.target.value)}
                    placeholder="예: 용어 정의가 기존 항목과 중복"
                  />
                </div>
              )}

              <div className="adm-actions" style={{ marginTop: 12 }}>
                <button
                  className="adm-btn adm-btn-primary"
                  type="button"
                  onClick={() => void approve(selected)}
                >
                  {isRejectedTab ? '재승인(발행)' : '승인(발행)'}
                </button>
                {isRejectedTab ? null : (
                  <button
                    className="adm-btn"
                    type="button"
                    onClick={() => void reject(selected)}
                    title="사유는 payload.rejection_reason 으로 기록됩니다"
                  >
                    반려
                  </button>
                )}
                <button className="adm-btn" type="button" onClick={() => setSelected(null)}>
                  닫기
                </button>
              </div>
            </>
          ) : (
            <EmptyState>왼쪽 목록에서 항목을 선택하세요.</EmptyState>
          )}
        </div>
      </div>
    </Section>
  );
}
