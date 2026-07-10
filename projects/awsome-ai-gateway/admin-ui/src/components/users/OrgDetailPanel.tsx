'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useState, useEffect, useTransition } from 'react';
import type { OrgTreeNode, ModelListItem } from '@/types/entities';
import {
  forceReauthTeamAction,
  getUserAllowedClientsAction,
  setUserAllowedClientsAction,
  getUserClientBudgetsAction,
  setUserClientBudgetAction,
  clearUserClientBudgetAction,
  getUserAllowedModelsAction,
  setUserAllowedModelsAction,
} from '@/lib/actions/users';
import { listActiveModelsAction } from '@/lib/actions/models';
import { SpinnerButton } from '@/components/common/SpinnerButton';
import { useToast } from '@/components/common/ToastProvider';
import { Badge, type BadgeTone } from '@/components/common/Badge';

interface OrgDetailPanelProps {
  node: OrgTreeNode | null;
}

const ROLE_LABEL: Record<string, string> = {
  ADMIN: '관리자',
  TEAM_LEADER: '팀장',
  DEVELOPER: '개발자',
};

const ROLE_TONE: Record<string, BadgeTone> = {
  ADMIN: 'pink',
  TEAM_LEADER: 'sky',
  DEVELOPER: 'teal',
};

export function OrgDetailPanel({ node }: OrgDetailPanelProps) {
  if (!node) {
    return (
      <div className="flex items-center justify-center h-full text-muted-foreground text-sm">
        트리에서 항목을 선택하세요
      </div>
    );
  }

  // ── ORGANIZATION ────────────────────────────────────────────────────────────
  if (node.type === 'ORGANIZATION') {
    const deptCount = node.children?.length ?? 0;
    return (
      <div>
        <h2 className="text-lg font-semibold mb-4">{node.name}</h2>
        <div className="flex items-center gap-2 text-sm mb-2">
          <span className="text-muted-foreground">부서 수</span>
          <span className="font-medium">{deptCount}개</span>
        </div>
      </div>
    );
  }

  // ── DEPARTMENT ──────────────────────────────────────────────────────────────
  if (node.type === 'DEPARTMENT') {
    const teamCount = node.meta.member_count ?? node.children?.length ?? 0;
    return (
      <div>
        <h2 className="text-lg font-semibold mb-4">{node.name}</h2>
        <div className="flex items-center gap-2 text-sm mb-2">
          <span className="text-muted-foreground">팀 수</span>
          <span className="font-medium">{teamCount}개</span>
        </div>
      </div>
    );
  }

  // ── TEAM ────────────────────────────────────────────────────────────────────
  if (node.type === 'TEAM') {
    return <TeamPanel node={node} />;
  }

  // ── USER ────────────────────────────────────────────────────────────────────
  if (node.type === 'USER') {
    return <UserPanel node={node} />;
  }

  return null;
}

// ── USER 상세 (앱 접근 권한 토글 포함) ────────────────────────────────────────

// 앱(client) 집합 — client_identifier 토큰과 동일. 새 앱 추가 시 여기만 늘리면
// 토글·예산 입력·dirty 비교가 모두 자동으로 확장된다(이전 both/single 이분법 폐기).
const ALL_CLIENTS = ['claude-code', 'cowork', 'codex'] as const;
type ClientId = (typeof ALL_CLIENTS)[number];

const CLIENT_OPTIONS: Array<{ value: ClientId; label: string }> = [
  { value: 'claude-code', label: 'Claude Code' },
  { value: 'cowork', label: 'Cowork' },
  { value: 'codex', label: 'Codex' },
];

// API allowed_clients([] = 전체 허용) → UI 체크 상태. [] 면 전부 체크로 표시.
function clientsToSelected(clients: string[]): ClientId[] {
  if (clients.length === 0) return [...ALL_CLIENTS];
  return ALL_CLIENTS.filter((c) => clients.includes(c));
}

// UI 체크 상태 → API allowed_clients. 전부 체크(또는 0개) = 전체 허용 → [](빈 배열).
// 부분 선택만 화이트리스트로 저장. (0개 체크로 잠그는 상태는 허용하지 않음 — 기존 동작 유지.)
function selectedToClients(selected: string[]): string[] {
  const chosen = ALL_CLIENTS.filter((c) => selected.includes(c));
  if (chosen.length === 0 || chosen.length === ALL_CLIENTS.length) return [];
  return chosen;
}

// allowed_clients 비교용 canonical key ([] 와 전체선택을 동일 취급).
function clientsKey(clients: string[]): string {
  return selectedToClients(clients).slice().sort().join(',');
}

function UserPanel({ node }: { node: OrgTreeNode }) {
  const { toast } = useToast();
  // 로드용/저장용 transition 분리 — 초기 조회 중에 Apply 버튼이 스피너로 보이는 혼동 방지.
  const [isLoadPending, startLoadTransition] = useTransition();
  const [isSavePending, startSaveTransition] = useTransition();

  const email = node.meta.email ?? '-';
  const role = node.meta.role ?? null;
  const teamName = node.meta.team_name ?? '미배정';

  const [loadedSelected, setLoadedSelected] = useState<ClientId[]>([...ALL_CLIENTS]);
  const [selected, setSelected] = useState<ClientId[]>([...ALL_CLIENTS]);
  // 허용 클라이언트 정책이 정상 로드됐는지 추적(Codex R2 #6). 로드 실패 시 stale
  // 전체허용([])으로 저장돼 codex 가 의도치 않게 허용되는 사고를 막기 위해 저장을 건너뛴다.
  const [clientsLoaded, setClientsLoaded] = useState(false);

  // per-app 예산 입력값 — client→문자열 map (빈 문자열 = 미설정). loaded* 는 prefill
  // 시점 값 기억 — 입력을 비우고 적용하면 clear 호출로 이어진다. 새 앱은 키만 추가됨.
  const emptyBudgets = (): Record<string, string> =>
    Object.fromEntries(ALL_CLIENTS.map((c) => [c, '']));
  const [budgets, setBudgets] = useState<Record<string, string>>(emptyBudgets);
  const [loadedBudgets, setLoadedBudgets] = useState<Record<string, string>>(emptyBudgets);

  const toggleClient = (c: ClientId) => {
    setSelected((prev) => (prev.includes(c) ? prev.filter((x) => x !== c) : [...prev, c]));
  };

  // 사용자별 허용 모델 (팀 정책 override). 비어있음 = override 해제 → 팀 정책 fallback.
  const [models, setModels] = useState<ModelListItem[]>([]);
  const [loadedModelAliases, setLoadedModelAliases] = useState<string[]>([]);
  const [selectedModelAliases, setSelectedModelAliases] = useState<string[]>([]);
  // ★ 모델 정책이 정상 로드됐는지 추적(Codex MF1). 로드 실패 시 stale 빈 목록을
  //   저장하면 기존 override 가 의도치 않게 DELETE(팀 폴백)되어 제한이 풀린다 —
  //   국가핵심기술 제한이므로 로드 실패 시에는 모델 정책 저장 자체를 건너뛴다.
  const [modelsLoaded, setModelsLoaded] = useState(false);

  useEffect(() => {
    // 사용자 전환 시 이전 사용자 상태 잔존 방지(잘못된 저장 차단).
    setModelsLoaded(false);
    setClientsLoaded(false);
    setLoadedModelAliases([]);
    setSelectedModelAliases([]);
    startLoadTransition(async () => {
      const [r, b, m, cat] = await Promise.all([
        getUserAllowedClientsAction(node.id),
        getUserClientBudgetsAction(node.id),
        getUserAllowedModelsAction(node.id),
        listActiveModelsAction(),
      ]);
      if (r.success) {
        const sel = clientsToSelected(r.data.clients);
        setLoadedSelected(sel);
        setSelected(sel);
        setClientsLoaded(true);
      } else {
        // 조회 실패 시 stale 전체허용이 실제값처럼 보여 잘못된 DELETE 를 유발할 수 있으므로 명시 알림.
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
      }
      if (cat.success) {
        setModels(cat.data);
      } else {
        toast({
          type: 'error',
          message: '모델 목록 조회 실패 — 새로고침 후 다시 시도하세요.',
          auto_dismiss_ms: 5000,
        });
      }
      if (m.success) {
        setLoadedModelAliases(m.data.modelAliases);
        setSelectedModelAliases(m.data.modelAliases);
        setModelsLoaded(true);
      } else {
        // 조회 실패 시 stale 빈 목록이 실제값처럼 보여 잘못된 DELETE(override 해제)를 유발할 수 있으므로 명시 알림.
        toast({
          type: 'error',
          message: '사용자별 허용 모델 조회 실패 — 새로고침 후 다시 시도하세요.',
          auto_dismiss_ms: 5000,
        });
      }
    });
  }, [node.id]);

  const toggleModel = (alias: string) => {
    setSelectedModelAliases((prev) =>
      prev.includes(alias) ? prev.filter((a) => a !== alias) : [...prev, alias]
    );
  };

  const handleApply = () => {
    startSaveTransition(async () => {
      // ★ Codex R2 #6: 허용 클라이언트 정책이 정상 로드되지 않았으면 stale 전체허용([])을
      //   저장해 codex 가 의도치 않게 허용되는 사고를 막기 위해 저장 자체를 중단한다.
      if (!clientsLoaded) {
        toast({
          type: 'error',
          message: '앱 접근 권한이 로드되지 않아 저장할 수 없습니다. 새로고침 후 다시 시도하세요.',
          auto_dismiss_ms: 4000,
        });
        return;
      }
      // 1) 접근 권한 먼저 저장 — 실패 시 (거부됐을 수도 있는) 접근 상태에 예산을 쓰지 않도록 중단.
      const r = await setUserAllowedClientsAction(node.id, selectedToClients(selected));
      if (!r.success) {
        toast({ type: 'error', message: r.error, auto_dismiss_ms: 4000 });
        return;
      }
      const savedSelected = clientsToSelected(r.data.clients);

      // 2) 새 접근 권한에 포함된 앱만 예산 반영. selected 에 없는 앱 예산은 손대지 않는다 —
      //    사용자가 쓸 수 없는 client 는 게이트웨이가 강제하지 않으므로 고아 예산은 무해(inert).
      const targets = ALL_CLIENTS.filter((c) => savedSelected.includes(c)).map((c) => ({
        client: c,
        value: budgets[c] ?? '',
        loaded: loadedBudgets[c] ?? '',
      }));

      // 3) >= 0 검증.
      for (const t of targets) {
        const trimmed = t.value.trim();
        if (trimmed === '') continue;
        const n = Number(trimmed);
        if (!Number.isFinite(n) || n < 0) {
          toast({ type: 'error', message: `유효하지 않은 예산 값입니다 (${t.client}). 0 이상 숫자를 입력하세요.`, auto_dismiss_ms: 4000 });
          return;
        }
      }

      // 4) 각 앱: 값이 있으면 set, 비었고 기존 예산이 있었으면 clear.
      let firstError: string | null = null;
      for (const t of targets) {
        const trimmed = t.value.trim();
        if (trimmed !== '') {
          const res = await setUserClientBudgetAction(node.id, t.client, { max_budget_usd: trimmed });
          if (!res.success && firstError === null) firstError = res.error;
        } else if (t.loaded.trim() !== '') {
          const res = await clearUserClientBudgetAction(node.id, t.client);
          if (!res.success && firstError === null) firstError = res.error;
        }
      }

      // 예산을 서버에서 재동기화해 loaded/입력 상태를 갱신하는 헬퍼.
      const resyncBudgets = async () => {
        const b = await getUserClientBudgetsAction(node.id);
        if (b.success) {
          const byClient = new Map(b.data.apps.map((a) => [a.client, a.max_budget_usd]));
          const next = emptyBudgets();
          for (const c of ALL_CLIENTS) next[c] = byClient.get(c) ?? '';
          setBudgets(next);
          setLoadedBudgets(next);
        }
      };

      // 낙관적 예산 상태 — 활성 client 는 입력값(trim), 비활성은 loaded 유지.
      const optimisticBudgets = (): Record<string, string> => {
        const next = emptyBudgets();
        for (const c of ALL_CLIENTS) {
          next[c] = savedSelected.includes(c) ? (budgets[c] ?? '').trim() : (loadedBudgets[c] ?? '');
        }
        return next;
      };

      // 5) 결과 처리.
      if (firstError) {
        // 일부 예산 쓰기 실패 — 낙관적 상태를 적용하지 않고 서버에서 재동기화한다.
        setLoadedSelected(savedSelected);
        setSelected(savedSelected);
        await resyncBudgets();
        toast({ type: 'error', message: firstError, auto_dismiss_ms: 4000 });
        return;
      }

      // 6) 사용자별 허용 모델 저장 — 빈 배열이면 action 이 DELETE(override 해제)로 처리.
      // ★ Codex MF1: 모델 정책이 정상 로드되지 않았으면(modelsLoaded=false) stale 빈 목록을
      //   저장해 기존 override 를 의도치 않게 해제하는 사고를 막기 위해 저장을 건너뛴다.
      if (modelsLoaded) {
        const mr = await setUserAllowedModelsAction(node.id, selectedModelAliases);
        if (!mr.success) {
          // 접근 권한·예산은 이미 저장됨 — 모델만 실패. loaded 상태를 동기화 후 알림.
          setLoadedSelected(savedSelected);
          setSelected(savedSelected);
          const opt = optimisticBudgets();
          setBudgets(opt);
          setLoadedBudgets(opt);
          toast({ type: 'error', message: mr.error, auto_dismiss_ms: 4000 });
          return;
        }
        setLoadedModelAliases(mr.data.modelAliases);
        setSelectedModelAliases(mr.data.modelAliases);
      }

      // 모두 성공 — loaded 상태를 낙관적 값으로 갱신.
      setLoadedSelected(savedSelected);
      setSelected(savedSelected);
      const opt = optimisticBudgets();
      setBudgets(opt);
      setLoadedBudgets(opt);
      toast({
        type: 'success',
        message: '앱 접근 권한·예산·허용 모델 저장됨 (게이트웨이 반영까지 최대 ~5분 — VK 캐시 TTL)',
        auto_dismiss_ms: 5000,
      });
    });
  };

  const busy = isLoadPending || isSavePending;

  // 접근 권한·예산·허용 모델 중 하나라도 loaded 상태에서 변경되면 적용 활성화.
  const budgetDirty = ALL_CLIENTS.some(
    (c) => (budgets[c] ?? '').trim() !== (loadedBudgets[c] ?? '').trim()
  );
  // 접근 권한은 canonical key 로 비교 ([]·전체선택 동일 취급, 순서 무관).
  // clientsLoaded=false 면 stale 상태가 dirty 로 보이지 않게 막는다(Codex R2 #6).
  const accessDirty = clientsLoaded && clientsKey(selected) !== clientsKey(loadedSelected);
  // 모델 선택은 순서 무관 비교 (toggle 시 순서가 바뀌므로).
  // modelsLoaded=false 면 비교 자체를 막아 stale 상태가 dirty 로 보이지 않게 한다(Codex MF1).
  const modelsDirty =
    modelsLoaded &&
    (selectedModelAliases.length !== loadedModelAliases.length ||
      selectedModelAliases.some((a) => !loadedModelAliases.includes(a)));
  const dirty = accessDirty || budgetDirty || modelsDirty;

  const btn = (active: boolean) =>
    [
      'pressable rounded-apple-sm px-3 py-1.5 text-sm font-medium transition-[background,color,box-shadow] duration-150',
      active
        ? 'bg-primary/10 text-primary font-semibold shadow-[inset_0_0_0_1px_hsl(var(--primary)/0.18)]'
        : 'text-muted-foreground interactive',
    ].join(' ');

  return (
    <div>
      <h2 className="text-lg font-semibold mb-4">{node.name}</h2>
      <div className="flex items-center gap-2 text-sm mb-2">
        <span className="text-muted-foreground">이메일</span>
        <span className="font-medium">{email}</span>
      </div>
      <div className="flex items-center gap-2 text-sm mb-2">
        <span className="text-muted-foreground">역할</span>
        {role ? (
          <Badge tone={ROLE_TONE[role] ?? 'neutral'}>{ROLE_LABEL[role] ?? role}</Badge>
        ) : (
          <span className="font-medium">-</span>
        )}
      </div>
      <div className="flex items-center gap-2 text-sm mb-4">
        <span className="text-muted-foreground">소속 팀</span>
        <span className="font-medium">{teamName}</span>
      </div>

      <div className="border-t pt-4">
        <p className="text-sm font-medium mb-2">앱 접근 권한</p>
        <p className="text-xs text-muted-foreground mb-2">
          전부 선택(또는 전체 해제) = 모든 앱 허용. 일부만 선택하면 화이트리스트로 제한됩니다.
        </p>
        {isLoadPending ? (
          <div className="text-xs text-muted-foreground py-1 mb-3">로딩 중…</div>
        ) : (
          <div
            role="group"
            aria-label="앱 접근 권한 선택"
            className="glass inline-flex items-center gap-0.5 rounded-apple-md p-1 mb-3"
          >
            {CLIENT_OPTIONS.map((o) => (
              <button
                key={o.value}
                type="button"
                onClick={() => toggleClient(o.value)}
                aria-pressed={selected.includes(o.value)}
                disabled={busy}
                className={btn(selected.includes(o.value))}
              >
                {o.label}
              </button>
            ))}
          </div>
        )}

        {!isLoadPending && (
          <div className="space-y-3 mb-3">
            {CLIENT_OPTIONS.filter((o) => selected.includes(o.value)).map((o) => (
              <div key={o.value}>
                <label className="block text-xs text-muted-foreground mb-1">
                  {o.label} 예산 (USD/월)
                </label>
                <input
                  type="number"
                  min={0}
                  step="0.01"
                  value={budgets[o.value] ?? ''}
                  onChange={(e) =>
                    setBudgets((prev) => ({ ...prev, [o.value]: e.target.value }))
                  }
                  disabled={busy}
                  placeholder="미설정"
                  className="glass w-full rounded-apple-sm border px-3 py-1.5 text-sm disabled:opacity-50"
                />
              </div>
            ))}
          </div>
        )}

        {!isLoadPending && (
          <div className="border rounded-apple-md p-3 mb-3">
            <p className="text-sm font-medium mb-1">사용자별 허용 모델 (팀 정책 override)</p>
            <p className="text-xs text-muted-foreground mb-3">
              선택 없음 = 팀 정책을 따름 (override 해제)
            </p>
            {!modelsLoaded ? (
              // 정책 로드 실패 — stale 상태로 저장하지 않도록 입력 비활성화 + 명시 안내(Codex MF1).
              <div className="text-xs text-destructive py-1">
                정책을 불러오지 못했습니다. 새로고침 후 다시 시도하세요. (저장 비활성화)
              </div>
            ) : models.length === 0 ? (
              <div className="text-xs text-muted-foreground py-1">활성 모델이 없습니다.</div>
            ) : (
              <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
                {models.map((m) => (
                  <label
                    key={m.alias}
                    className="flex items-center gap-2 rounded-apple-sm border p-2 cursor-pointer hover:bg-muted/50"
                  >
                    <input
                      type="checkbox"
                      checked={selectedModelAliases.includes(m.alias)}
                      onChange={() => toggleModel(m.alias)}
                      disabled={busy || !modelsLoaded}
                      className="h-4 w-4 rounded border-gray-300"
                    />
                    <span className="text-sm">{m.display_name || m.alias}</span>
                  </label>
                ))}
              </div>
            )}
          </div>
        )}

        <div>
          <SpinnerButton
            type="button"
            isLoading={isSavePending}
            disabled={busy || !dirty}
            onClick={handleApply}
          >
            적용
          </SpinnerButton>
        </div>
      </div>
    </div>
  );
}

// ── TEAM 상세 (강제 재인증 버튼 포함) ─────────────────────────────────────────

function TeamPanel({ node }: { node: OrgTreeNode }) {
  const { toast } = useToast();
  const [isPending, startTransition] = useTransition();
  const [confirmOpen, setConfirmOpen] = useState(false);

  const leader = node.meta.leader_name ?? '미지정';
  const memberCount = node.meta.member_count ?? 0;

  const handleForceReauth = () => {
    startTransition(async () => {
      const result = await forceReauthTeamAction(node.id);
      setConfirmOpen(false);
      if (result.success) {
        toast({
          type: 'success',
          message: `${result.data.revoked_count}개의 API Key가 회수되었습니다. 사용자에게 Claude Code 재실행을 안내하세요.`,
          auto_dismiss_ms: 5000,
        });
      } else {
        toast({ type: 'error', message: result.error, auto_dismiss_ms: 4000 });
      }
    });
  };

  return (
    <div>
      <h2 className="text-lg font-semibold mb-4">{node.name}</h2>
      <div className="flex items-center gap-2 text-sm mb-2">
        <span className="text-muted-foreground">리더</span>
        <span className="font-medium">{leader}</span>
      </div>
      <div className="flex items-center gap-2 text-sm mb-4">
        <span className="text-muted-foreground">구성원 수</span>
        <span className="font-medium">{memberCount}명</span>
      </div>

      <SpinnerButton
        type="button"
        className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
        onClick={() => setConfirmOpen(true)}
        disabled={memberCount === 0}
      >
        팀 전체 강제 재인증
      </SpinnerButton>

      {confirmOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
          <div className="bg-background border rounded-lg shadow-lg max-w-md w-full mx-4 p-6">
            <h3 className="text-base font-semibold mb-3">팀 전체 강제 재인증</h3>
            <p className="text-sm text-muted-foreground mb-2">
              <span className="font-medium text-foreground">{node.name}</span> 팀의
              모든 활성 API Key ({memberCount}명)를 즉시 무효화합니다.
            </p>
            <p className="text-sm text-muted-foreground mb-4">
              사용자는 호출 시 401 을 받게 되며, Claude Code 를 재실행해야 새 Key
              가 발급됩니다. 필요 시 사용자에게 별도로 안내해 주세요.
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
              <SpinnerButton
                type="button"
                className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                isLoading={isPending}
                onClick={handleForceReauth}
              >
                진행
              </SpinnerButton>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}