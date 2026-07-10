// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { Globe, Users, User, ChevronRight, ChevronDown } from 'lucide-react';
import type { RateLimitTreeNode } from '@/types/entities';
import type { RateLimitScope } from '@/types/enums';

interface RateLimitTreeProps {
  nodes: RateLimitTreeNode[];
  selectedNodeId: string | null;
  expandedNodes: Set<string>;
  onSelect: (node: RateLimitTreeNode) => void;
  onToggle: (id: string) => void;
  depth?: number;
}

function NodeIcon({ scope }: { scope: RateLimitScope }) {
  if (scope === 'GLOBAL') return <Globe size={14} className="flex-shrink-0" aria-hidden="true" />;
  if (scope === 'TEAM') return <Users size={14} className="flex-shrink-0" aria-hidden="true" />;
  return <User size={14} className="flex-shrink-0" aria-hidden="true" />;
}

interface TreeNodeProps {
  node: RateLimitTreeNode;
  selectedNodeId: string | null;
  expandedNodes: Set<string>;
  onSelect: (node: RateLimitTreeNode) => void;
  onToggle: (id: string) => void;
  depth: number;
}

function TreeNode({
  node,
  selectedNodeId,
  expandedNodes,
  onSelect,
  onToggle,
  depth,
}: TreeNodeProps) {
  const isSelected = selectedNodeId === node.id;
  const hasChildren = Boolean(node.children && node.children.length > 0);
  // USER 는 leaf. GLOBAL/TEAM 만 expandable.
  const isExpandable = node.scope !== 'USER' && hasChildren;
  const isExpanded = expandedNodes.has(node.id);

  const handleClick = () => {
    onSelect(node);
    if (isExpandable) {
      onToggle(node.id);
    }
  };

  return (
    <div>
      <button
        onClick={handleClick}
        className={[
          'w-full flex items-center gap-2 py-2 text-sm hover:bg-accent/50 transition-colors text-left',
          isSelected ? 'bg-accent text-accent-foreground' : '',
        ]
          .filter(Boolean)
          .join(' ')}
        style={{ paddingLeft: `${12 + depth * 16}px`, paddingRight: '12px' }}
      >
        {isExpandable ? (
          isExpanded ? (
            <ChevronDown size={12} className="flex-shrink-0 text-muted-foreground" aria-hidden="true" />
          ) : (
            <ChevronRight size={12} className="flex-shrink-0 text-muted-foreground" aria-hidden="true" />
          )
        ) : (
          <span className="w-3 flex-shrink-0" aria-hidden="true" />
        )}

        <NodeIcon scope={node.scope} />
        <span className="flex-1 truncate">{node.label}</span>
        <span className="text-xs text-muted-foreground flex-shrink-0">
          {node.inherited_from
            ? '상속'
            : node.config
            ? `${node.config.rpm ?? '∞'}rpm`
            : '-'}
        </span>
      </button>

      {isExpanded && hasChildren && node.children.map((child) => (
        <TreeNode
          key={child.id}
          node={child}
          selectedNodeId={selectedNodeId}
          expandedNodes={expandedNodes}
          onSelect={onSelect}
          onToggle={onToggle}
          depth={depth + 1}
        />
      ))}
    </div>
  );
}

export function RateLimitTree({
  nodes,
  selectedNodeId,
  expandedNodes,
  onSelect,
  onToggle,
  depth = 0,
}: RateLimitTreeProps) {
  if (nodes.length === 0) {
    return (
      <p className="p-4 text-sm text-muted-foreground">설정 데이터가 없습니다</p>
    );
  }

  return (
    <div className="py-1">
      {nodes.map((node) => (
        <TreeNode
          key={node.id}
          node={node}
          selectedNodeId={selectedNodeId}
          expandedNodes={expandedNodes}
          onSelect={onSelect}
          onToggle={onToggle}
          depth={depth}
        />
      ))}
    </div>
  );
}
