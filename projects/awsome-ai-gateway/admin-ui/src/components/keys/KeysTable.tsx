'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useState } from 'react';
import { revokeKeyAction } from '@/lib/actions/keys';
import { ConfirmDialog } from '@/components/common/ConfirmDialog';
import { useToast } from '@/components/common/ToastProvider';
import { Badge, type BadgeTone } from '@/components/common/Badge';
import { Table, THead, TBody, Tr, Th, Td } from '@/components/common/Table';
import type { VirtualKeyListItem } from '@/types/entities';
import { KeyStatus } from '@/types/enums';

interface KeysTableProps {
  keys: VirtualKeyListItem[];
}

const STATUS_TONE: Record<string, BadgeTone> = {
  [KeyStatus.ACTIVE]: 'teal',
  [KeyStatus.EXPIRED]: 'neutral',
  [KeyStatus.REVOKED]: 'pink',
};

const STATUS_LABELS: Record<string, string> = {
  [KeyStatus.ACTIVE]: '활성',
  [KeyStatus.EXPIRED]: '만료',
  [KeyStatus.REVOKED]: '해지',
};

function formatDate(iso: string | null): string {
  if (!iso) return '없음';
  const date = new Date(iso);
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, '0');
  const d = String(date.getDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

export function KeysTable({ keys }: KeysTableProps) {
  const { toast } = useToast();

  const [revokeState, setRevokeState] = useState<{
    isOpen: boolean;
    keyId: string;
    keyPrefix: string;
  }>({ isOpen: false, keyId: '', keyPrefix: '' });

  const [revokingId, setRevokingId] = useState<string | null>(null);

  const handleRevoke = async () => {
    setRevokingId(revokeState.keyId);
    const result = await revokeKeyAction(revokeState.keyId);
    setRevokingId(null);
    if (result.success) {
      toast({
        type: 'success',
        message: `키 ${revokeState.keyPrefix}가 성공적으로 해지되었습니다.`,
        auto_dismiss_ms: 4000,
      });
    } else {
      toast({
        type: 'error',
        message: result.error ?? '키 해지에 실패했습니다.',
        auto_dismiss_ms: 5000,
      });
    }
  };

  if (keys.length === 0) {
    return (
      <div className="flex items-center justify-center glass rounded-apple py-16 text-sm text-muted-foreground">
        등록된 API Key가 없습니다.
      </div>
    );
  }

  return (
    <>
      <div className="glass rounded-apple overflow-hidden">
        <Table>
          <THead>
            <Tr>
              <Th>키 접두사</Th>
              <Th>사용자 이메일</Th>
              <Th>상태</Th>
              <Th>생성일</Th>
              <Th>만료일</Th>
              <Th numeric>액션</Th>
            </Tr>
          </THead>
          <TBody>
            {keys.map((key) => (
              <Tr key={key.key_id}>
                <Td className="font-mono mono-id text-xs">{key.key_prefix}</Td>
                <Td className="text-foreground">
                  {key.user_email ?? <span className="text-muted-foreground">—</span>}
                </Td>
                <Td>
                  <Badge tone={STATUS_TONE[key.status] ?? 'neutral'}>
                    {STATUS_LABELS[key.status] ?? key.status}
                  </Badge>
                </Td>
                <Td className="text-muted-foreground">{formatDate(key.created_at)}</Td>
                <Td className="text-muted-foreground">{formatDate(key.expires_at)}</Td>
                <Td numeric>
                  <div className="flex items-center justify-end gap-2">
                    <button
                      onClick={() =>
                        setRevokeState({
                          isOpen: true,
                          keyId: key.key_id,
                          keyPrefix: key.key_prefix,
                        })
                      }
                      disabled={
                        key.status === KeyStatus.REVOKED || revokingId === key.key_id
                      }
                      className="inline-flex items-center rounded-md border border-destructive/50 bg-background px-3 py-1.5 text-xs font-medium text-destructive hover:bg-destructive hover:text-destructive-foreground transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-40"
                    >
                      {revokingId === key.key_id ? '처리 중...' : '해지'}
                    </button>
                  </div>
                </Td>
              </Tr>
            ))}
          </TBody>
        </Table>
      </div>

      {/* Revoke Confirm Dialog */}
      <ConfirmDialog
        isOpen={revokeState.isOpen}
        onClose={() => setRevokeState({ isOpen: false, keyId: '', keyPrefix: '' })}
        onConfirm={handleRevoke}
        title="API Key 해지"
        message={`키 "${revokeState.keyPrefix}"를 해지하시겠습니까? 이 작업은 되돌릴 수 없습니다.`}
        confirmLabel="해지"
        isDestructive
      />
    </>
  );
}