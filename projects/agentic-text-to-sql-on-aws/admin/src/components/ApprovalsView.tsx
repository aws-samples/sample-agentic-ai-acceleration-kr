// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

/**
 * 승인 큐 화면 (화면 3) — Manager 이상.
 *
 * candidate 엔티티를 목록으로 보고, 상세(payload)를 확인한 뒤 **승인(publish)** 한다.
 * 반려는 별도 상태를 만들지 않는다 — candidate 로 남겨두는 것이 반려다(설계 결정:
 * 상태 기계를 candidate/published 2개로 유지해 파생 저장소 동기화 규칙을 단순하게 둔다).
 */

import { useCallback, useEffect, useState } from 'react';
import { apiFetch, type SessionInfo } from '@/lib/client';
import { ENTITY_TYPE_LABEL, type SemanticEntity } from '@/lib/types';
import { Alert, EmptyState, Section, formatTime } from './ui';

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

export function ApprovalsView({ session }: { session: SessionInfo }) {
  const [items, setItems] = useState<SemanticEntity[]>([]);
  const [selected, setSelected] = useState<SemanticEntity | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const body = await apiFetch<{ entities?: SemanticEntity[] }>('/api/approvals', {
        token: session.accessToken,
      });
      const entities = body.entities ?? [];
      setItems(entities);
      // 선택 항목이 목록에서 사라졌으면(승인 완료) 상세를 닫는다.
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
  }, [session.accessToken]);

  useEffect(() => {
    void load();
  }, [load]);

  const approve = async (entity: SemanticEntity) => {
    setError(null);
    setNotice(null);
    try {
      await apiFetch(
        `/api/semantic/entities/${encodeURIComponent(entity.entity_type)}/` +
          `${encodeURIComponent(entity.entity_id)}/publish`,
        { token: session.accessToken, method: 'POST' }
      );
      setNotice(`승인(발행) 완료 — ${entity.entity_id}`);
      await load();
    } catch (caught) {
      setError((caught as Error).message);
    }
  };

  return (
    <Section
      title="승인 큐"
      description="후보(candidate) 상태 엔티티를 검토해 승인(발행)합니다. 반려는 별도 조작 없이 후보로 남겨 두면 됩니다."
    >
      <Alert kind="error" message={error} />
      <Alert kind="ok" message={notice} />

      <div className="adm-row">
        <span className="adm-badge">대기 {items.length}건</span>
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
                <th>제출</th>
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
                    <button
                      className="adm-btn adm-btn-sm adm-btn-primary"
                      type="button"
                      onClick={() => void approve(entity)}
                    >
                      승인
                    </button>
                  </td>
                </tr>
              ))}
              {!items.length && !loading ? (
                <tr>
                  <td colSpan={5}>
                    <EmptyState>승인 대기 중인 항목이 없습니다.</EmptyState>
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
                <span className="adm-mono">{selected.entity_id}</span> · v{selected.version ?? '-'}
              </div>
              <pre className="adm-code">{JSON.stringify(payloadOf(selected), null, 2)}</pre>
              <div className="adm-actions" style={{ marginTop: 12 }}>
                <button
                  className="adm-btn adm-btn-primary"
                  type="button"
                  onClick={() => void approve(selected)}
                >
                  승인(발행)
                </button>
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
