// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

/**
 * 평가·개선 화면 (화면 7) — Manager 이상.
 *
 * 구성
 * ----
 * (a) 평가 실행     — 배치 평가 시작(조회 창 지정) + 후보 채굴 실행, 실행 목록·결과 스코어
 * (b) online eval   — OnlineEvaluationConfig 상태(샘플링률·평가자)
 * (c) 개선 추천      — StartRecommendation(SYSTEM_PROMPT/TOOL_DESCRIPTION) + 결과 텍스트
 * (d) Configuration Bundle — 버전 목록·활성 뱃지·승격(=롤백)·최초 생성 폼
 *
 * 설계 메모
 * --------
 * - 추천·번들은 **Preview API** 라 실패해도 화면이 죽지 않는다. 실패 시 안내 문구를
 *   띄우고 나머지 섹션은 계속 동작한다.
 * - A/B 트래픽 분할은 제공하지 않는다 — 승격은 전량 전환이고 롤백은 예전 버전 재승격이다.
 *   이 제약을 화면에 명시해 운영자가 오해하지 않게 한다.
 * - env 미구성(evaluation 스택 미배포) 환경에서도 목록은 빈 값으로 정상 렌더된다.
 */

import { useCallback, useEffect, useState } from 'react';
import { apiFetch, type SessionInfo } from '@/lib/client';
import {
  RECOMMENDATION_TYPES,
  RECOMMENDATION_TYPE_LABEL,
  type BatchEvaluationItem,
  type ConfigurationBundleItem,
  type ConfigurationBundleVersionItem,
  type EvaluatorSummaryItem,
  type OnlineEvalStatus,
  type RecommendationDetail,
  type RecommendationItem,
  type RecommendationTypeKey,
} from '@/lib/types';
import { Alert, EmptyState, Section, formatTime } from './ui';

/** 조회 창 선택값 (배치 평가·추천·채굴 공용). */
const WINDOW_OPTIONS = [
  { value: 1, label: '최근 1시간' },
  { value: 24, label: '최근 24시간' },
  { value: 168, label: '최근 7일' },
];

/** 평균 스코어 표기 — 값이 없으면 "—". */
function formatScore(value?: number): string {
  if (value == null) return '—';
  return value.toLocaleString('ko-KR', { maximumFractionDigits: 3 });
}

function formatCount(value?: number): string {
  return value == null ? '—' : value.toLocaleString('ko-KR');
}

export function EvaluationView({ session }: { session: SessionInfo }) {
  const token = session.accessToken;

  // (a) 배치 평가
  const [hours, setHours] = useState(24);
  const [evaluators, setEvaluators] = useState<EvaluatorSummaryItem[]>([]);
  const [executionEvaluatorId, setExecutionEvaluatorId] = useState<string | null>(null);
  const [runs, setRuns] = useState<BatchEvaluationItem[]>([]);
  const [selectedRun, setSelectedRun] = useState<BatchEvaluationItem | null>(null);
  const [runsLoading, setRunsLoading] = useState(false);
  const [starting, setStarting] = useState(false);
  const [mining, setMining] = useState(false);

  // (b) online eval
  const [online, setOnline] = useState<OnlineEvalStatus | null>(null);

  // (c) 추천
  const [recommendations, setRecommendations] = useState<RecommendationItem[]>([]);
  const [recoDetail, setRecoDetail] = useState<RecommendationDetail | null>(null);
  const [recoNote, setRecoNote] = useState<string | null>(null);
  const [recoBusy, setRecoBusy] = useState(false);

  // (d) bundle
  const [bundles, setBundles] = useState<ConfigurationBundleItem[]>([]);
  const [activeBundle, setActiveBundle] = useState<{
    bundleId: string;
    versionId: string;
  } | null>(null);
  const [selectedBundleId, setSelectedBundleId] = useState<string | null>(null);
  const [versions, setVersions] = useState<ConfigurationBundleVersionItem[]>([]);
  const [bundleNote, setBundleNote] = useState<string | null>(null);
  const [bundleBusy, setBundleBusy] = useState(false);
  const [formPrompt, setFormPrompt] = useState('');
  const [formModelId, setFormModelId] = useState('');
  const [formCommit, setFormCommit] = useState('');

  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // ---------------------------------------------------------------- 조회

  const loadEvaluators = useCallback(async () => {
    try {
      const body = await apiFetch<{
        evaluators?: EvaluatorSummaryItem[];
        execution_evaluator_id?: string | null;
      }>('/api/eval/evaluators', { token });
      setEvaluators(body.evaluators ?? []);
      setExecutionEvaluatorId(body.execution_evaluator_id ?? null);
    } catch (caught) {
      // 평가자 목록 실패는 다른 섹션을 막지 않는다(목록은 참고용).
      console.warn('[eval] 평가자 목록 조회 실패:', caught);
      setEvaluators([]);
    }
  }, [token]);

  const loadRuns = useCallback(async () => {
    setRunsLoading(true);
    try {
      const body = await apiFetch<{ runs?: BatchEvaluationItem[] }>('/api/eval/runs', { token });
      const items = body.runs ?? [];
      setRuns(items);
      setSelectedRun((current) =>
        current
          ? (items.find((r) => r.batch_evaluation_id === current.batch_evaluation_id) ?? current)
          : null
      );
    } catch (caught) {
      setError((caught as Error).message);
      setRuns([]);
    } finally {
      setRunsLoading(false);
    }
  }, [token]);

  const loadOnline = useCallback(async () => {
    try {
      const body = await apiFetch<OnlineEvalStatus>('/api/eval/online', { token });
      setOnline(body);
    } catch (caught) {
      setOnline({ configured: false, note: (caught as Error).message });
    }
  }, [token]);

  const loadRecommendations = useCallback(async () => {
    try {
      const body = await apiFetch<{ recommendations?: RecommendationItem[] }>(
        '/api/recommendations',
        { token }
      );
      setRecommendations(body.recommendations ?? []);
      setRecoNote(null);
    } catch (caught) {
      setRecommendations([]);
      setRecoNote(
        `개선 추천 기능을 사용할 수 없습니다 (Preview) — ${(caught as Error).message}. ` +
          '수동으로 프롬프트를 편집해 새 번들 버전을 만드는 경로로 진행하세요.'
      );
    }
  }, [token]);

  const loadBundles = useCallback(async () => {
    try {
      const body = await apiFetch<{
        bundles?: ConfigurationBundleItem[];
        active?: { bundleId: string; versionId: string } | null;
      }>('/api/bundles', { token });
      const items = body.bundles ?? [];
      setBundles(items);
      setActiveBundle(body.active ?? null);
      setBundleNote(null);
      setSelectedBundleId(
        (current) => current ?? body.active?.bundleId ?? items[0]?.bundle_id ?? null
      );
    } catch (caught) {
      setBundles([]);
      setBundleNote(
        `번들 목록을 불러올 수 없습니다 (Preview) — ${(caught as Error).message}. ` +
          '활성 설정은 orchestrator 코드 기본값으로 폴백되어 서비스는 계속 동작합니다.'
      );
    }
  }, [token]);

  const loadVersions = useCallback(
    async (bundleId: string) => {
      try {
        const body = await apiFetch<{
          versions?: ConfigurationBundleVersionItem[];
        }>(`/api/bundles/${encodeURIComponent(bundleId)}/versions`, { token });
        setVersions(body.versions ?? []);
      } catch (caught) {
        setVersions([]);
        setBundleNote(`버전 목록 조회 실패 — ${(caught as Error).message}`);
      }
    },
    [token]
  );

  useEffect(() => {
    void loadEvaluators();
    void loadRuns();
    void loadOnline();
    void loadRecommendations();
    void loadBundles();
  }, [loadEvaluators, loadRuns, loadOnline, loadRecommendations, loadBundles]);

  useEffect(() => {
    if (selectedBundleId) void loadVersions(selectedBundleId);
    else setVersions([]);
  }, [selectedBundleId, loadVersions]);

  // ---------------------------------------------------------------- 동작

  const startRun = async () => {
    setError(null);
    setNotice(null);
    setStarting(true);
    try {
      const body = await apiFetch<{ batch_evaluation_id?: string }>('/api/eval/runs', {
        token,
        method: 'POST',
        body: JSON.stringify({ hours }),
      });
      setNotice(`배치 평가를 시작했습니다 — ${body.batch_evaluation_id ?? '(ID 미확인)'}`);
      await loadRuns();
    } catch (caught) {
      setError((caught as Error).message);
    } finally {
      setStarting(false);
    }
  };

  const runMining = async () => {
    setError(null);
    setNotice(null);
    setMining(true);
    try {
      const body = await apiFetch<{ scanned?: number; mined?: number; skipped_existing?: number }>(
        '/api/mining/run',
        { token, method: 'POST', body: JSON.stringify({ hours }) }
      );
      setNotice(
        `후보 채굴 완료 — 스캔 ${body.scanned ?? 0}건 / 신규 ${body.mined ?? 0}건 / ` +
          `중복 skip ${body.skipped_existing ?? 0}건. 승인 큐에서 검토하세요.`
      );
    } catch (caught) {
      setError((caught as Error).message);
    } finally {
      setMining(false);
    }
  };

  const openRun = async (run: BatchEvaluationItem) => {
    setError(null);
    setSelectedRun(run);
    try {
      const body = await apiFetch<{ run?: BatchEvaluationItem }>(
        `/api/eval/runs/${encodeURIComponent(run.batch_evaluation_id)}`,
        { token }
      );
      if (body.run) setSelectedRun(body.run);
    } catch (caught) {
      setError((caught as Error).message);
    }
  };

  const startRecommendation = async (type: RecommendationTypeKey) => {
    setError(null);
    setNotice(null);
    setRecoBusy(true);
    try {
      const body = await apiFetch<{ recommendation_id?: string }>('/api/recommendations', {
        token,
        method: 'POST',
        body: JSON.stringify({ type, hours }),
      });
      setNotice(
        `${RECOMMENDATION_TYPE_LABEL[type]} 추천을 시작했습니다 — ${body.recommendation_id ?? '(ID 미확인)'}`
      );
      await loadRecommendations();
    } catch (caught) {
      setRecoNote(
        `추천 실행 실패 (Preview) — ${(caught as Error).message}. ` +
          '수동으로 프롬프트를 편집해 새 번들 버전을 만드는 경로로 진행하세요.'
      );
    } finally {
      setRecoBusy(false);
    }
  };

  const openRecommendation = async (item: RecommendationItem) => {
    setError(null);
    try {
      const body = await apiFetch<{ recommendation?: RecommendationDetail }>(
        `/api/recommendations/${encodeURIComponent(item.recommendation_id)}`,
        { token }
      );
      setRecoDetail(body.recommendation ?? null);
    } catch (caught) {
      setRecoNote(`추천 상세 조회 실패 (Preview) — ${(caught as Error).message}`);
    }
  };

  const createBundle = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    setNotice(null);
    setBundleBusy(true);
    try {
      const body = await apiFetch<{ bundle_id?: string; version_id?: string }>('/api/bundles', {
        token,
        method: 'POST',
        body: JSON.stringify({ systemPrompt: formPrompt, modelId: formModelId.trim() }),
      });
      setNotice(`번들을 생성했습니다 — ${body.bundle_id} / ${body.version_id}`);
      if (body.bundle_id) setSelectedBundleId(body.bundle_id);
      await loadBundles();
    } catch (caught) {
      setBundleNote(`번들 생성 실패 (Preview) — ${(caught as Error).message}`);
    } finally {
      setBundleBusy(false);
    }
  };

  const createVersion = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!selectedBundleId) return;
    setError(null);
    setNotice(null);
    setBundleBusy(true);
    try {
      const body = await apiFetch<{ version_id?: string }>(
        `/api/bundles/${encodeURIComponent(selectedBundleId)}/versions`,
        {
          token,
          method: 'POST',
          body: JSON.stringify({
            systemPrompt: formPrompt,
            modelId: formModelId.trim(),
            commitMessage: formCommit.trim() || undefined,
            parentVersionId:
              activeBundle?.bundleId === selectedBundleId ? activeBundle.versionId : undefined,
          }),
        }
      );
      setNotice(`새 버전을 만들었습니다 — ${body.version_id}. 승격 전까지는 반영되지 않습니다.`);
      await loadVersions(selectedBundleId);
    } catch (caught) {
      setBundleNote(`버전 생성 실패 (Preview) — ${(caught as Error).message}`);
    } finally {
      setBundleBusy(false);
    }
  };

  const promote = async (versionId: string) => {
    if (!selectedBundleId) return;
    const confirmed = window.confirm(
      `이 버전을 활성 설정으로 승격합니다.\n\n번들: ${selectedBundleId}\n버전: ${versionId}\n\n` +
        '전량 전환입니다(A/B 분할 없음). 이후 모든 세션이 이 설정을 사용합니다. 계속할까요?'
    );
    if (!confirmed) return;

    setError(null);
    setNotice(null);
    setBundleBusy(true);
    try {
      await apiFetch(`/api/bundles/${encodeURIComponent(selectedBundleId)}/promote`, {
        token,
        method: 'POST',
        body: JSON.stringify({ versionId }),
      });
      setNotice(`승격 완료 — ${versionId} (orchestrator 는 최대 60초 내 반영)`);
      await loadBundles();
      await loadVersions(selectedBundleId);
    } catch (caught) {
      setBundleNote(`승격 실패 — ${(caught as Error).message}`);
    } finally {
      setBundleBusy(false);
    }
  };

  const hasBundles = bundles.length > 0;

  return (
    <>
      <Alert kind="error" message={error} />
      <Alert kind="ok" message={notice} />

      {/* ---------------------------------------------------------- (a) */}
      <Section
        title="평가 실행"
        description="orchestrator 트레이스(CloudWatch Logs)를 소스로 배치 평가를 시작합니다. 기본 평가자는 Execution Accuracy(EX) + Builtin.Correctness 입니다. 비동기 작업이라 결과는 목록에서 확인합니다."
      >
        <div className="adm-row">
          <div className="adm-field">
            <label className="adm-label" htmlFor="adm-eval-window">
              조회 창
            </label>
            <select
              id="adm-eval-window"
              className="adm-select"
              value={hours}
              onChange={(e) => setHours(Number(e.target.value))}
            >
              {WINDOW_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>
          <button
            className="adm-btn adm-btn-primary"
            type="button"
            onClick={() => void startRun()}
            disabled={starting}
          >
            {starting ? '시작 중…' : '배치 평가 실행'}
          </button>
          <button
            className="adm-btn"
            type="button"
            onClick={() => void runMining()}
            disabled={mining}
            title="orchestrator 로그에서 fewshot/term 후보를 채굴해 승인 큐에 적재합니다"
          >
            {mining ? '채굴 중…' : '후보 채굴 실행'}
          </button>
          <button
            className="adm-btn"
            type="button"
            onClick={() => void loadRuns()}
            disabled={runsLoading}
          >
            {runsLoading ? '불러오는 중…' : '새로고침'}
          </button>
        </div>

        {executionEvaluatorId ? (
          <p className="adm-desc">
            EX 평가자: <span className="adm-mono">{executionEvaluatorId}</span> · 사용 가능한 평가자{' '}
            {evaluators.length}종
          </p>
        ) : (
          <Alert
            kind="info"
            message="EXECUTION_EVALUATOR_ID 가 미구성입니다 — evaluation 스택 배포 후 EX 평가자가 기본 선택됩니다."
          />
        )}

        <div className="adm-split">
          <div className="adm-table-wrap">
            <table className="adm-table">
              <thead>
                <tr>
                  <th>평가 이름</th>
                  <th>상태</th>
                  <th>생성 시각</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => (
                  <tr
                    key={run.batch_evaluation_id}
                    className={
                      selectedRun?.batch_evaluation_id === run.batch_evaluation_id
                        ? 'adm-row-selected'
                        : undefined
                    }
                  >
                    <td>
                      <button
                        className="adm-btn-link adm-mono"
                        type="button"
                        onClick={() => void openRun(run)}
                      >
                        {run.batch_evaluation_name ?? run.batch_evaluation_id}
                      </button>
                    </td>
                    <td>
                      <span className="adm-badge">{run.status ?? '-'}</span>
                    </td>
                    <td>{formatTime(run.created_at)}</td>
                  </tr>
                ))}
                {!runs.length && !runsLoading ? (
                  <tr>
                    <td colSpan={3}>
                      <EmptyState>배치 평가 실행 이력이 없습니다.</EmptyState>
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>

          <div className="adm-panel">
            <h3 style={{ margin: '0 0 12px', fontSize: 14 }}>평가 결과</h3>
            {selectedRun ? (
              <>
                <div className="adm-desc" style={{ marginBottom: 10 }}>
                  <span className="adm-mono">{selectedRun.batch_evaluation_id}</span> ·{' '}
                  {selectedRun.status ?? '-'}
                </div>
                {selectedRun.sessions ? (
                  <p className="adm-desc">
                    세션 — 전체 {formatCount(selectedRun.sessions.total)} / 완료{' '}
                    {formatCount(selectedRun.sessions.completed)} / 진행{' '}
                    {formatCount(selectedRun.sessions.in_progress)} / 실패{' '}
                    {formatCount(selectedRun.sessions.failed)}
                  </p>
                ) : null}
                <table className="adm-table">
                  <thead>
                    <tr>
                      <th>평가자</th>
                      <th>평균 스코어</th>
                      <th>평가/실패</th>
                    </tr>
                  </thead>
                  <tbody>
                    {selectedRun.scores.map((score, index) => (
                      <tr key={`${score.evaluator_id ?? 'unknown'}-${index}`}>
                        <td className="adm-mono">{score.evaluator_id ?? '-'}</td>
                        <td>{formatScore(score.average_score)}</td>
                        <td className="adm-mono">
                          {formatCount(score.total_evaluated)} / {formatCount(score.total_failed)}
                        </td>
                      </tr>
                    ))}
                    {!selectedRun.scores.length ? (
                      <tr>
                        <td colSpan={3}>
                          <EmptyState>
                            아직 집계된 스코어가 없습니다 (평가가 진행 중일 수 있습니다).
                          </EmptyState>
                        </td>
                      </tr>
                    ) : null}
                  </tbody>
                </table>
                {selectedRun.error_details?.length ? (
                  <pre className="adm-code">{selectedRun.error_details.join('\n')}</pre>
                ) : null}
              </>
            ) : (
              <EmptyState>왼쪽 목록에서 평가를 선택하세요.</EmptyState>
            )}
          </div>
        </div>
      </Section>

      {/* ---------------------------------------------------------- (b) */}
      <Section
        title="온라인 평가 (샘플링)"
        description="운영 트래픽의 일부를 상시 평가하는 OnlineEvaluationConfig 상태입니다. 승격 전후 비교의 기준선으로 사용합니다."
      >
        {online?.configured ? (
          <>
            <div className="adm-cards">
              <div className="adm-card">
                <div className="adm-card-label">구성 상태</div>
                <div className="adm-card-value" style={{ fontSize: 18 }}>
                  {online.config_status ?? '—'}
                </div>
              </div>
              <div className="adm-card">
                <div className="adm-card-label">실행 상태</div>
                <div className="adm-card-value" style={{ fontSize: 18 }}>
                  {online.execution_status ?? '—'}
                </div>
              </div>
              <div className="adm-card">
                <div className="adm-card-label">샘플링률</div>
                <div className="adm-card-value">
                  {online.sampling_percentage == null ? '—' : online.sampling_percentage}
                  {online.sampling_percentage == null ? null : (
                    <span className="adm-card-unit">%</span>
                  )}
                </div>
              </div>
              <div className="adm-card">
                <div className="adm-card-label">평가자 수</div>
                <div className="adm-card-value">{online.evaluator_ids?.length ?? 0}</div>
              </div>
            </div>
            <p className="adm-desc">
              config: <span className="adm-mono">{online.online_evaluation_config_id}</span>
              {online.evaluator_ids?.length ? (
                <>
                  {' '}
                  · 평가자 <span className="adm-mono">{online.evaluator_ids.join(', ')}</span>
                </>
              ) : null}
            </p>
            <Alert kind="error" message={online.failure_reason} />
          </>
        ) : (
          <Alert
            kind="info"
            message={online?.note ?? '온라인 평가 구성이 확인되지 않았습니다 (미구성).'}
          />
        )}
      </Section>

      {/* ---------------------------------------------------------- (c) */}
      <Section
        title="개선 추천"
        description="평가 트레이스를 분석해 시스템 프롬프트·도구 설명 개선안을 생성합니다(Preview 기능). 추천 결과는 자동 반영되지 않고, Manager 가 번들 새 버전으로 반영한 뒤 승격해야 적용됩니다."
      >
        <Alert kind="info" message={recoNote} />
        <div className="adm-row">
          {RECOMMENDATION_TYPES.map((type) => (
            <button
              key={type}
              className="adm-btn"
              type="button"
              onClick={() => void startRecommendation(type)}
              disabled={recoBusy}
            >
              {RECOMMENDATION_TYPE_LABEL[type]} 추천 실행
            </button>
          ))}
          <button className="adm-btn" type="button" onClick={() => void loadRecommendations()}>
            새로고침
          </button>
        </div>

        <div className="adm-split">
          <div className="adm-table-wrap">
            <table className="adm-table">
              <thead>
                <tr>
                  <th>이름</th>
                  <th>종류</th>
                  <th>상태</th>
                  <th>생성</th>
                </tr>
              </thead>
              <tbody>
                {recommendations.map((item) => (
                  <tr
                    key={item.recommendation_id}
                    className={
                      recoDetail?.recommendation_id === item.recommendation_id
                        ? 'adm-row-selected'
                        : undefined
                    }
                  >
                    <td>
                      <button
                        className="adm-btn-link adm-mono"
                        type="button"
                        onClick={() => void openRecommendation(item)}
                      >
                        {item.name ?? item.recommendation_id}
                      </button>
                    </td>
                    <td>{item.type ? (RECOMMENDATION_TYPE_LABEL[item.type] ?? item.type) : '-'}</td>
                    <td>
                      <span className="adm-badge">{item.status ?? '-'}</span>
                    </td>
                    <td>{formatTime(item.created_at)}</td>
                  </tr>
                ))}
                {!recommendations.length ? (
                  <tr>
                    <td colSpan={4}>
                      <EmptyState>추천 실행 이력이 없습니다.</EmptyState>
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>

          <div className="adm-panel">
            <h3 style={{ margin: '0 0 12px', fontSize: 14 }}>추천 상세</h3>
            {recoDetail ? (
              <>
                <div className="adm-desc" style={{ marginBottom: 10 }}>
                  {recoDetail.type
                    ? (RECOMMENDATION_TYPE_LABEL[recoDetail.type] ?? recoDetail.type)
                    : '-'}{' '}
                  · {recoDetail.status ?? '-'}
                </div>
                {recoDetail.recommended_system_prompt ? (
                  <pre className="adm-code">{recoDetail.recommended_system_prompt}</pre>
                ) : null}
                {recoDetail.recommended_tools?.length ? (
                  <pre className="adm-code">
                    {recoDetail.recommended_tools
                      .map(
                        (tool) =>
                          `# ${tool.tool_name ?? '(도구명 미확인)'}\n${
                            tool.recommended_tool_description ?? ''
                          }`
                      )
                      .join('\n\n')}
                  </pre>
                ) : null}
                {recoDetail.explanation ? (
                  <p className="adm-desc">{recoDetail.explanation}</p>
                ) : null}
                <Alert
                  kind="error"
                  message={
                    recoDetail.error_message ??
                    (recoDetail.error_code ? `오류 코드: ${recoDetail.error_code}` : null)
                  }
                />
                {!recoDetail.recommended_system_prompt && !recoDetail.recommended_tools?.length ? (
                  <EmptyState>
                    아직 결과가 없습니다 (COMPLETED 상태가 되면 추천 텍스트가 표시됩니다).
                  </EmptyState>
                ) : null}
                {recoDetail.recommended_system_prompt ? (
                  <div className="adm-actions" style={{ marginTop: 12 }}>
                    <button
                      className="adm-btn"
                      type="button"
                      onClick={() => {
                        setFormPrompt(recoDetail.recommended_system_prompt ?? '');
                        setFormCommit(`추천 반영 (${recoDetail.recommendation_id})`);
                        setNotice(
                          '추천 프롬프트를 아래 번들 편집 폼에 불러왔습니다. 검토 후 새 버전을 만드세요.'
                        );
                      }}
                    >
                      번들 편집 폼으로 불러오기
                    </button>
                  </div>
                ) : null}
              </>
            ) : (
              <EmptyState>왼쪽 목록에서 추천을 선택하세요.</EmptyState>
            )}
          </div>
        </div>
      </Section>

      {/* ---------------------------------------------------------- (d) */}
      <Section
        title="Configuration Bundle (승격 · 롤백)"
        description="orchestrator 의 시스템 프롬프트·모델 설정을 불변 버전으로 관리합니다. 활성 버전은 SSM 포인터가 단일 원천이며, A/B 트래픽 분할은 제공하지 않습니다 — 승격은 전량 전환이고 롤백은 예전 버전을 다시 승격하는 방식(수동 전환 폴백)입니다."
      >
        <Alert kind="info" message={bundleNote} />
        <div className="adm-row">
          <div className="adm-field">
            <label className="adm-label" htmlFor="adm-bundle-select">
              번들
            </label>
            <select
              id="adm-bundle-select"
              className="adm-select"
              value={selectedBundleId ?? ''}
              onChange={(e) => setSelectedBundleId(e.target.value || null)}
            >
              <option value="">선택하세요</option>
              {bundles.map((bundle) => (
                <option key={bundle.bundle_id} value={bundle.bundle_id}>
                  {bundle.bundle_name ?? bundle.bundle_id}
                </option>
              ))}
            </select>
          </div>
          <button className="adm-btn" type="button" onClick={() => void loadBundles()}>
            새로고침
          </button>
          {activeBundle ? (
            <span className="adm-badge adm-badge-published">
              활성 {activeBundle.bundleId} / {activeBundle.versionId}
            </span>
          ) : (
            <span className="adm-badge adm-badge-candidate">활성 포인터 없음 (코드 기본값)</span>
          )}
        </div>

        <div className="adm-split">
          <div className="adm-table-wrap">
            <table className="adm-table">
              <thead>
                <tr>
                  <th>버전</th>
                  <th>커밋 메시지</th>
                  <th>생성</th>
                  <th>작업</th>
                </tr>
              </thead>
              <tbody>
                {versions.map((version) => {
                  const isActive =
                    activeBundle?.bundleId === selectedBundleId &&
                    activeBundle?.versionId === version.version_id;
                  return (
                    <tr key={version.version_id}>
                      <td>
                        <span className="adm-mono">{version.version_id}</span>{' '}
                        {isActive ? (
                          <span className="adm-badge adm-badge-published">활성</span>
                        ) : null}
                      </td>
                      <td>{version.commit_message ?? '-'}</td>
                      <td>
                        <div>{formatTime(version.version_created_at)}</div>
                        <div className="adm-mono" style={{ color: 'var(--t2s-muted)' }}>
                          {version.created_by ?? '-'}
                        </div>
                      </td>
                      <td>
                        <button
                          className="adm-btn adm-btn-sm adm-btn-primary"
                          type="button"
                          onClick={() => void promote(version.version_id)}
                          disabled={bundleBusy || isActive}
                        >
                          {isActive ? '활성' : '이 버전으로 승격'}
                        </button>
                      </td>
                    </tr>
                  );
                })}
                {!versions.length ? (
                  <tr>
                    <td colSpan={4}>
                      <EmptyState>
                        {selectedBundleId
                          ? '버전이 없습니다.'
                          : '번들을 선택하거나, 오른쪽 폼으로 최초 번들을 생성하세요.'}
                      </EmptyState>
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>

          <div className="adm-panel">
            <h3 style={{ margin: '0 0 12px', fontSize: 14 }}>
              {hasBundles && selectedBundleId ? '새 버전 만들기' : '최초 번들 생성'}
            </h3>
            <form
              onSubmit={
                hasBundles && selectedBundleId
                  ? (event) => void createVersion(event)
                  : (event) => void createBundle(event)
              }
            >
              <div className="adm-field">
                <label className="adm-label" htmlFor="adm-bundle-model">
                  model_id
                </label>
                <input
                  id="adm-bundle-model"
                  className="adm-input adm-mono"
                  value={formModelId}
                  onChange={(e) => setFormModelId(e.target.value)}
                  placeholder="us.anthropic.claude-...-v1:0"
                  required
                />
              </div>
              <div className="adm-field">
                <label className="adm-label" htmlFor="adm-bundle-prompt">
                  system_prompt
                </label>
                <textarea
                  id="adm-bundle-prompt"
                  className="adm-textarea adm-mono"
                  rows={10}
                  value={formPrompt}
                  onChange={(e) => setFormPrompt(e.target.value)}
                  placeholder="orchestrator 시스템 프롬프트"
                  required
                />
              </div>
              {hasBundles && selectedBundleId ? (
                <div className="adm-field">
                  <label className="adm-label" htmlFor="adm-bundle-commit">
                    커밋 메시지
                  </label>
                  <input
                    id="adm-bundle-commit"
                    className="adm-input"
                    value={formCommit}
                    onChange={(e) => setFormCommit(e.target.value)}
                    placeholder="변경 이유를 남기세요"
                  />
                </div>
              ) : null}
              <div className="adm-actions">
                <button className="adm-btn adm-btn-primary" type="submit" disabled={bundleBusy}>
                  {bundleBusy
                    ? '저장 중…'
                    : hasBundles && selectedBundleId
                      ? '새 버전 저장'
                      : '번들 생성'}
                </button>
              </div>
            </form>
            <p className="adm-desc">
              저장은 버전 생성까지입니다 — 실제 적용은 목록에서 <strong>승격</strong> 해야
              반영됩니다(사람 승인 게이트).
            </p>
          </div>
        </div>
      </Section>
    </>
  );
}
