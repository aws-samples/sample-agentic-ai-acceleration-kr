'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useState } from 'react';
import { useTranslations } from 'next-intl';
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

function formatDate(iso: string | null, noExpiry: string): string {
  if (!iso) return noExpiry;
  const date = new Date(iso);
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, '0');
  const d = String(date.getDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

export function KeysTable({ keys }: KeysTableProps) {
  const t = useTranslations('keys');
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
        message: t('revokeSuccess', { prefix: revokeState.keyPrefix }),
        auto_dismiss_ms: 4000,
      });
    } else {
      toast({
        type: 'error',
        message: result.error ?? t('revokeFailed'),
        auto_dismiss_ms: 5000,
      });
    }
  };

  if (keys.length === 0) {
    return (
      <div className="flex items-center justify-center glass rounded-apple py-16 text-sm text-muted-foreground">
        {t('noKeys')}
      </div>
    );
  }

  return (
    <>
      <div className="glass rounded-apple overflow-hidden">
        <Table>
          <THead>
            <Tr>
              <Th>{t('keyPrefix')}</Th>
              <Th>{t('userEmail')}</Th>
              <Th>{t('status')}</Th>
              <Th>{t('createdAt')}</Th>
              <Th>{t('expiresAt')}</Th>
              <Th numeric>{t('actions')}</Th>
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
                    {t(`keyStatus.${key.status}` as 'keyStatus.ACTIVE' | 'keyStatus.EXPIRED' | 'keyStatus.REVOKED')}
                  </Badge>
                </Td>
                <Td className="text-muted-foreground">{formatDate(key.created_at, t('noExpiry'))}</Td>
                <Td className="text-muted-foreground">{formatDate(key.expires_at, t('noExpiry'))}</Td>
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
                      {revokingId === key.key_id ? t('revoking') : t('revoke')}
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
        title={t('revokeDialogTitle')}
        message={t('revokeDialogMessage', { prefix: revokeState.keyPrefix })}
        confirmLabel={t('revokeDialogConfirm')}
        isDestructive
      />
    </>
  );
}