/**
 * How a Harness's Slack integration reads in the list and the dialog.
 *
 * Kept out of the component so the mapping is testable: an integration that is
 * half-deleted must never read as "connected", and a dead token must say so
 * rather than look like a transient blip.
 *
 * Run: node --test src/app/harness/slackStatus.test.mjs
 */

/** @typedef {{ label: string, variant: "success"|"warning"|"destructive"|"secondary"|"info" }} SlackBadge */

const CONNECTION = {
  connected: { label: "연결됨", variant: "success" },
  connecting: { label: "연결 중", variant: "info" },
  reconnecting: { label: "재연결 중", variant: "warning" },
  auth_error: { label: "토큰 거부됨", variant: "destructive" },
  secret_error: { label: "토큰 읽기 실패", variant: "destructive" },
  stopped: { label: "중지됨", variant: "secondary" },
  disconnected: { label: "연결 안 됨", variant: "warning" },
};

/**
 * @param {{ status?: string|null, connection_state?: string|null, connection?: { state?: string|null }|null }|null|undefined} integration
 * @returns {SlackBadge|null} null when the Harness has no integration
 */
export function slackBadge(integration) {
  if (!integration) return null;
  if (integration.status === "deleting") return { label: "삭제 미완료", variant: "destructive" };
  const state = integration.connection?.state ?? integration.connection_state ?? "disconnected";
  return CONNECTION[state] ?? { label: state, variant: "secondary" };
}

const STEP_LABELS = {
  stop: "수신 중단",
  revoke_token: "Bot Token 폐기",
  delete_records: "시크릿·연동 정보 삭제",
};

/** The cleanup steps in order, each marked done / failed / not reached. */
export function deletionProgress(steps) {
  const byStep = Object.fromEntries((steps ?? []).map((s) => [s.step, s]));
  return Object.keys(STEP_LABELS).map((step) => {
    const result = byStep[step];
    return {
      step,
      label: STEP_LABELS[step],
      state: result ? (result.ok ? "done" : "failed") : "pending",
      error: result && !result.ok ? result.error ?? null : null,
    };
  });
}

/** Both tokens look like what Slack issues; the server re-checks everything. */
export function tokensLookValid(botToken, appToken) {
  return /^xoxb-\S+$/.test((botToken ?? "").trim()) && /^xapp-\S+$/.test((appToken ?? "").trim());
}
