"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Download, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  formatUsdPer1m,
  missingTiers,
  rateCandidateNote,
  rateSourceLabel,
} from "@/app/insights/insightsFormat.mjs";
import {
  deleteRate,
  fetchPublishedRates,
  fetchRateCard,
  InsightsApiError,
  putRates,
  type RateCandidate,
  type RateCardOverview,
  type RateEntryInput,
  type RateRow,
  type RepriceReport,
} from "@/lib/insights";
import { EmptyPlot, Plot, PlotCell } from "./charts";

/**
 * The model rate card, as the admin fills it.
 *
 * The ledger prices a turn only with a rate it can stand behind; a model the
 * committed table lacks is "요율 미등록" until the bill has shown the same value
 * on two consecutive days. That rule is right and it is slow. This widget is the
 * other half: what a source can state today — the bill after one clean day, the
 * Price List where it publishes — is offered as a candidate to accept, and what
 * no source can state is typed in. Either way the entry is dated, lands in the
 * same overlay the bill's learner writes to (so the bill can still correct it),
 * and every turn from its effective date is repriced before the request returns.
 *
 * Lives on the Settings page, not the dashboard, because it is a form and the
 * dashboard is figures. Admin-only: a 403 renders as such, not as an error.
 */

const TIERS = ["input", "output", "cache_read", "cache_write"] as const;
const TIER_LABEL: Record<string, string> = {
  input: "입력",
  output: "출력",
  cache_read: "캐시 읽기",
  cache_write: "캐시 쓰기",
};
const ROUTING_LABEL: Record<string, string> = { global: "global", regional: "regional" };

type Draft = { usd: Record<string, string>; effectiveFrom: string };
type State = "loading" | "ready" | "forbidden" | "error";

const rowKey = (row: { family: string; routing: string }) => `${row.family}|${row.routing}`;
const candidateKey = (c: { family: string; routing: string; tier: string }) => `${c.family}|${c.routing}|${c.tier}`;

function repriceSentence(report: RepriceReport): string {
  const parts: string[] = [];
  if (report.newly_priced > 0) parts.push(`${report.newly_priced.toLocaleString("ko-KR")}턴 비용 채움`);
  const moved = report.repriced - report.newly_priced - report.unpriced;
  if (moved > 0) parts.push(`${moved.toLocaleString("ko-KR")}턴 다시 계산`);
  if (report.unpriced > 0) parts.push(`${report.unpriced.toLocaleString("ko-KR")}턴 비용 해제`);
  if (report.still_unpriced > 0) parts.push(`요율 미등록 ${report.still_unpriced.toLocaleString("ko-KR")}턴 남음`);
  return parts.length > 0 ? parts.join(" · ") : "바뀐 턴 없음";
}

export function RateCardPanel({ days, onChanged }: { days: number; onChanged?: () => void }) {
  const [data, setData] = useState<RateCardOverview | null>(null);
  const [state, setState] = useState<State>("loading");
  const [message, setMessage] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [published, setPublished] = useState<RateCandidate[] | null>(null);
  const [publishedCount, setPublishedCount] = useState<number | null>(null);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [newRow, setNewRow] = useState<{ family: string; routing: string } | null>(null);

  const load = useCallback(async () => {
    try {
      const value = await fetchRateCard(days);
      setData(value);
      setState("ready");
      // Rows still missing a tier open with their inputs ready; the rest closed.
      setDrafts((current) => {
        const next: Record<string, Draft> = {};
        for (const row of value.rows) {
          const key = rowKey(row);
          if (current[key]) {
            next[key] = current[key];
          } else if (!row.complete) {
            next[key] = {
              usd: {},
              effectiveFrom: row.unpriced_since ?? value.window.end_date,
            };
          }
        }
        return next;
      });
    } catch (error) {
      if (error instanceof InsightsApiError && error.status === 403) {
        setState("forbidden");
        return;
      }
      setMessage(error instanceof Error ? error.message : String(error));
      setState("error");
    }
  }, [days]);

  useEffect(() => {
    setState("loading");
    void load();
  }, [load]);

  const candidates = useMemo(() => {
    const out = new Map<string, RateCandidate>();
    for (const c of data?.candidates ?? []) out.set(candidateKey(c), c);
    for (const c of published ?? []) if (!out.has(candidateKey(c))) out.set(candidateKey(c), c);
    return [...out.values()];
  }, [data, published]);

  // Every write goes through here: one busy flag, one notice, one error line,
  // and the summary beside this widget refetched once the server has repriced.
  // Returns whether it succeeded so a caller can keep a draft on failure.
  const run = useCallback(
    async (work: () => Promise<{ repriced: RepriceReport } | void>, done: string): Promise<boolean> => {
      setBusy(true);
      setMessage(null);
      try {
        const result = await work();
        setNotice(result ? `${done} · ${repriceSentence(result.repriced)}` : done);
        await load();
        onChanged?.();
        return true;
      } catch (error) {
        setMessage(error instanceof Error ? error.message : String(error));
        return false;
      } finally {
        setBusy(false);
      }
    },
    [load, onChanged],
  );

  const accept = (items: RateCandidate[]) =>
    run(
      () =>
        putRates(
          items.map((c) => ({
            family: c.family,
            routing: c.routing,
            tier: c.tier,
            usd_per_1m: c.usd_per_1m,
            effective_from: c.effective_from ?? data?.window.end_date ?? "",
            source: c.source,
          })),
        ),
      `${items.length}개 요율 등록`,
    );

  const fetchPublished = () =>
    run(async () => {
      const result = await fetchPublishedRates();
      setPublished(result.candidates);
      setPublishedCount(result.published);
    }, "Price List 조회");

  const saveDraft = (row: { family: string; routing: string }) => {
    const draft = drafts[rowKey(row)];
    if (!draft) return;
    const entries: RateEntryInput[] = TIERS.filter((tier) => (draft.usd[tier] ?? "").trim() !== "").map((tier) => ({
      family: row.family,
      routing: row.routing,
      tier,
      usd_per_1m: draft.usd[tier].trim(),
      effective_from: draft.effectiveFrom,
    }));
    if (entries.length === 0) {
      setMessage("입력한 요율이 없습니다.");
      return;
    }
    // The draft is dropped before the save so the reload after it decides afresh:
    // a row still missing a tier reopens with empty inputs, a complete one closes.
    // On failure the draft comes back untouched.
    setDrafts((current) => {
      const next = { ...current };
      delete next[rowKey(row)];
      return next;
    });
    void run(() => putRates(entries), `${entries.length}개 요율 저장`).then((ok) => {
      if (ok) {
        setNewRow(null);
        return;
      }
      setDrafts((current) => ({ ...current, [rowKey(row)]: draft }));
    });
  };

  const setDraft = (key: string, patch: Partial<Draft> | { tier: string; value: string }) =>
    setDrafts((current) => {
      const base = current[key] ?? { usd: {}, effectiveFrom: data?.window.end_date ?? "" };
      if ("tier" in patch) {
        return { ...current, [key]: { ...base, usd: { ...base.usd, [patch.tier]: patch.value } } };
      }
      return { ...current, [key]: { ...base, ...patch } };
    });

  if (state === "forbidden") {
    return (
      <PlotCell>
        <EmptyPlot label="모델 요율은 관리자만 관리할 수 있습니다." />
      </PlotCell>
    );
  }
  if (state === "error") {
    return (
      <PlotCell>
        <EmptyPlot label={`요율표를 읽지 못했습니다: ${message ?? "알 수 없는 오류"}`} />
      </PlotCell>
    );
  }
  if (state === "loading" || !data) {
    return (
      <PlotCell>
        <EmptyPlot label="요율표를 읽는 중…" />
      </PlotCell>
    );
  }

  const rows: RateRow[] = [...data.rows];
  if (newRow) {
    rows.push({
      family: newRow.family,
      routing: newRow.routing,
      models: [],
      turns: 0,
      unpriced_turns: 0,
      unpriced_since: null,
      tiers: { input: null, output: null, cache_read: null, cache_write: null },
      complete: false,
    });
  }

  return (
    <>
      <PlotCell>
        <Plot
          title={`모델 요율 · 요율표 ${data.version}`}
          hint="이 플랫폼이 실행한 모델의 1M 토큰당 요율입니다. 빈 칸은 AWS 청구서나 Price List 에서 받아오거나 직접 입력합니다. 저장하면 유효 시작일부터의 턴 비용이 바로 다시 계산되고, 이후 청구서가 이틀 연속 다른 값을 보이면 그 값으로 교정됩니다."
          actions={
            <Button variant="outline" size="sm" disabled={busy} onClick={() => void fetchPublished()}>
              <Download className="mr-1 size-3.5" />
              Price List 받아오기
            </Button>
          }
        >
          {(notice || message || publishedCount !== null) && (
            <div className="mb-2 space-y-1 text-xxs">
              {notice && <p className="text-muted-foreground">{notice}</p>}
              {publishedCount !== null && (
                <p className="text-muted-foreground">
                  Price List 게시 행 {publishedCount}개
                  {published && published.length === 0 ? " · 요율표에 없거나 다른 값은 없습니다" : ""}
                </p>
              )}
              {message && <p className="text-destructive">{message}</p>}
            </div>
          )}
          {rows.length === 0 ? (
            <EmptyPlot label="이 기간에 실행된 모델이 없습니다." />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-border text-muted-foreground">
                    <th className="px-2 py-1 text-left font-medium">모델</th>
                    <th className="px-2 py-1 text-left font-medium">라우팅</th>
                    {TIERS.map((tier) => (
                      <th key={tier} className="px-2 py-1 text-right font-medium">
                        {TIER_LABEL[tier]} $/1M
                      </th>
                    ))}
                    <th className="whitespace-nowrap px-2 py-1 text-left font-medium">유효 시작일</th>
                    <th className="px-2 py-1 text-right font-medium" />
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => {
                    const key = rowKey(row);
                    const draft = drafts[key];
                    const editing = Boolean(draft);
                    return (
                      <tr key={key} className="border-b border-border/50 align-top last:border-0">
                        <td className="px-2 py-1.5">
                          <div className="font-mono text-xxs">{row.family}</div>
                          {row.models.length > 0 && (
                            <div className="text-xxs text-muted-foreground">{row.models.join(", ")}</div>
                          )}
                          {row.unpriced_turns > 0 && (
                            <div className="text-xxs text-warning">
                              요율 미등록 {row.unpriced_turns.toLocaleString("ko-KR")}턴
                              {row.unpriced_since ? ` · ${row.unpriced_since} 부터` : ""}
                            </div>
                          )}
                        </td>
                        <td className="px-2 py-1.5">{ROUTING_LABEL[row.routing] ?? row.routing}</td>
                        {TIERS.map((tier) => {
                          const cell = row.tiers[tier];
                          const open = editing && (missingTiers(row).includes(tier) || draft.usd[tier] !== undefined);
                          return (
                            <td key={tier} className="px-2 py-1.5 text-right tabular-nums">
                              {cell && !open ? (
                                <button
                                  type="button"
                                  className="text-right hover:underline"
                                  title="새 요율을 입력합니다 (가격 변경)"
                                  onClick={() => setDraft(key, { tier, value: cell.usd_per_1m })}
                                >
                                  <div>{formatUsdPer1m(cell.usd_per_1m)}</div>
                                  <div className="text-xxs text-muted-foreground">
                                    {rateSourceLabel(cell.source)}
                                    {cell.effective_from ? ` · ${cell.effective_from}` : ""}
                                  </div>
                                </button>
                              ) : (
                                <Input
                                  inputMode="decimal"
                                  placeholder={cell ? cell.usd_per_1m : "—"}
                                  className="h-7 w-24 text-right text-xs"
                                  value={draft?.usd[tier] ?? ""}
                                  disabled={busy}
                                  onChange={(event) => setDraft(key, { tier, value: event.target.value })}
                                />
                              )}
                            </td>
                          );
                        })}
                        <td className="px-2 py-1.5">
                          {editing ? (
                            <Input
                              type="date"
                              className="h-7 w-36 text-xs"
                              value={draft.effectiveFrom}
                              max={data.window.end_date}
                              disabled={busy}
                              onChange={(event) => setDraft(key, { effectiveFrom: event.target.value })}
                            />
                          ) : (
                            <span className="text-xxs text-muted-foreground">—</span>
                          )}
                        </td>
                        <td className="px-2 py-1.5 text-right">
                          {editing ? (
                            <div className="flex justify-end gap-1">
                              <Button size="sm" variant="default" className="h-7" disabled={busy} onClick={() => saveDraft(row)}>
                                저장
                              </Button>
                              <Button
                                size="sm"
                                variant="ghost"
                                className="h-7"
                                disabled={busy}
                                onClick={() => {
                                  setDrafts((current) => {
                                    const next = { ...current };
                                    delete next[key];
                                    return next;
                                  });
                                  if (newRow && rowKey(newRow) === key) setNewRow(null);
                                }}
                              >
                                취소
                              </Button>
                            </div>
                          ) : (
                            <Button
                              size="sm"
                              variant="ghost"
                              className="h-7"
                              disabled={busy}
                              onClick={() => setDraft(key, { effectiveFrom: data.window.end_date })}
                            >
                              직접 입력
                            </Button>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              {!newRow && (
                <div className="mt-2 flex items-center gap-2 text-xxs text-muted-foreground">
                  <span>아직 실행하지 않은 모델의 요율을 미리 등록:</span>
                  <Input
                    placeholder="claude-opus-5-5"
                    className="h-7 w-44 font-mono text-xxs"
                    disabled={busy}
                    onKeyDown={(event) => {
                      if (event.key !== "Enter") return;
                      const family = (event.target as HTMLInputElement).value.trim().toLowerCase();
                      if (!family) return;
                      const pending = { family, routing: "global" };
                      setNewRow(pending);
                      setDraft(rowKey(pending), { effectiveFrom: data.window.end_date });
                      (event.target as HTMLInputElement).value = "";
                    }}
                  />
                  <span>Enter · global 라우팅으로 열립니다</span>
                </div>
              )}
            </div>
          )}
        </Plot>
      </PlotCell>

      {candidates.length > 0 && (
        <PlotCell>
          <Plot
            title="받아올 수 있는 요율"
            hint="AWS 청구서에 하루라도 정확히 나눠진 요율과 Price List 게시값 중 요율표에 없거나 다른 것입니다. 등록하면 그 유효 시작일부터 반영됩니다. 자동 학습은 이틀 연속 관측을 기다리므로, 하루 관측만 있는 값은 사람이 확인하고 등록합니다."
            actions={
              <Button size="sm" variant="outline" disabled={busy} onClick={() => void accept(candidates)}>
                모두 등록
              </Button>
            }
          >
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border text-muted-foreground">
                  <th className="px-2 py-1 text-left font-medium">모델</th>
                  <th className="px-2 py-1 text-left font-medium">라우팅</th>
                  <th className="px-2 py-1 text-left font-medium">tier</th>
                  <th className="px-2 py-1 text-right font-medium">받아온 값 $/1M</th>
                  <th className="px-2 py-1 text-right font-medium">현재 요율표</th>
                  <th className="px-2 py-1 text-left font-medium">근거</th>
                  <th className="px-2 py-1 text-left font-medium">유효 시작일</th>
                  <th className="px-2 py-1" />
                </tr>
              </thead>
              <tbody>
                {candidates.map((c) => (
                  <tr key={candidateKey(c)} className="border-b border-border/50 last:border-0">
                    <td className="px-2 py-1 font-mono text-xxs">{c.family}</td>
                    <td className="px-2 py-1">{c.routing}</td>
                    <td className="px-2 py-1">{TIER_LABEL[c.tier] ?? c.tier}</td>
                    <td className="px-2 py-1 text-right tabular-nums">{formatUsdPer1m(c.usd_per_1m)}</td>
                    <td className="px-2 py-1 text-right tabular-nums text-muted-foreground">
                      {c.current_usd_per_1m ? formatUsdPer1m(c.current_usd_per_1m) : "—"}
                    </td>
                    <td className="px-2 py-1 text-muted-foreground">{rateCandidateNote(c)}</td>
                    <td className="px-2 py-1 tabular-nums">{c.effective_from ?? data.window.end_date}</td>
                    <td className="px-2 py-1 text-right">
                      <Button size="sm" variant="ghost" className="h-7" disabled={busy} onClick={() => void accept([c])}>
                        등록
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Plot>
        </PlotCell>
      )}

      {data.entries.length > 0 && (
        <PlotCell>
          <Plot
            title="등록된 요율"
            hint="요율표 위에 날짜별로 얹힌 항목입니다. 지우면 그 항목이 매긴 턴의 비용이 다시 비고, 다른 요율이 있으면 그 값으로 계산됩니다."
          >
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border text-muted-foreground">
                  <th className="px-2 py-1 text-left font-medium">모델</th>
                  <th className="px-2 py-1 text-left font-medium">라우팅</th>
                  <th className="px-2 py-1 text-left font-medium">tier</th>
                  <th className="px-2 py-1 text-left font-medium">유효 시작일</th>
                  <th className="px-2 py-1 text-right font-medium">$/1M</th>
                  <th className="px-2 py-1 text-left font-medium">출처</th>
                  <th className="px-2 py-1 text-left font-medium">등록 시각</th>
                  <th className="px-2 py-1" />
                </tr>
              </thead>
              <tbody>
                {data.entries.map((entry) => (
                  <tr key={entry.key} className="border-b border-border/50 last:border-0">
                    <td className="px-2 py-1 font-mono text-xxs">{entry.family}</td>
                    <td className="px-2 py-1">{entry.routing}</td>
                    <td className="px-2 py-1">{TIER_LABEL[entry.tier] ?? entry.tier}</td>
                    <td className="px-2 py-1 tabular-nums">{entry.effective_from}</td>
                    <td className="px-2 py-1 text-right tabular-nums">{formatUsdPer1m(entry.usd_per_1m)}</td>
                    <td className="px-2 py-1 text-muted-foreground">{rateSourceLabel(entry.source)}</td>
                    <td className="px-2 py-1 tabular-nums text-muted-foreground">
                      {entry.registered_at ? entry.registered_at.slice(0, 16).replace("T", " ") : "—"}
                    </td>
                    <td className="px-2 py-1 text-right">
                      <Button
                        size="sm"
                        variant="ghost"
                        className="h-7 text-destructive"
                        disabled={busy}
                        title="이 항목을 지우고 해당 턴을 다시 계산합니다"
                        onClick={() => void run(() => deleteRate(entry.key), "요율 삭제")}
                      >
                        <Trash2 className="size-3.5" />
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Plot>
        </PlotCell>
      )}
    </>
  );
}
