/**
 * A Slack integration's state, as the Harness list and dialog show it.
 *
 * Run: node --test src/app/harness/slackStatus.test.mjs
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { deletionProgress, slackBadge, tokensLookValid } from "./slackStatus.mjs";

test("no integration, no badge", () => {
  assert.equal(slackBadge(null), null);
  assert.equal(slackBadge(undefined), null);
});

test("a half-deleted integration never reads as connected", () => {
  const badge = slackBadge({ status: "deleting", connection: { state: "connected" } });
  assert.deepEqual(badge, { label: "삭제 미완료", variant: "destructive" });
});

test("the list summary and the full record read the same", () => {
  assert.deepEqual(slackBadge({ status: "active", connection_state: "connected" }), slackBadge({ status: "active", connection: { state: "connected" } }));
});

test("a refused token is an error, a reconnect is a warning", () => {
  assert.equal(slackBadge({ status: "active", connection: { state: "auth_error" } }).variant, "destructive");
  assert.equal(slackBadge({ status: "active", connection: { state: "reconnecting" } }).variant, "warning");
  // Seen from another process: its connection lives elsewhere.
  assert.equal(slackBadge({ status: "active" }).label, "연결 안 됨");
});

test("deletion progress lists every step in order", () => {
  const progress = deletionProgress([
    { step: "stop", ok: true },
    { step: "revoke_token", ok: false, error: "network_error" },
  ]);
  assert.deepEqual(progress.map((p) => [p.step, p.state, p.error]), [
    ["stop", "done", null],
    ["revoke_token", "failed", "network_error"],
    ["delete_records", "pending", null],
  ]);
});

test("token shapes are checked before the server is asked", () => {
  assert.equal(tokensLookValid("xoxb-1-2", "xapp-1-2"), true);
  assert.equal(tokensLookValid("xapp-1-2", "xoxb-1-2"), false);
  assert.equal(tokensLookValid("xoxb-", "xapp-1"), false);
  assert.equal(tokensLookValid(" xoxb-1 ", " xapp-1 "), true);
});
