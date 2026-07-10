// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { adminAPI } from '@/lib/api-client';
import type { OrgTreeNode } from '@/types/entities';
import { OrgTreeView } from '@/components/users/OrgTreeView';
import { CognitoSyncButton } from '@/components/users/CognitoSyncButton';
import { RegisterScreenContext } from '@/components/chat/RegisterScreenContext';

/** 트리를 순회해 노드 타입별 개수만 집계. email/이름 등 PII 는 일절 미수집. */
function countOrgNodes(node: OrgTreeNode | null): Record<string, number> {
  const counts: Record<string, number> = {};
  const walk = (n: OrgTreeNode) => {
    counts[n.type] = (counts[n.type] ?? 0) + 1;
    n.children?.forEach(walk);
  };
  if (node) walk(node);
  return counts;
}

export default async function UsersPage() {
  const orgTree = await adminAPI
    .get<OrgTreeNode>('/admin/users/tree')
    .catch(() => null);

  // 퀵챗 화면 컨텍스트용 — 조직 구조의 "규모(개수)"만. 사용자 이메일/이름/리더명은
  // 절대 동봉하지 않는다(PII). 상세는 agent 가 query_db 로 직접 조회.
  const counts = countOrgNodes(orgTree);

  return (
    <div>
      {/* 퀵챗 화면 컨텍스트 등록(렌더 null) — 조직 규모 개수만. PII 없음. */}
      <RegisterScreenContext
        page="사용자/팀 관리"
        data={{
          부서수: counts.DEPARTMENT ?? 0,
          팀수: counts.TEAM ?? 0,
          사용자수: counts.USER ?? 0,
        }}
      />

      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold">사용자/팀 관리</h1>
        <div className="flex items-center gap-3">
          <CognitoSyncButton />
          <p className="text-xs text-muted-foreground">
            부서/팀/사용자 구조는 Cognito 그룹이 원천입니다. 변경이 필요하면 Cognito
            Console 에서 그룹을 수정하세요.
          </p>
        </div>
      </div>
      <OrgTreeView root={orgTree} />
    </div>
  );
}
