'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useEffect, useRef, useState } from 'react';
import type { RateLimitTreeNode } from '@/types/entities';
import { RateLimitTree } from './RateLimitTree';
import { RateLimitConfigPanel } from './RateLimitConfigPanel';

interface RateLimitTreeViewProps {
  nodes: RateLimitTreeNode[];
}

const EXPANDED_NODES_STORAGE_KEY = 'rate-limits:tree:expandedNodes';

function collectDefaultExpanded(nodes: RateLimitTreeNode[]): Set<string> {
  // 기본적으로 GLOBAL 만 펼침. TEAM 은 접혀 있어서 USER 리스트 숨김.
  const ids = new Set<string>();
  for (const n of nodes) {
    if (n.scope === 'GLOBAL') ids.add(n.id);
  }
  return ids;
}

export function RateLimitTreeView({ nodes }: RateLimitTreeViewProps) {
  const [selectedNode, setSelectedNode] = useState<RateLimitTreeNode | null>(null);
  const [expandedNodes, setExpandedNodes] = useState<Set<string>>(() =>
    collectDefaultExpanded(nodes)
  );
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
          비활성 팀/유저 포함
        </label>
      )}
      <div className="flex gap-0 border rounded-lg overflow-hidden min-h-[600px]">
      <div className="w-72 border-r overflow-y-auto">
        <RateLimitTree
          nodes={filteredNodes}
          selectedNodeId={selectedNode?.id ?? null}
          expandedNodes={expandedNodes}
          onSelect={setSelectedNode}
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