'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useState, useTransition } from 'react';
import { RefreshCw, Loader2, X } from 'lucide-react';
import {
  previewPriceSyncAction,
  applyPriceSyncAction,
  type PriceSyncPreview,
} from '@/lib/actions/models';
import { useToast } from '@/components/common/ToastProvider';
import { Table, THead, TBody, Tr, Th, Td } from '@/components/common/Table';

/**
 * AWS Price List 단가 동기화 버튼 + diff 미리보기/승인 다이얼로그.
 *
 * 흐름(자동적용 금지): 버튼 → preview(읽기) → diff 표 → 변경분 선택 → 적용(승인).
 * 소스는 AWS Price List API(서버), AgentCore Gateway 아님. 적용은 기존 set_pricing
 * 경로라 시계열·감사·캐시무효화가 보존된다.
 */
export function PriceSyncButton() {
  const { toast } = useToast();
  const [open, setOpen] = useState(false);
  const [preview, setPreview] = useState<PriceSyncPreview | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(false);
  const [applying, startApply] = useTransition();

  async function openAndPreview() {
    setOpen(true);
    setPreview(null);
    setLoading(true);
    const res = await previewPriceSyncAction();
    setLoading(false);
    if (!res.success) {
      toast({ type: 'error', message: res.error || '미리보기 실패', auto_dismiss_ms: 5000 });
      setOpen(false);
      return;
    }
    setPreview(res.data);
    // 기본 선택 = 변경분 전체(matched && changed)
    setSelected(new Set(res.data.diffs.filter((d) => d.matched && d.changed).map((d) => d.alias)));
  }

  function toggle(alias: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(alias)) next.delete(alias);
      else next.add(alias);
      return next;
    });
  }

  function apply() {
    const aliases = [...selected];
    if (!aliases.length) {
      toast({ type: 'error', message: '적용할 모델을 선택하세요', auto_dismiss_ms: 4000 });
      return;
    }
    startApply(async () => {
      const res = await applyPriceSyncAction(aliases);
      if (!res.success) {
        toast({ type: 'error', message: res.error || '적용 실패', auto_dismiss_ms: 5000 });
        return;
      }
      const { applied, skipped, errors } = res.data;
      toast({ type: 'success', message: `적용 ${applied.length}건${skipped.length ? `, 스킵 ${skipped.length}` : ''}`, auto_dismiss_ms: 4000 });
      if (errors.length) toast({ type: 'error', message: `오류 ${errors.length}건: ${errors[0]}`, auto_dismiss_ms: 6000 });
      setOpen(false);
    });
  }

  const changedDiffs = preview?.diffs.filter((d) => d.matched && d.changed) ?? [];
  const unchanged = preview?.diffs.filter((d) => d.matched && !d.changed).length ?? 0;
  const unmatched = preview?.diffs.filter((d) => !d.matched) ?? [];

  return (
    <>
      <button
        onClick={openAndPreview}
        className="inline-flex items-center justify-center gap-2 rounded-md border border-border bg-card px-3 py-2 text-sm font-medium transition-colors hover:bg-accent/50 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        title="AWS Price List 공식 단가와 비교해 동기화(승인형)"
      >
        <RefreshCw size={15} />
        AWS 단가 동기화
      </button>

      {open && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
          <div className="glass max-h-[85vh] w-full max-w-3xl overflow-hidden rounded-apple flex flex-col">
            <div className="flex items-center justify-between border-b border-border px-5 py-3">
              <h2 className="text-base font-semibold">AWS 공식 단가 동기화</h2>
              <button onClick={() => setOpen(false)} className="text-muted-foreground hover:text-foreground">
                <X size={18} />
              </button>
            </div>

            <div className="overflow-auto px-5 py-4">
              {loading && (
                <div className="flex items-center gap-2 py-8 text-sm text-muted-foreground">
                  <Loader2 size={16} className="animate-spin" /> AWS Price List 조회 중…
                </div>
              )}

              {preview && (
                <>
                  <p className="mb-3 text-xs text-muted-foreground">
                    소스: AWS Price List API ({preview.region}) · 매칭 {preview.matched_count} · 변경{' '}
                    {preview.changed_count} · 동일 {unchanged} · 미매칭 {unmatched.length}
                  </p>

                  {changedDiffs.length === 0 ? (
                    <p className="py-6 text-sm text-muted-foreground">
                      변경된 단가가 없습니다(현재 DB 단가가 AWS 공식가와 일치).
                    </p>
                  ) : (
                    <Table density="compact">
                      <THead>
                        <Tr>
                          <Th>적용</Th>
                          <Th>모델</Th>
                          <Th numeric>입력(현재→AWS)</Th>
                          <Th numeric>출력(현재→AWS)</Th>
                          <Th>비고</Th>
                        </Tr>
                      </THead>
                      <TBody>
                        {changedDiffs.map((d) => (
                          <Tr key={d.alias}>
                            <Td>
                              <input
                                type="checkbox"
                                checked={selected.has(d.alias)}
                                onChange={() => toggle(d.alias)}
                              />
                            </Td>
                            <Td>{d.alias}</Td>
                            <Td numeric>{fmtChange(d.current?.input_price_per_1k_tokens, d.proposed_input_per_1k)}</Td>
                            <Td numeric>{fmtChange(d.current?.output_price_per_1k_tokens, d.proposed_output_per_1k)}</Td>
                            <Td>{d.note ? <span className="text-amber-600 dark:text-amber-400 text-[11px]">{d.note}</span> : ''}</Td>
                          </Tr>
                        ))}
                      </TBody>
                    </Table>
                  )}

                  {unmatched.length > 0 && (
                    <p className="mt-3 text-[11px] text-muted-foreground">
                      미매칭(AWS 단가 미발견, 적용 불가): {unmatched.map((d) => d.alias).join(', ')}
                    </p>
                  )}
                </>
              )}
            </div>

            <div className="flex items-center justify-end gap-2 border-t border-border px-5 py-3">
              <button
                onClick={() => setOpen(false)}
                className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-accent/50"
              >
                취소
              </button>
              <button
                onClick={apply}
                disabled={applying || !preview || selected.size === 0}
                className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
              >
                {applying && <Loader2 size={14} className="animate-spin" />}
                선택 {selected.size}건 적용
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

function fmtChange(current: string | undefined, proposed: string | null): string {
  const c = current != null ? `$${Number(current).toFixed(6)}` : '—';
  const p = proposed != null ? `$${Number(proposed).toFixed(6)}` : '—';
  return `${c} → ${p}`;
}
