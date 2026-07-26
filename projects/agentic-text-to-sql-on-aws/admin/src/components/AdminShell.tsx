// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

/**
 * 관리자 패널 셸 — 세션 유지 + 탭 라우팅.
 *
 * 화면 전환은 클라이언트 상태로만 처리한다(서버 라우팅 불필요, 단일 컨테이너 SPA 형태).
 * 그룹이 Manager 면 권한 관리(iam) 탭을 숨긴다 — 서버 route 도 Admin 만 허용하므로
 * 화면 숨김은 UX 보조이고 실제 강제는 서버·Cedar 가 담당한다(이중 방어).
 */

import { useEffect, useState } from 'react';
import { clearToken, loadSession, type SessionInfo } from '@/lib/client';
import { ApprovalsView } from './ApprovalsView';
import { CurationView } from './CurationView';
import { DashboardView } from './DashboardView';
import { DatasourcesView } from './DatasourcesView';
import { EvaluationView } from './EvaluationView';
import { IamView } from './IamView';
import { LoginView } from './LoginView';

type TabKey = 'curation' | 'approvals' | 'datasources' | 'evaluation' | 'iam' | 'dashboard';

const TABS: Array<{ key: TabKey; label: string; adminOnly?: boolean }> = [
  { key: 'curation', label: 'Semantic 큐레이션' },
  { key: 'approvals', label: '승인 큐' },
  { key: 'datasources', label: '데이터 소스' },
  // M5 — 평가(배치·온라인)·개선 추천·config bundle 승격 (Manager 이상).
  { key: 'evaluation', label: '평가·개선' },
  { key: 'iam', label: '권한 관리', adminOnly: true },
  { key: 'dashboard', label: '대시보드' },
];

export function AdminShell() {
  const [session, setSession] = useState<SessionInfo | null>(null);
  // 초기 마운트 전에는 sessionStorage 를 읽을 수 없어 로그인 화면 깜빡임을 막기 위한 플래그.
  const [restored, setRestored] = useState(false);
  const [tab, setTab] = useState<TabKey>('curation');

  useEffect(() => {
    setSession(loadSession());
    setRestored(true);
  }, []);

  if (!restored) return null;

  if (!session) {
    return <LoginView onLogin={setSession} />;
  }

  const visibleTabs = TABS.filter((entry) => !entry.adminOnly || session.isAdmin);
  // Admin 이 아닌데 iam 탭이 선택돼 있으면 첫 탭으로 되돌린다.
  const activeTab = visibleTabs.some((entry) => entry.key === tab) ? tab : visibleTabs[0].key;

  const logout = () => {
    clearToken();
    setSession(null);
    setTab('curation');
  };

  return (
    <div className="adm-shell">
      <header className="adm-header">
        <div>
          <h1>Agentic Text-to-SQL 관리자 패널</h1>
          <p>semantic 큐레이션 · 승인 · 데이터 소스 · 평가·개선 · 권한 · 관측</p>
        </div>
        <div className="adm-user">
          <span className="adm-mono">{session.username}</span>
          <span className="adm-badge">{session.groups.join(', ') || '그룹 없음'}</span>
          <button className="adm-btn adm-btn-sm" type="button" onClick={logout}>
            로그아웃
          </button>
        </div>
      </header>

      <nav className="adm-tabs">
        {visibleTabs.map((entry) => (
          <button
            key={entry.key}
            type="button"
            className={`adm-tab ${activeTab === entry.key ? 'adm-tab-active' : ''}`}
            onClick={() => setTab(entry.key)}
          >
            {entry.label}
          </button>
        ))}
      </nav>

      <main className="adm-main">
        {activeTab === 'curation' ? <CurationView session={session} /> : null}
        {activeTab === 'approvals' ? <ApprovalsView session={session} /> : null}
        {activeTab === 'datasources' ? <DatasourcesView session={session} /> : null}
        {activeTab === 'evaluation' ? <EvaluationView session={session} /> : null}
        {activeTab === 'iam' ? <IamView session={session} /> : null}
        {activeTab === 'dashboard' ? <DashboardView session={session} /> : null}
      </main>
    </div>
  );
}
