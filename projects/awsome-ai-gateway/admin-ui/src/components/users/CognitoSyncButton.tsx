'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useState, useTransition } from 'react';
import { RefreshCw } from 'lucide-react';
import { syncCognitoAction } from '@/lib/actions/users';
import { useToast } from '@/components/common/ToastProvider';

export function CognitoSyncButton() {
  const { toast } = useToast();
  const [isPending, startTransition] = useTransition();
  const [confirmOpen, setConfirmOpen] = useState(false);

  const handleSync = () => {
    startTransition(async () => {
      const result = await syncCognitoAction();
      setConfirmOpen(false);
      if (result.success) {
        const { groups_synced, users_created, users_updated, users_deactivated, errors } =
          result.data;
        const summary = [
          `그룹 ${groups_synced}개 동기화`,
          users_created > 0 ? `신규 ${users_created}명` : null,
          users_updated > 0 ? `업데이트 ${users_updated}명` : null,
          users_deactivated > 0 ? `비활성화 ${users_deactivated}명` : null,
        ]
          .filter(Boolean)
          .join(', ');

        toast({
          type: errors.length > 0 ? 'warning' : 'success',
          message: summary || '동기화 완료 (변경 없음)',
          auto_dismiss_ms: 5000,
        });
      } else {
        toast({ type: 'error', message: result.error, auto_dismiss_ms: 4000 });
      }
    });
  };

  return (
    <>
      <button
        type="button"
        onClick={() => setConfirmOpen(true)}
        disabled={isPending}
        className="inline-flex items-center gap-2 px-3 py-1.5 text-sm font-medium rounded-md border bg-background hover:bg-accent transition-colors disabled:opacity-50"
      >
        <RefreshCw size={14} className={isPending ? 'animate-spin' : ''} />
        Cognito 동기화
      </button>

      {confirmOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
          <div className="bg-background border rounded-lg shadow-lg max-w-md w-full mx-4 p-6">
            <h3 className="text-base font-semibold mb-3">Cognito 동기화</h3>
            <p className="text-sm text-muted-foreground mb-4">
              Cognito User Pool 에서 모든 그룹과 사용자 정보를 가져와 로컬 DB 를
              동기화합니다. 새로운 사용자/팀이 추가되고, 기존 정보가 업데이트됩니다.
            </p>
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setConfirmOpen(false)}
                disabled={isPending}
                className="px-3 py-1.5 text-sm rounded-md border hover:bg-muted"
              >
                취소
              </button>
              <button
                type="button"
                onClick={handleSync}
                disabled={isPending}
                className="inline-flex items-center gap-2 px-3 py-1.5 text-sm font-medium rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
              >
                {isPending && <RefreshCw size={14} className="animate-spin" />}
                {isPending ? '동기화 중...' : '동기화 실행'}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}