// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

/**
 * 권한 관리 화면 (화면 5) — **Admin 전용**.
 *
 * Cognito 사용자 목록·생성·그룹 지정과 Cedar 정책 read-only 뷰를 제공한다.
 * Cedar 정책은 CDK(gateway 스택)가 소유하므로 편집 불가임을 화면에서 명시한다 —
 * 콘솔 편집은 다음 배포에 덮여 드리프트가 된다.
 */

import { useCallback, useEffect, useState } from 'react';
import { apiFetch, type SessionInfo } from '@/lib/client';
import type { CedarPolicySummary, IamUser } from '@/lib/types';
import { Alert, EmptyState, Section, formatTime } from './ui';

export function IamView({ session }: { session: SessionInfo }) {
  const [users, setUsers] = useState<IamUser[]>([]);
  const [groups, setGroups] = useState<string[]>([]);
  const [policies, setPolicies] = useState<CedarPolicySummary[]>([]);
  const [policyEngineId, setPolicyEngineId] = useState<string>('');
  const [openPolicyId, setOpenPolicyId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [policyError, setPolicyError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [newUsername, setNewUsername] = useState('');
  const [newEmail, setNewEmail] = useState('');
  const [newGroup, setNewGroup] = useState('');
  const [creating, setCreating] = useState(false);

  const loadUsers = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const body = await apiFetch<{ users?: IamUser[] }>('/api/iam/users', {
        token: session.accessToken,
      });
      setUsers(body.users ?? []);
    } catch (caught) {
      setError((caught as Error).message);
      setUsers([]);
    } finally {
      setLoading(false);
    }
  }, [session.accessToken]);

  const loadGroups = useCallback(async () => {
    try {
      const body = await apiFetch<{ groups?: Array<{ name: string }> }>('/api/iam/groups', {
        token: session.accessToken,
      });
      const names = (body.groups ?? []).map((g) => g.name).filter(Boolean);
      setGroups(names);
      setNewGroup((current) => current || names[0] || '');
    } catch (caught) {
      // 그룹 목록 실패는 치명적이지 않다 — 직접 입력으로 대체 가능.
      console.warn('그룹 목록 조회 실패:', caught);
    }
  }, [session.accessToken]);

  const loadPolicies = useCallback(async () => {
    setPolicyError(null);
    try {
      const body = await apiFetch<{
        policies?: CedarPolicySummary[];
        policy_engine_id?: string;
      }>('/api/cedar/policies', { token: session.accessToken });
      setPolicies(body.policies ?? []);
      setPolicyEngineId(body.policy_engine_id ?? '');
    } catch (caught) {
      setPolicyError((caught as Error).message);
      setPolicies([]);
    }
  }, [session.accessToken]);

  useEffect(() => {
    void loadUsers();
    void loadGroups();
    void loadPolicies();
  }, [loadUsers, loadGroups, loadPolicies]);

  const createUser = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    setNotice(null);
    setCreating(true);
    try {
      await apiFetch('/api/iam/users', {
        token: session.accessToken,
        method: 'POST',
        body: JSON.stringify({
          username: newUsername.trim(),
          email: newEmail.trim() || undefined,
          group: newGroup || undefined,
        }),
      });
      setNotice(`사용자를 생성했습니다 — ${newUsername.trim()} (임시 비밀번호는 Cognito 가 발송)`);
      setNewUsername('');
      setNewEmail('');
      await loadUsers();
    } catch (caught) {
      setError((caught as Error).message);
    } finally {
      setCreating(false);
    }
  };

  const changeGroup = async (username: string, group: string, action: 'add' | 'remove') => {
    setError(null);
    setNotice(null);
    try {
      await apiFetch(`/api/iam/users/${encodeURIComponent(username)}/groups`, {
        token: session.accessToken,
        method: 'POST',
        body: JSON.stringify({ group, action }),
      });
      setNotice(
        `${username}: ${group} 그룹을 ${action === 'add' ? '지정' : '해제'}했습니다 ` +
          '(사용자 토큰 갱신 후 반영)'
      );
      await loadUsers();
    } catch (caught) {
      setError((caught as Error).message);
    }
  };

  return (
    <>
      <Section
        title="사용자 · 그룹"
        description="Cognito 사용자를 생성하고 그룹(Admin/Manager/User)을 지정합니다. 그룹 클레임이 Cedar 인가와 화면 권한의 근거입니다."
      >
        <Alert kind="error" message={error} />
        <Alert kind="ok" message={notice} />

        <div className="adm-split">
          <div>
            <div className="adm-row">
              <button
                className="adm-btn"
                type="button"
                onClick={() => void loadUsers()}
                disabled={loading}
              >
                {loading ? '불러오는 중…' : '새로고침'}
              </button>
            </div>
            <div className="adm-table-wrap">
              <table className="adm-table">
                <thead>
                  <tr>
                    <th>사용자</th>
                    <th>상태</th>
                    <th>그룹</th>
                    <th>생성</th>
                    <th>그룹 변경</th>
                  </tr>
                </thead>
                <tbody>
                  {users.map((user) => (
                    <tr key={user.username}>
                      <td>
                        <div className="adm-mono">{user.username}</div>
                        {user.email ? (
                          <div style={{ color: 'var(--t2s-muted)', fontSize: 12 }}>
                            {user.email}
                          </div>
                        ) : null}
                      </td>
                      <td>
                        <span className="adm-badge">{user.status ?? '-'}</span>
                      </td>
                      <td>
                        {user.groups.length ? (
                          <div className="adm-actions">
                            {user.groups.map((group) => (
                              <button
                                key={group}
                                className="adm-badge"
                                type="button"
                                title="클릭하면 그룹에서 해제합니다"
                                onClick={() => void changeGroup(user.username, group, 'remove')}
                                style={{ cursor: 'pointer', background: 'transparent' }}
                              >
                                {group} ✕
                              </button>
                            ))}
                          </div>
                        ) : (
                          <span style={{ color: 'var(--t2s-muted)' }}>없음</span>
                        )}
                      </td>
                      <td>{formatTime(user.created_at)}</td>
                      <td>
                        <GroupAssigner
                          groups={groups}
                          onAssign={(group) => void changeGroup(user.username, group, 'add')}
                        />
                      </td>
                    </tr>
                  ))}
                  {!users.length && !loading ? (
                    <tr>
                      <td colSpan={5}>
                        <EmptyState>사용자가 없습니다.</EmptyState>
                      </td>
                    </tr>
                  ) : null}
                </tbody>
              </table>
            </div>
          </div>

          <form className="adm-panel" onSubmit={createUser}>
            <h3 style={{ margin: '0 0 12px', fontSize: 14 }}>사용자 생성</h3>
            <div className="adm-field" style={{ marginBottom: 10 }}>
              <label className="adm-label" htmlFor="adm-new-username">
                사용자 이름
              </label>
              <input
                id="adm-new-username"
                className="adm-input adm-mono"
                value={newUsername}
                onChange={(e) => setNewUsername(e.target.value)}
                placeholder="예: manager@example.com"
                required
              />
            </div>
            <div className="adm-field" style={{ marginBottom: 10 }}>
              <label className="adm-label" htmlFor="adm-new-email">
                이메일 (선택)
              </label>
              <input
                id="adm-new-email"
                className="adm-input"
                type="email"
                value={newEmail}
                onChange={(e) => setNewEmail(e.target.value)}
              />
            </div>
            <div className="adm-field" style={{ marginBottom: 12 }}>
              <label className="adm-label" htmlFor="adm-new-group">
                초기 그룹 (선택)
              </label>
              {groups.length ? (
                <select
                  id="adm-new-group"
                  className="adm-select"
                  value={newGroup}
                  onChange={(e) => setNewGroup(e.target.value)}
                >
                  <option value="">지정하지 않음</option>
                  {groups.map((group) => (
                    <option key={group} value={group}>
                      {group}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  id="adm-new-group"
                  className="adm-input"
                  value={newGroup}
                  onChange={(e) => setNewGroup(e.target.value)}
                  placeholder="예: Manager"
                />
              )}
            </div>
            <button className="adm-btn adm-btn-primary" type="submit" disabled={creating}>
              {creating ? '생성 중…' : '생성'}
            </button>
            <p className="adm-desc" style={{ marginTop: 10, marginBottom: 0 }}>
              임시 비밀번호는 Cognito 가 생성해 초대 메일로 발송합니다. 첫 로그인 시 비밀번호 변경이
              필요하며, 변경 전에는 이 콘솔 로그인이 추가 인증 단계로 거부됩니다.
            </p>
          </form>
        </div>
      </Section>

      <Section
        title="Cedar 정책 (읽기 전용)"
        description="정책은 CDK(gateway 스택)가 소유하는 IaC 산출물입니다. 이 화면에서는 편집할 수 없으며, 변경은 인프라 코드 수정 후 재배포로 수행해야 합니다."
      >
        <Alert kind="error" message={policyError} />
        <Alert
          kind="info"
          message={
            policyEngineId
              ? `PolicyEngine: ${policyEngineId} · 정책 ${policies.length}건 · 편집 불가(read-only)`
              : undefined
          }
        />
        <div className="adm-table-wrap">
          <table className="adm-table">
            <thead>
              <tr>
                <th>이름</th>
                <th>정책 ID</th>
                <th>상태</th>
                <th>모드</th>
                <th>수정</th>
                <th>정의</th>
              </tr>
            </thead>
            <tbody>
              {policies.map((policy) => (
                <tr key={policy.policy_id}>
                  <td>{policy.name ?? '-'}</td>
                  <td className="adm-mono">{policy.policy_id}</td>
                  <td>
                    <span className="adm-badge">{policy.status ?? '-'}</span>
                  </td>
                  <td>{policy.enforcement_mode ?? '-'}</td>
                  <td>{formatTime(policy.updated_at)}</td>
                  <td>
                    {policy.statement ? (
                      <>
                        <button
                          className="adm-btn-link"
                          type="button"
                          onClick={() =>
                            setOpenPolicyId((current) =>
                              current === policy.policy_id ? null : policy.policy_id
                            )
                          }
                        >
                          {openPolicyId === policy.policy_id ? '접기' : '보기'}
                        </button>
                        {openPolicyId === policy.policy_id ? (
                          <pre className="adm-code" style={{ marginTop: 8 }}>
                            {policy.statement}
                          </pre>
                        ) : null}
                      </>
                    ) : (
                      <span style={{ color: 'var(--t2s-muted)' }}>표시 불가</span>
                    )}
                  </td>
                </tr>
              ))}
              {!policies.length ? (
                <tr>
                  <td colSpan={6}>
                    <EmptyState>표시할 정책이 없습니다.</EmptyState>
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </Section>
    </>
  );
}

/** 행 단위 그룹 지정 셀렉터 (선택 후 즉시 적용). */
function GroupAssigner({
  groups,
  onAssign,
}: {
  groups: string[];
  // `_` 접두어: 타입 위치 인자를 미사용으로 오탐하는 base no-unused-vars 회피.
  onAssign: (_group: string) => void;
}) {
  const [value, setValue] = useState('');
  return (
    <div className="adm-actions">
      {groups.length ? (
        <select
          className="adm-select"
          style={{ width: 130 }}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          aria-label="지정할 그룹"
        >
          <option value="">그룹 선택</option>
          {groups.map((group) => (
            <option key={group} value={group}>
              {group}
            </option>
          ))}
        </select>
      ) : (
        <input
          className="adm-input"
          style={{ width: 130 }}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="그룹명"
          aria-label="지정할 그룹"
        />
      )}
      <button
        className="adm-btn adm-btn-sm"
        type="button"
        disabled={!value}
        onClick={() => {
          onAssign(value);
          setValue('');
        }}
      >
        지정
      </button>
    </div>
  );
}
