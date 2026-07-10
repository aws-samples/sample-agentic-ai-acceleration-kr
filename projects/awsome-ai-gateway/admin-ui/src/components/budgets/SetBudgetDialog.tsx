'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useState, useTransition, useEffect } from 'react';
import { X } from 'lucide-react';
import type { BudgetScope } from '@/types/enums';
import { setBudgetAction, deleteUserBudgetAction } from '@/lib/actions/budgets';
import {
  getUserAllowedClientsAction,
  getUserClientBudgetsAction,
  setUserClientBudgetAction,
  clearUserClientBudgetAction,
} from '@/lib/actions/users';
import { FormError } from '@/components/common/FormError';
import { SpinnerButton } from '@/components/common/SpinnerButton';
import { useToast } from '@/components/common/ToastProvider';

interface SetBudgetDialogProps {
  isOpen: boolean;
  onClose: () => void;
  target: {
    id: string;
    name: string;
    type: (typeof BudgetScope)[keyof typeof BudgetScope];
    currentLimit: number;
    parentLimit?: number;
  } | null;
}

const POLICY_OPTIONS = [
  { value: 'HARD_BLOCK' as const, label: '하드 차단', desc: '한도 초과 시 요청 차단' },
  { value: 'SOFT_WARNING' as const, label: '경고만', desc: '한도 초과 시 경고 알림만 발송' },
  { value: 'THROTTLE' as const, label: '쓰로틀링', desc: '임계치 초과 시 RPM 자동 감소' },
];

const DEFAULT_THRESHOLDS = [80, 90, 100];

// per-app(client) 예산 게이팅 — /users 화면(OrgDetailPanel UserPanel)과 동일 로직.
// 빈 allowed_clients = 전체 허용. 새 앱은 ALL_CLIENTS 에만 추가하면 자동 확장.
const ALL_CLIENTS = ['claude-code', 'cowork', 'codex'] as const;
type ClientId = (typeof ALL_CLIENTS)[number];
const CLIENT_LABELS: Record<ClientId, string> = {
  'claude-code': 'Claude Code',
  cowork: 'Cowork',
  codex: 'Codex',
};

// API allowed_clients([] = 전체 허용) → 허용 client 목록. [] 면 전부 허용으로 펼친다.
function allowedClientList(clients: string[]): ClientId[] {
  if (clients.length === 0) return [...ALL_CLIENTS];
  return ALL_CLIENTS.filter((c) => clients.includes(c));
}

export function SetBudgetDialog({ isOpen, onClose, target }: SetBudgetDialogProps) {
  const { toast } = useToast();
  const [isPending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);
  const [value, setValue] = useState<string>(String(target?.currentLimit ?? ''));
  const [policy, setPolicy] = useState<'HARD_BLOCK' | 'SOFT_WARNING' | 'THROTTLE'>('HARD_BLOCK');
  const [thresholds, setThresholds] = useState<number[]>(DEFAULT_THRESHOLDS);
  const [newThreshold, setNewThreshold] = useState<string>('50');

  // per-app(client) 예산 — USER scope 에서만 사용. 빈 문자열 = 미설정.
  // loaded* 는 prefill 시점 값 기억 → 비우고 저장하면 clear 로 이어진다. client→문자열 map.
  const emptyBudgets = (): Record<string, string> =>
    Object.fromEntries(ALL_CLIENTS.map((c) => [c, '']));
  const [allowedClients, setAllowedClients] = useState<ClientId[]>([...ALL_CLIENTS]);
  const [budgets, setBudgets] = useState<Record<string, string>>(emptyBudgets);
  const [loadedBudgets, setLoadedBudgets] = useState<Record<string, string>>(emptyBudgets);
  // 허용 클라이언트 정책 로드 성공 여부(Codex R2 #6). 실패 시 stale 전체허용 기준으로
  // per-app 예산을 쓰면 codex 등에 잘못 기록될 수 있어 per-app 저장을 건너뛴다.
  const [clientsLoaded, setClientsLoaded] = useState(false);
  const [isAppLoadPending, startAppLoadTransition] = useTransition();

  useEffect(() => {
    if (target) {
      setValue(target.currentLimit ? String(target.currentLimit) : '');
    }
  }, [target]);

  const numericValue = parseFloat(value) || 0;
  const maxValue = target?.parentLimit ?? 999999;
  const isUserScope = target?.type === 'USER';
  const [useTeamBudget, setUseTeamBudget] = useState(false);

  // 다이얼로그가 USER 대상으로 열릴 때 allowed-clients + per-app 예산을 병렬 로드.
  // TEAM scope 는 앱별 예산 개념이 없으므로 스킵.
  useEffect(() => {
    if (!isOpen || !isUserScope || !target?.id) return;
    const userId = target.id;
    setClientsLoaded(false);  // 대상/오픈 전환 시 stale 정책으로 저장하지 않도록 리셋.
    startAppLoadTransition(async () => {
      const [r, b] = await Promise.all([
        getUserAllowedClientsAction(userId),
        getUserClientBudgetsAction(userId),
      ]);
      if (r.success) {
        setAllowedClients(allowedClientList(r.data.clients));
        setClientsLoaded(true);
      } else {
        // 조회 실패 시 stale 전체허용이 실제값처럼 보여 잘못된 clear 를 유발할 수 있으므로 명시 알림.
        toast({
          type: 'error',
          message: '앱 접근 권한 조회 실패 — 새로고침 후 다시 시도하세요.',
          auto_dismiss_ms: 5000,
        });
      }
      if (b.success) {
        const byClient = new Map(b.data.apps.map((a) => [a.client, a.max_budget_usd]));
        const next = emptyBudgets();
        for (const c of ALL_CLIENTS) next[c] = byClient.get(c) ?? '';
        setBudgets(next);
        setLoadedBudgets(next);
      } else {
        // 조회 실패 시 stale 값(이전 대상의 예산)이 남아 잘못 저장될 수 있으므로 비우고 명시 알림.
        setBudgets(emptyBudgets());
        setLoadedBudgets(emptyBudgets());
        toast({
          type: 'error',
          message: '앱별 예산 조회 실패 — 새로고침 후 다시 시도하세요.',
          auto_dismiss_ms: 5000,
        });
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target?.id, isUserScope, isOpen]);

  if (!target) return null;

  const handleUseTeamBudget = () => {
    setError(null);
    startTransition(async () => {
      const result = await deleteUserBudgetAction(target.id);
      if (result.success) {
        toast({
          type: 'success',
          message: `${target.name}의 개인 예산이 삭제되었습니다. 팀 예산이 적용됩니다.`,
          auto_dismiss_ms: 3000,
        });
        onClose();
      } else {
        setError(result.error);
      }
    });
  };

  const addThreshold = () => {
    const val = parseInt(newThreshold) || 0;
    if (val >= 1 && val <= 100 && !thresholds.includes(val)) {
      setThresholds(prev => [...prev, val].sort((a, b) => a - b));
    }
  };

  const removeThreshold = (val: number) => {
    setThresholds(prev => prev.filter(t => t !== val));
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    if (thresholds.length === 0) {
      setError('최소 1개 이상의 알림 임계값을 설정해야 합니다.');
      return;
    }

    startTransition(async () => {
      const result = await setBudgetAction({
        target_id: target.id,
        target_type: target.type,
        max_budget_usd: numericValue,
        policy,
        alert_thresholds: thresholds,
      });

      if (!result.success) {
        setError(result.error);
        return;
      }

      // 총 예산 저장 성공. USER scope 에서는 이어서 앱별(per-app) 예산도 저장한다.
      // /users 화면과 동일한 actions·endpoints 를 사용하므로 두 화면이 자동으로 동기화된다.
      // ★ Codex R2 #6: 허용 클라이언트가 정상 로드된 경우에만 per-app 예산을 건드린다.
      //   stale 전체허용 기준으로 쓰면 codex 등 잘못된 앱에 예산이 기록될 수 있다.
      let appError: string | null = null;
      if (isUserScope && clientsLoaded) {
        // allowed_clients 로 게이팅: 사용자가 허용된 앱만 set/clear. 허용되지 않은 앱은 손대지 않는다.
        const targets = ALL_CLIENTS.filter((c) => allowedClients.includes(c)).map((c) => ({
          client: c,
          value: budgets[c] ?? '',
          loaded: loadedBudgets[c] ?? '',
        }));

        // 검증: >= 0 의 유한수만 허용 (UserPanel 과 동일).
        for (const t of targets) {
          const trimmed = t.value.trim();
          if (trimmed === '') continue;
          const n = Number(trimmed);
          if (!Number.isFinite(n) || n < 0) {
            const msg = `유효하지 않은 앱 예산 값입니다 (${t.client}). 0 이상 숫자를 입력하세요.`;
            setError(msg);
            return;
          }
        }

        // 각 앱: 값이 있으면 set, 비었고 기존 예산이 있었으면 clear.
        // policy·thresholds 는 관리자가 이 다이얼로그에서 고른 값을 그대로 상속.
        for (const t of targets) {
          const trimmed = t.value.trim();
          if (trimmed !== '') {
            const res = await setUserClientBudgetAction(target.id, t.client, {
              max_budget_usd: trimmed,
              policy,
              alert_thresholds: thresholds,
            });
            if (!res.success && appError === null) appError = res.error;
          } else if (t.loaded.trim() !== '') {
            const res = await clearUserClientBudgetAction(target.id, t.client);
            if (!res.success && appError === null) appError = res.error;
          }
        }
      }

      if (appError) {
        // 총 예산은 저장됐으나 일부 앱 예산 저장 실패 — 성공을 잃지 않도록 알림으로 surface 하고 닫지 않는다.
        // 낙관적으로 입력한 값이 "저장됨"으로 보이지 않도록 서버에서 재동기화한다 (UserPanel 과 동일).
        const b = await getUserClientBudgetsAction(target.id);
        if (b.success) {
          const byClient = new Map(b.data.apps.map((a) => [a.client, a.max_budget_usd]));
          const next = emptyBudgets();
          for (const c of ALL_CLIENTS) next[c] = byClient.get(c) ?? '';
          setBudgets(next);
          setLoadedBudgets(next);
        }
        toast({
          type: 'success',
          message: `${target.name}의 예산이 $${numericValue.toFixed(2)}로 설정되었습니다. (앱별 예산 일부 실패)`,
          auto_dismiss_ms: 3000,
        });
        setError(`앱별 예산 저장 실패: ${appError}`);
        return;
      }

      toast({
        type: 'success',
        message: `${target.name}의 예산이 $${numericValue.toFixed(2)}로 설정되었습니다.`,
        auto_dismiss_ms: 3000,
      });
      onClose();
    });
  };

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div className="bg-background rounded-lg p-6 w-full max-w-md shadow-xl border border-border max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">예산 설정</h2>
          <button
            onClick={onClose}
            className="rounded-sm opacity-70 hover:opacity-100 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring transition-opacity"
            aria-label="닫기"
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>

        <p className="text-sm text-muted-foreground mb-4">
          대상: <span className="font-medium text-foreground">{target.name}</span>
        </p>

        <form onSubmit={handleSubmit} className="space-y-4">
          {/* Use Team Budget option (USER scope only) */}
          {isUserScope && (
            <div className="flex items-center justify-between rounded-md border border-border p-3 bg-muted/30">
              <div>
                <p className="text-sm font-medium">팀 예산 적용</p>
                <p className="text-xs text-muted-foreground">개인 예산을 삭제하고 팀 예산을 적용합니다</p>
              </div>
              <SpinnerButton
                type="button"
                onClick={handleUseTeamBudget}
                isLoading={isPending && useTeamBudget}
                className="bg-secondary text-secondary-foreground hover:bg-secondary/80 px-3 py-1.5 rounded-md text-xs font-medium"
              >
                팀 예산으로 전환
              </SpinnerButton>
            </div>
          )}

          {/* Budget Amount */}
          <div className="space-y-2">
            <label className="text-sm font-medium">최대 예산 (USD)</label>
            <div className="space-y-3">
              <input
                type="range"
                min={0}
                max={maxValue}
                step={0.01}
                value={numericValue}
                onChange={(e) => setValue(e.target.value)}
                className="w-full h-2 bg-gray-200 rounded-full appearance-none cursor-pointer accent-primary"
              />
              <div className="flex items-center gap-2">
                <span className="text-sm text-muted-foreground">$</span>
                <input
                  type="number"
                  min={0}
                  max={maxValue}
                  step={0.01}
                  value={value}
                  onChange={(e) => setValue(e.target.value)}
                  className="flex-1 rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                />
              </div>
              {target.parentLimit !== undefined && (
                <p className="text-xs text-muted-foreground">
                  상한: ${target.parentLimit.toFixed(2)}
                </p>
              )}
            </div>
          </div>

          {/* Policy Selection */}
          <div className="space-y-2">
            <label className="text-sm font-medium">초과 시 정책</label>
            <div className="space-y-1.5">
              {POLICY_OPTIONS.map(opt => (
                <label key={opt.value} className="flex items-start gap-2 cursor-pointer p-2 rounded-md hover:bg-accent">
                  <input
                    type="radio"
                    name="policy"
                    value={opt.value}
                    checked={policy === opt.value}
                    onChange={() => setPolicy(opt.value)}
                    className="mt-0.5 h-4 w-4"
                  />
                  <div>
                    <span className="text-sm font-medium">{opt.label}</span>
                    <p className="text-xs text-muted-foreground">{opt.desc}</p>
                  </div>
                </label>
              ))}
            </div>
          </div>

          {/* Alert Thresholds */}
          <div className="space-y-2">
            <label className="text-sm font-medium">알림 임계값 (%)</label>
            <p className="text-xs text-muted-foreground">예산 사용률이 임계값에 도달하면 알림을 발송합니다.</p>
            <div className="flex flex-wrap gap-1.5">
              {thresholds.map(t => (
                <span key={t} className="inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs font-medium bg-primary/10 text-primary">
                  {t}%
                  <button
                    type="button"
                    onClick={() => removeThreshold(t)}
                    className="text-primary/60 hover:text-primary"
                  >
                    &times;
                  </button>
                </span>
              ))}
            </div>
            <div className="flex items-center gap-2">
              <input
                type="number"
                min={1}
                max={100}
                value={newThreshold}
                onChange={(e) => setNewThreshold(e.target.value)}
                className="w-20 rounded-md border border-input bg-background px-2 py-1.5 text-sm text-center"
              />
              <span className="text-xs text-muted-foreground">%</span>
              <button
                type="button"
                onClick={addThreshold}
                className="text-sm text-primary hover:underline"
              >
                추가
              </button>
            </div>
          </div>

          {/* Per-app budgets (USER scope only, allowed_clients-gated) */}
          {isUserScope && (
            <div className="space-y-2 rounded-md border border-border p-3">
              <label className="text-sm font-medium">앱별 예산 (USD/월)</label>
              <p className="text-xs text-muted-foreground">
                Claude Code · Cowork · Codex 별 개별 예산입니다. 사용자 관리 화면과 동일하게 적용됩니다.
              </p>
              {isAppLoadPending ? (
                <div className="text-xs text-muted-foreground py-1">로딩 중…</div>
              ) : (
                <div className="space-y-3 pt-1">
                  {allowedClients.length < ALL_CLIENTS.length && (
                    <p className="text-xs text-muted-foreground">
                      이 사용자는 {allowedClients.map((c) => CLIENT_LABELS[c]).join(' · ')}만 허용됨
                    </p>
                  )}
                  {ALL_CLIENTS.filter((c) => allowedClients.includes(c)).map((c) => (
                    <div key={c}>
                      <label className="block text-xs text-muted-foreground mb-1">
                        {CLIENT_LABELS[c]}
                      </label>
                      <input
                        type="number"
                        min={0}
                        step={0.01}
                        value={budgets[c] ?? ''}
                        onChange={(e) =>
                          setBudgets((prev) => ({ ...prev, [c]: e.target.value }))
                        }
                        disabled={isPending}
                        placeholder="미설정"
                        className="flex-1 w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-50"
                      />
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          <FormError error={error} />

          <div className="flex items-center justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              disabled={isPending}
              className="inline-flex items-center justify-center rounded-md border border-border bg-background px-4 py-2 text-sm font-medium hover:bg-accent transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-50"
            >
              취소
            </button>
            <SpinnerButton type="submit" isLoading={isPending}>
              저장
            </SpinnerButton>
          </div>
        </form>
      </div>
    </div>
  );
}