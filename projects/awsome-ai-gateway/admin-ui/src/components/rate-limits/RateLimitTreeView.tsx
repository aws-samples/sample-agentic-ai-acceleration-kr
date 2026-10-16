'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useEffect, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import type { RateLimitTreeNode } from '@/types/entities';
import { RateLimitTree } from './RateLimitTree';
import { RateLimitConfigPanel } from './RateLimitConfigPanel';

interface RateLimitTreeViewProps {
  nodes: RateLimitTreeNode[];
}

const EXPANDED_NODES_STORAGE_KEY = 'rate-limits:tree:expandedNodes';

/** id 로 노드를 찾는다 — 루트와 자식(팀 멤버) 모두. */
function findNode(nodes: RateLimitTreeNode[], id: string | null): RateLimitTreeNode | null {
  if (id == null) return null;
  for (const n of nodes) {
    if (n.id === id) return n;
    const hit = findNode(n.children, id);
    if (hit) return hit;
  }
  return null;
}

export function RateLimitTreeView({ nodes }: RateLimitTreeViewProps) {
  const t = useTranslations('rateLimits');
  // ⚠️ 선택은 **id** 로만 들고, 노드는 매 렌더 새 `nodes` 에서 찾는다. 예전엔 클릭한 노드 객체를
  //    state 에 붙잡아 두었다 — '개인 한도 제거'/저장 후 서버가 새 트리를 내려도 패널은 옛 객체를
  //    계속 보여줘(지운 오버라이드 값과 '제거' 버튼이 그대로) 이미 지운 행을 다시 지우거나 옛 값을
  //    입력칸에서 다시 저장하게 만들었다.
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selectedNode = findNode(nodes, selectedId);
  // 최초 진입은 모두 접힌 상태다. 루트는 GLOBAL(모델별, children=[]) · TEAM(멤버를 자식으로) ·
  // 팀 미배정 USER(children=[]) 세 부류가 **형제로** 온다 — GLOBAL 은 모델 축이라 TEAM/USER 의
  // 부모가 아니다(admin-api get_rate_limit_tree). 예전의 "GLOBAL 만 펼침" 기본값은 GLOBAL 노드가
  // 트리에 없던 시절의 것이라 아무것도 펼치지 않았다. 펼침 상태는 아래 effect 가 복원한다.
  const [expandedNodes, setExpandedNodes] = useState<Set<string>>(() => new Set<string>());
  const [showInactive, setShowInactive] = useState(false);
  const hasMountedRef = useRef(false);

  const hasInactive = nodes.some(n => n.is_active === false);
  const filteredNodes = showInactive
    ? nodes
    : nodes.filter(n => n.is_active !== false).map(n => ({
        ...n,
        children: n.children.filter(c => c.is_active !== false),
      }));

  useEffect(() => {
    try {
      const raw = sessionStorage.getItem(EXPANDED_NODES_STORAGE_KEY);
      if (raw) {
        const ids = JSON.parse(raw) as unknown;
        if (Array.isArray(ids) && ids.every((x) => typeof x === 'string')) {
          setExpandedNodes(new Set(ids as string[]));
        }
      }
    } catch {
      // 손상/비활성 storage 무시
    }
  }, []);

  useEffect(() => {
    if (!hasMountedRef.current) {
      hasMountedRef.current = true;
      return;
    }
    try {
      sessionStorage.setItem(
        EXPANDED_NODES_STORAGE_KEY,
        JSON.stringify([...expandedNodes])
      );
    } catch {
      // quota/비활성 storage 무시
    }
  }, [expandedNodes]);

  const handleToggle = (id: string) => {
    setExpandedNodes((prev) => {
      if (prev.has(id)) {
        return new Set([...prev].filter((x) => x !== id));
      }
      return new Set([...prev, id]);
    });
  };

  return (
    <div className="space-y-3">
      {hasInactive && (
        <label className="flex items-center gap-1.5 cursor-pointer text-xs text-muted-foreground">
          <input
            type="checkbox"
            checked={showInactive}
            onChange={e => setShowInactive(e.target.checked)}
            className="h-3.5 w-3.5 rounded border-gray-300"
          />
          {t('includeInactive')}
        </label>
      )}
      <div className="flex gap-0 border rounded-lg overflow-hidden min-h-[600px]">
      <div className="w-72 border-r overflow-y-auto">
        <RateLimitTree
          nodes={filteredNodes}
          selectedNodeId={selectedNode?.id ?? null}
          expandedNodes={expandedNodes}
          onSelect={(n) => setSelectedId(n.id)}
          onToggle={handleToggle}
        />
      </div>
      <div className="flex-1 p-6">
        <RateLimitConfigPanel node={selectedNode} />
      </div>
      </div>
    </div>
  );
}