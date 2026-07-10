// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { Building2, FolderOpen, Folder, Users, User, ChevronRight, ChevronDown } from 'lucide-react';
import type { OrgTreeNode } from '@/types/entities';
import type { OrgNodeType } from '@/types/enums';

interface OrgTreeProps {
  node: OrgTreeNode;
  selectedNodeId: string | null;
  expandedNodes: Set<string>;
  onSelect: (node: OrgTreeNode) => void;
  onToggle: (id: string) => void;
  depth?: number;
}

function NodeIcon({ type, isExpanded }: { type: OrgNodeType; isExpanded: boolean }) {
  switch (type) {
    case 'ORGANIZATION':
      return <Building2 size={14} className="flex-shrink-0" aria-hidden="true" />;
    case 'DEPARTMENT':
      return isExpanded
        ? <FolderOpen size={14} className="flex-shrink-0" aria-hidden="true" />
        : <Folder size={14} className="flex-shrink-0" aria-hidden="true" />;
    case 'TEAM':
      return <Users size={14} className="flex-shrink-0" aria-hidden="true" />;
    case 'USER':
      return <User size={14} className="flex-shrink-0" aria-hidden="true" />;
    default:
      return null;
  }
}

const EXPANDABLE_TYPES: OrgNodeType[] = ['ORGANIZATION', 'DEPARTMENT', 'TEAM'];

export function OrgTree({
  node,
  selectedNodeId,
  expandedNodes,
  onSelect,
  onToggle,
  depth = 0,
}: OrgTreeProps) {
  const isSelected = selectedNodeId === node.id;
  const hasChildren = node.children && node.children.length > 0;
  const isExpandable = EXPANDABLE_TYPES.includes(node.type) && hasChildren;
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
          'w-full flex items-center gap-2 px-3 py-2 text-sm text-left hover:bg-accent/50 rounded-sm transition-colors',
          isSelected ? 'bg-accent text-accent-foreground' : '',
        ]
          .filter(Boolean)
          .join(' ')}
        style={{ paddingLeft: `${12 + depth * 16}px` }}
      >
        {/* 펼치기/접기 chevron */}
        {isExpandable ? (
          isExpanded ? (
            <ChevronDown size={12} className="flex-shrink-0 text-muted-foreground" aria-hidden="true" />
          ) : (
            <ChevronRight size={12} className="flex-shrink-0 text-muted-foreground" aria-hidden="true" />
          )
        ) : (
          <span className="w-3 flex-shrink-0" aria-hidden="true" />
        )}

        <NodeIcon type={node.type} isExpanded={isExpanded} />
        <span className="flex-1 truncate">{node.name}</span>
      </button>

      {/* 자식 노드 재귀 렌더링 */}
      {isExpanded && hasChildren && node.children.map((child) => (
        <OrgTree
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
