/**
 * The page's table logic, kept out of React so it can be pinned here:
 * statuses must read as words, "0" must not look like "unknown", and
 * provider limits ("as of last login") must survive sorting and filtering.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  auditActionLabel,
  driftTitle,
  filterUsers,
  isStaleInFlight,
  isToolPattern,
  parseEmails,
  providerLabel,
  shortTool,
  sortUsers,
  teamStatusLabel,
  managedByLabel,
  toggleEntry,
  userStatusLabel,
} from "./orgTable.mjs";

const team = (over) => ({ name: "hr", managed_by: "platform", status: "ready", policy_ids: { tools: "p" }, error: null, ...over });

test("team status reads as words with a tone", () => {
  assert.deepEqual(teamStatusLabel(team()), { label: "준비됨", tone: "ok" });
  assert.deepEqual(teamStatusLabel(team({ status: "provisioning" })), { label: "프로비저닝 중", tone: "warn" });
  assert.deepEqual(teamStatusLabel(team({ status: "error", error: { step: "iam_role" } })), { label: "오류 · iam_role", tone: "error" });
  assert.deepEqual(teamStatusLabel(team({ status: "deleting" })), { label: "삭제 중", tone: "muted" });
  assert.deepEqual(teamStatusLabel(team({ managed_by: "workshop" })), { label: "준비됨", tone: "ok" });
  assert.equal(managedByLabel(team()), "플랫폼");
  assert.equal(managedByLabel(team({ managed_by: "workshop" })), "workshop");
});

const users = [
  { sub: "a", email: "alice@x", name: "", provider: "cognito", role: "admin", teams: [], status: "CONFIRMED", enabled: true, last_login: "", turns_30d: 0, model_cost_micros_30d: 0 },
  { sub: "b", email: "bob@x", name: "Bob", provider: "cognito", role: "user", teams: ["finance"], status: "FORCE_CHANGE_PASSWORD", enabled: true, last_login: "", turns_30d: 12, model_cost_micros_30d: 500 },
  { sub: "c", email: "carol@x", name: "", provider: "cognito", role: "user", teams: ["hr"], status: "CONFIRMED", enabled: false, last_login: "", turns_30d: 3, model_cost_micros_30d: 0 },
  { sub: "e", email: "dan@corp", name: "Dan", provider: "entra", role: "user", teams: ["hr"], status: "", enabled: true, last_login: "2026-10-09T00:00:00+00:00", turns_30d: 0, model_cost_micros_30d: 0 },
];

test("sortUsers orders by turns, cost and email, stable on ties", () => {
  assert.deepEqual(sortUsers(users, "turns_30d", "desc").map((u) => u.sub), ["b", "c", "a", "e"]);
  assert.deepEqual(sortUsers(users, "email", "asc").map((u) => u.sub), ["a", "b", "c", "e"]);
  assert.deepEqual(sortUsers(users, "model_cost_micros_30d", "desc").map((u) => u.sub), ["b", "a", "c", "e"]);
  assert.deepEqual(users.map((u) => u.sub), ["a", "b", "c", "e"], "input untouched");
});

test("filterUsers matches email or name, team, role, provider and status", () => {
  assert.deepEqual(filterUsers(users, { q: "BOB" }).map((u) => u.sub), ["b"]);
  assert.deepEqual(filterUsers(users, { q: "dan" }).map((u) => u.sub), ["e"]);
  assert.deepEqual(filterUsers(users, { team: "hr" }).map((u) => u.sub), ["c", "e"]);
  assert.deepEqual(filterUsers(users, { role: "admin" }).map((u) => u.sub), ["a"]);
  assert.deepEqual(filterUsers(users, { provider: "entra" }).map((u) => u.sub), ["e"]);
  assert.deepEqual(filterUsers(users, { status: "disabled" }).map((u) => u.sub), ["c"]);
  assert.deepEqual(filterUsers(users, { status: "FORCE_CHANGE_PASSWORD" }).map((u) => u.sub), ["b"]);
  assert.equal(filterUsers(users, {}).length, 4);
});

test("user status: disabled wins, Entra says as-of-login, Cognito states are words", () => {
  assert.deepEqual(userStatusLabel(users[2]), { label: "비활성", tone: "muted" });
  assert.deepEqual(userStatusLabel(users[1]), { label: "임시 비밀번호", tone: "warn" });
  assert.deepEqual(userStatusLabel(users[0]), { label: "활성", tone: "ok" });
  assert.deepEqual(userStatusLabel(users[3]), { label: "마지막 로그인 기준", tone: "muted" });
});

test("labels", () => {
  assert.equal(providerLabel("cognito"), "Cognito");
  assert.equal(providerLabel("entra"), "Entra");
  assert.equal(providerLabel("okta"), "okta");
  assert.equal(shortTool("bap-platform-tools___fetch_url"), "fetch_url");
  assert.equal(shortTool("plain"), "plain");
  assert.equal(auditActionLabel("team.create"), "팀 생성");
  assert.equal(auditActionLabel("user.reset_password"), "비밀번호 초기화");
  assert.equal(auditActionLabel("something.else"), "something.else");
  assert.equal(driftTitle({ kind: "unregistered_group", team: "zzz" }), "미등록 팀 · zzz");
  assert.equal(driftTitle({ kind: "missing_role", team: "hr" }), "실행 역할 없음 · hr");
});

test("parseEmails splits on commas, whitespace and newlines, lowercases and dedupes", () => {
  assert.deepEqual(parseEmails("A@x.com, b@x.com\n c@x.com  a@x.com"), ["a@x.com", "b@x.com", "c@x.com"]);
  assert.deepEqual(parseEmails(""), []);
});

test("tool entries: harness patterns vs gateway names, and the @builtin toggle", () => {
  assert.equal(isToolPattern("@builtin"), true);
  assert.equal(isToolPattern("@bap-platform-tools/approve_expense"), true);
  assert.equal(isToolPattern("*"), true);
  assert.equal(isToolPattern("bap-platform-tools___calculate"), false);
  assert.deepEqual(toggleEntry(["a___b"], "@builtin", true), ["a___b", "@builtin"]);
  assert.deepEqual(toggleEntry(["a___b", "@builtin"], "@builtin", true), ["a___b", "@builtin"]);
  assert.deepEqual(toggleEntry(["@builtin", "a___b"], "@builtin", false), ["a___b"]);
});

test("in-flight rows older than five minutes are stale, ready rows never", () => {
  const now = Date.parse("2026-10-10T12:00:00Z");
  assert.equal(isStaleInFlight(team({ status: "provisioning", updated_at: "2026-10-10T11:54:59Z" }), now), true);
  assert.equal(isStaleInFlight(team({ status: "deleting", updated_at: "2026-10-10T11:59:00Z" }), now), false);
  assert.equal(isStaleInFlight(team({ status: "provisioning", updated_at: "" }), now), true);
  assert.equal(isStaleInFlight(team({ status: "ready", updated_at: "2026-01-01T00:00:00Z" }), now), false);
  assert.equal(driftTitle({ kind: "stale_deleting", team: "hr" }), "삭제 멈춤 · hr");
});
