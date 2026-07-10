'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useState, useTransition, useEffect } from 'react';
import { useToast } from '@/components/common/ToastProvider';
import { SpinnerButton } from '@/components/common/SpinnerButton';
import {
  getDowngradeConfigAction,
  setDowngradeConfigAction,
  deleteDowngradeConfigAction,
} from '@/lib/actions/budgets';
import type { ModelListItem } from '@/types/entities';

interface DowngradeRuleForm {
  from_model_alias: string;
  to_model_alias: string;
  threshold_pct: string;
}

interface AutoDowngradeConfigProps {
  scopeType: 'TEAM' | 'USER';
  scopeId: string;
  scopeName: string;
  models: ModelListItem[];
}

export function AutoDowngradeConfig({ scopeType, scopeId, scopeName, models }: AutoDowngradeConfigProps) {
  const { toast } = useToast();
  const [isPending, startTransition] = useTransition();
  const [enabled, setEnabled] = useState(false);
  const [rules, setRules] = useState<DowngradeRuleForm[]>([]);
  const [loaded, setLoaded] = useState(false);

  const activeModels = models.filter(m => m.is_active);

  useEffect(() => {
    if (!scopeId) return;
    startTransition(async () => {
      const result = await getDowngradeConfigAction(scopeType, scopeId);
      if (result.success) {
        setEnabled(result.data.enabled);
        setRules(
          result.data.rules.map(r => ({
            from_model_alias: r.from_model_alias,
            to_model_alias: r.to_model_alias,
            threshold_pct: String(r.threshold_pct),
          })),
        );
      }
      setLoaded(true);
    });
  }, [scopeType, scopeId]);

  const addRule = () => {
    setRules(prev => [
      ...prev,
      {
        from_model_alias: activeModels[0]?.alias ?? '',
        to_model_alias: activeModels.length > 1 ? activeModels[1].alias : activeModels[0]?.alias ?? '',
        threshold_pct: '80',
      },
    ]);
  };

  const removeRule = (index: number) => {
    setRules(prev => prev.filter((_, i) => i !== index));
  };

  const updateRule = (index: number, field: keyof DowngradeRuleForm, value: string | number) => {
    setRules(prev =>
      prev.map((r, i) => (i === index ? { ...r, [field]: value } : r)),
    );
  };

  const handleSave = () => {
    if (rules.length === 0) {
      toast({ type: 'error', message: '최소 1개 이상의 다운그레이드 규칙을 추가해야 합니다.', auto_dismiss_ms: 3000 });
      return;
    }
    for (const rule of rules) {
      if (rule.from_model_alias === rule.to_model_alias) {
        toast({ type: 'error', message: `소스와 타겟 모델이 동일합니다: ${rule.from_model_alias}`, auto_dismiss_ms: 3000 });
        return;
      }
    }
    startTransition(async () => {
      const result = await setDowngradeConfigAction(scopeType, scopeId, {
        enabled,
        rules: rules.map(r => ({ ...r, threshold_pct: parseInt(r.threshold_pct) || 0 })),
      });
      if (result.success) {
        toast({ type: 'success', message: '자동 다운그레이드 설정이 저장되었습니다.', auto_dismiss_ms: 3000 });
      } else {
        let msg = result.error;
        if (msg?.includes('Budget must be configured') || msg?.includes('must be greater than 0 for downgrade')) {
          msg = '이 팀에 예산이 설정되어 있지 않습니다. 예산을 먼저 설정해주세요.';
        }
        toast({ type: 'error', message: msg, auto_dismiss_ms: 5000 });
      }
    });
  };

  const handleDisable = () => {
    startTransition(async () => {
      const result = await deleteDowngradeConfigAction(scopeType, scopeId);
      if (result.success) {
        setEnabled(false);
        setRules([]);
        toast({ type: 'success', message: '자동 다운그레이드 설정이 해제되었습니다.', auto_dismiss_ms: 3000 });
      } else {
        toast({ type: 'error', message: result.error, auto_dismiss_ms: 5000 });
      }
    });
  };

  if (!loaded) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground py-4">
        <div className="h-4 w-4 animate-spin rounded-full border-2 border-primary border-t-transparent" />
        설정 로딩 중...
      </div>
    );
  }

  return (
    <div className="space-y-4 glass rounded-apple p-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-sm font-semibold">자동 다운그레이드</h3>
          <p className="text-xs text-muted-foreground">
            {scopeName} — 예산 임계치 초과 시 자동으로 하위 모델로 라우팅
          </p>
        </div>
        <label className="flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={enabled}
            onChange={e => setEnabled(e.target.checked)}
            className="h-4 w-4 rounded border-gray-300"
          />
          <span className="text-sm">설정하기</span>
        </label>
      </div>

      {enabled && (
        <>
          {rules.length > 0 && (
            <div className="flex flex-wrap items-center gap-2 rounded-md bg-muted/30 p-3">
              <span className="text-xs font-medium text-muted-foreground">다운그레이드 체인:</span>
              {rules
                .sort((a, b) => (parseInt(a.threshold_pct) || 0) - (parseInt(b.threshold_pct) || 0))
                .map((rule, i) => (
                  <span key={i} className="flex items-center gap-1 text-xs">
                    <span className="badge badge-pink font-mono">
                      {rule.from_model_alias}
                    </span>
                    <span className="text-muted-foreground">&rarr;</span>
                    <span className="badge badge-teal font-mono">
                      {rule.to_model_alias}
                    </span>
                    <span className="text-muted-foreground">({rule.threshold_pct}%)</span>
                    {i < rules.length - 1 && <span className="text-muted-foreground mx-1">|</span>}
                  </span>
                ))}
            </div>
          )}

          <div className="space-y-2">
            {rules.map((rule, index) => (
              <div key={index} className="flex items-center gap-2 rounded-md border p-2">
                <select
                  value={rule.from_model_alias}
                  onChange={e => updateRule(index, 'from_model_alias', e.target.value)}
                  className="flex-1 rounded-md border border-input bg-background px-2 py-1.5 text-sm"
                >
                  {activeModels.map(m => (
                    <option key={m.alias} value={m.alias}>{m.alias}</option>
                  ))}
                </select>
                <span className="text-sm text-muted-foreground">&rarr;</span>
                <select
                  value={rule.to_model_alias}
                  onChange={e => updateRule(index, 'to_model_alias', e.target.value)}
                  className="flex-1 rounded-md border border-input bg-background px-2 py-1.5 text-sm"
                >
                  {activeModels.map(m => (
                    <option key={m.alias} value={m.alias}>{m.alias}</option>
                  ))}
                </select>
                <input
                  type="number"
                  min={1}
                  max={100}
                  value={rule.threshold_pct}
                  onChange={e => updateRule(index, 'threshold_pct', e.target.value)}
                  className="w-16 rounded-md border border-input bg-background px-2 py-1.5 text-sm text-center"
                />
                <span className="text-xs text-muted-foreground">%</span>
                <button
                  type="button"
                  onClick={() => removeRule(index)}
                  className="text-destructive hover:text-destructive/80 text-sm px-1"
                  title="삭제"
                >
                  &times;
                </button>
              </div>
            ))}
          </div>

          <button
            type="button"
            onClick={addRule}
            className="text-sm text-primary hover:underline"
          >
            + 규칙 추가
          </button>
        </>
      )}

      <div className="flex items-center gap-3 pt-2 border-t">
        <SpinnerButton
          onClick={handleSave}
          isLoading={isPending}
          disabled={!enabled && rules.length === 0}
          className="bg-primary text-primary-foreground hover:bg-primary/90 px-4 py-2 rounded-md text-sm font-medium disabled:opacity-50"
        >
          설정 저장
        </SpinnerButton>
        {enabled && (
          <button
            type="button"
            onClick={handleDisable}
            disabled={isPending}
            className="text-sm text-destructive hover:underline disabled:opacity-50"
          >
            설정 해제
          </button>
        )}
      </div>
    </div>
  );
}