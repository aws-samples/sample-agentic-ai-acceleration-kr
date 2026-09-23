'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useCallback, useState } from 'react';
import { useRouter } from 'next/navigation';
import { useTranslations } from 'next-intl';
import { previewAwsPricingAction, syncAwsPricingAction } from '@/lib/actions/models';
import type { AwsPricePreviewResponse } from '@/types/api';

// fetch ≠ apply: 이 다이얼로그는 preview(읽기 전용 drift)를 먼저 보여주고, 운영자가 체크한
// alias 만 sync(반영)한다. 가격은 곧 과금·차단이라 자동 반영하지 않는다 — 버튼 하나로
// 전부 덮어쓰지 않도록 변경 있는 행만 기본 체크하고, 반영은 명시적 클릭이 필요하다.

export function AwsPriceSyncButton() {
  const t = useTranslations('models.awsSync');
  const router = useRouter();
  const [isOpen, setIsOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [preview, setPreview] = useState<AwsPricePreviewResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [region, setRegion] = useState('us-east-1');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [syncing, setSyncing] = useState(false);
  const [result, setResult] = useState<{ synced: string[]; skipped: string[] } | null>(null);

  const load = useCallback(async (regionCode: string) => {
    setLoading(true);
    setError(null);
    setResult(null);
    const res = await previewAwsPricingAction(regionCode);
    if (res.success) {
      setPreview(res.data);
      // 변경이 있는(=drift) matched 행만 기본 선택. 변경 없는 행은 반영해도 no-op 이라 뺀다.
      setSelected(
        new Set(res.data.items.filter((i) => i.matched && i.changes.length > 0).map((i) => i.alias))
      );
    } else {
      setPreview(null);
      setError(res.error);
    }
    setLoading(false);
  }, []);

  const open = useCallback(() => {
    setIsOpen(true);
    void load(region);
  }, [load, region]);

  const toggle = (alias: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(alias)) next.delete(alias);
      else next.add(alias);
      return next;
    });
  };

  const apply = async () => {
    setSyncing(true);
    setError(null);
    const res = await syncAwsPricingAction([...selected], preview?.region_code ?? region);
    if (res.success) {
      setResult(res.data);
      router.refresh();
    } else {
      setError(res.error);
    }
    setSyncing(false);
  };

  const driftItems = preview?.items.filter((i) => i.matched && i.changes.length > 0) ?? [];
  const cleanCount = preview?.items.filter((i) => i.matched && i.changes.length === 0).length ?? 0;
  const unmatched = preview?.items.filter((i) => !i.matched) ?? [];

  return (
    <>
      <button
        onClick={open}
        className="inline-flex items-center justify-center gap-2 rounded-md px-4 py-2 text-sm font-medium transition-colors border border-input bg-background shadow-sm hover:bg-accent focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
      >
        {t('button')}
      </button>

      {isOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
          <div className="w-full max-w-3xl max-h-[85vh] overflow-auto rounded-lg bg-background p-6 shadow-lg">
            <div className="mb-4 flex items-center justify-between">
              <h2 className="text-lg font-semibold">{t('title')}</h2>
              <button
                onClick={() => setIsOpen(false)}
                className="text-muted-foreground hover:text-foreground"
                aria-label={t('close')}
              >
                ✕
              </button>
            </div>

            <p className="mb-4 text-sm text-muted-foreground">{t('description')}</p>

            <div className="mb-4 flex items-center gap-2">
              <label className="text-sm font-medium">{t('region')}</label>
              <input
                value={region}
                onChange={(e) => setRegion(e.target.value)}
                className="rounded-md border border-input bg-background px-2 py-1 text-sm"
              />
              <button
                onClick={() => void load(region)}
                disabled={loading}
                className="rounded-md border border-input px-3 py-1 text-sm hover:bg-accent disabled:opacity-50"
              >
                {t('reload')}
              </button>
            </div>

            {loading && <p className="text-sm text-muted-foreground">{t('loading')}</p>}
            {error && <p className="text-sm text-destructive">{error}</p>}

            {result && (
              <div className="mb-4 rounded-md border border-input p-3 text-sm">
                <p className="font-medium text-green-600 dark:text-green-400">
                  {t('syncedCount', { count: result.synced.length })}
                </p>
                {result.skipped.length > 0 && (
                  <p className="text-muted-foreground">
                    {t('skippedCount', { count: result.skipped.length })}: {result.skipped.join(', ')}
                  </p>
                )}
              </div>
            )}

            {preview && !loading && (
              <>
                {driftItems.length === 0 && (
                  <p className="text-sm text-muted-foreground">{t('noDrift')}</p>
                )}
                {driftItems.map((item) => (
                  <div key={item.alias} className="mb-3 rounded-md border border-input p-3">
                    <label className="flex items-center gap-2 font-medium">
                      <input
                        type="checkbox"
                        checked={selected.has(item.alias)}
                        onChange={() => toggle(item.alias)}
                      />
                      <span>{item.alias}</span>
                      <span className="text-xs text-muted-foreground">
                        ({item.aws_endpoint} · {item.aws_region_code})
                      </span>
                    </label>
                    {item.note && (
                      <p className="mt-1 text-xs text-amber-600 dark:text-amber-400">⚠ {item.note}</p>
                    )}
                    <table className="mt-2 w-full text-xs">
                      <thead>
                        <tr className="text-left text-muted-foreground">
                          <th className="py-1">{t('field')}</th>
                          <th className="py-1">{t('current')}</th>
                          <th className="py-1">{t('awsValue')}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {item.changes.map((c) => (
                          <tr key={c.field}>
                            <td className="py-0.5 font-mono">{c.field}</td>
                            <td className="py-0.5">{c.current ?? '—'}</td>
                            <td className="py-0.5 font-medium">{c.aws ?? '—'}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ))}

                {(cleanCount > 0 || unmatched.length > 0) && (
                  <p className="mt-2 text-xs text-muted-foreground">
                    {t('inSync', { count: cleanCount })}
                    {unmatched.length > 0 &&
                      ` · ${t('unmatched', { count: unmatched.length })}: ${unmatched.map((i) => i.alias).join(', ')}`}
                  </p>
                )}
              </>
            )}

            <div className="mt-6 flex justify-end gap-2">
              <button
                onClick={() => setIsOpen(false)}
                className="rounded-md border border-input px-4 py-2 text-sm hover:bg-accent"
              >
                {t('cancel')}
              </button>
              <button
                onClick={() => void apply()}
                disabled={syncing || selected.size === 0}
                className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground shadow hover:bg-primary/90 disabled:opacity-50"
              >
                {syncing ? t('applying') : t('apply', { count: selected.size })}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
