/**
 * Pure helpers for the Organization page tables. No React, no fetch: the
 * labels and orderings here are what the tests pin.
 */

const TEAM_STATUS = {
  ready: ["준비됨", "ok"],
  provisioning: ["프로비저닝 중", "warn"],
  deleting: ["삭제 중", "muted"],
};

/** "platform" is the server's own team; any other value names who wrote the row. */
export function managedByLabel(team) {
  return team.managed_by === "platform" ? "플랫폼" : team.managed_by || "외부";
}

export function teamStatusLabel(team) {
  if (team.status === "error") {
    const step = team.error && team.error.step ? ` · ${team.error.step}` : "";
    return { label: `오류${step}`, tone: "error" };
  }
  const [label, tone] = TEAM_STATUS[team.status] || [team.status || "—", "muted"];
  return { label, tone };
}

const NUMERIC = new Set(["turns_30d", "model_cost_micros_30d"]);
// Read off the row as the table shows it: teams as their list, status as its label.
const SORT_VALUE = {
  teams: (u) => (u.teams || []).join(", "),
  status: (u) => userStatusLabel(u).label,
};

/** Words start A→Z; figures and dates start biggest/newest first. */
export function firstSortDirection(key) {
  return NUMERIC.has(key) || key === "last_login" ? "desc" : "asc";
}

/** Stable sort; a blank text value (no team, never logged in) stays last in either direction. */
export function sortUsers(users, key, direction = "desc") {
  const sign = direction === "asc" ? 1 : -1;
  const value = SORT_VALUE[key] || ((u) => u[key] ?? (NUMERIC.has(key) ? 0 : ""));
  return users
    .map((u, i) => [u, i, value(u)])
    .sort(([, ia, va], [, ib, vb]) => {
      if (!NUMERIC.has(key) && (va === "") !== (vb === "")) return va === "" ? 1 : -1;
      const cmp = NUMERIC.has(key) ? va - vb : String(va).localeCompare(String(vb));
      return cmp !== 0 ? cmp * sign : ia - ib;
    })
    .map(([u]) => u);
}

export function filterUsers(users, { q = "", team = "", role = "", provider = "", status = "" } = {}) {
  const needle = q.trim().toLowerCase();
  return users.filter((u) => {
    if (needle && !u.email.toLowerCase().includes(needle) && !(u.name || "").toLowerCase().includes(needle)) return false;
    if (team && !u.teams.includes(team)) return false;
    if (role && u.role !== role) return false;
    if (provider && u.provider !== provider) return false;
    if (status === "disabled" && u.enabled) return false;
    if (status && status !== "disabled" && u.status !== status) return false;
    return true;
  });
}

export function providerLabel(provider) {
  if (provider === "cognito") return "Cognito";
  if (provider === "entra") return "Entra";
  return provider;
}

const COGNITO_STATUS = {
  CONFIRMED: ["활성", "ok"],
  FORCE_CHANGE_PASSWORD: ["임시 비밀번호", "warn"],
  RESET_REQUIRED: ["재설정 필요", "warn"],
  UNCONFIRMED: ["미확인", "warn"],
  COMPROMISED: ["침해 의심", "error"],
  ARCHIVED: ["보관됨", "muted"],
};

export function userStatusLabel(user) {
  if (!user.enabled) return { label: "비활성", tone: "muted" };
  if (user.provider !== "cognito") return { label: "마지막 로그인 기준", tone: "muted" };
  const [label, tone] = COGNITO_STATUS[user.status] || [user.status || "—", "muted"];
  return { label, tone };
}

export function shortTool(name) {
  const idx = name.lastIndexOf("___");
  return idx >= 0 ? name.slice(idx + 3) : name;
}

/**
 * `allowed_tools` holds two kinds of entry: harness patterns (`@builtin`,
 * `@<server>/<tool>`, `*`) and gateway names (`<target>___<tool>`). Only the
 * second kind is in the gateway list the pickers show.
 */
export function isToolPattern(entry) {
  return entry === "*" || entry.startsWith("@");
}

export const BUILTIN_PATTERN = "@builtin";

export function toggleEntry(list, entry, on) {
  if (on) return list.includes(entry) ? list : [...list, entry];
  return list.filter((t) => t !== entry);
}

/** Server PROVISION_STALE_SECONDS: an in-flight row older than this is stuck, not running. */
export const STALE_MS = 5 * 60 * 1000;

export function isStaleInFlight(team, nowMs) {
  if (team.status !== "provisioning" && team.status !== "deleting") return false;
  const at = Date.parse(team.updated_at || "");
  return Number.isNaN(at) || nowMs - at >= STALE_MS;
}

const ACTIONS = {
  "team.create": "팀 생성",
  "team.update": "팀 설정 변경",
  "team.delete": "팀 삭제",
  "team.provision": "팀 프로비저닝",
  "member.add": "구성원 추가",
  "member.remove": "구성원 제거",
  "user.invite": "사용자 초대",
  "user.update_role": "역할 변경",
  "user.update_teams": "팀 변경",
  "user.disable": "비활성화",
  "user.enable": "복구",
  "user.reset_password": "비밀번호 초기화",
  "user.delete": "사용자 삭제",
};

export function auditActionLabel(action) {
  return ACTIONS[action] || action;
}

const DRIFT = {
  unregistered_group: "미등록 팀",
  missing_group: "Cognito 그룹 없음",
  missing_role: "실행 역할 없음",
  missing_policy: "게이트웨이 정책 없음",
  provisioning_error: "프로비저닝 오류",
  stale_provisioning: "프로비저닝 멈춤",
  stale_deleting: "삭제 멈춤",
};

export function driftTitle(item) {
  return `${DRIFT[item.kind] || item.kind} · ${item.team}`;
}

export function parseEmails(text) {
  const out = [];
  for (const raw of text.split(/[\s,;]+/)) {
    const email = raw.trim().toLowerCase();
    if (email && !out.includes(email)) out.push(email);
  }
  return out;
}

/**
 * The Organization directory's pick, as the address holds it: `?team=<name>`,
 * `?view=users|noteam`, or nothing for 모든 팀. The users tab the directory
 * replaced (`?tab=users`) still lands on 모든 사용자.
 *
 * @typedef {{kind: "teams"} | {kind: "users"} | {kind: "noteam"} | {kind: "team", name: string}} OrgView
 * @param {URLSearchParams} params
 * @returns {OrgView}
 */
export function viewFromParams(params) {
  const team = params.get("team");
  if (team) return { kind: "team", name: team };
  const view = params.get("view") ?? (params.get("tab") === "users" ? "users" : null);
  return view === "users" || view === "noteam" ? { kind: view } : { kind: "teams" };
}

/** @param {OrgView} view @returns {string} the `/org` query for it, `""` for 모든 팀. */
export function viewQuery(view) {
  if (view.kind === "team") return `?team=${encodeURIComponent(view.name)}`;
  return view.kind === "teams" ? "" : `?view=${view.kind}`;
}
