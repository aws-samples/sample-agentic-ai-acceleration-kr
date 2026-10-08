/**
 * The rule this file protects: **a count is AgentCore's or it is not shown.**
 *
 * One session can sit in several failure categories, so adding the category
 * counts up gives a number that is not a count of anything. The only honest total
 * is a distinct count over session ids — and those are stripped for a plain
 * user, in which case there is no total, not a smaller one.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  clusterEntries,
  distinctAffectedSessions,
  failureRows,
} from "./triageRows.mjs";

const hit = (id) => ({ session_id: id, thread_id: null, explanation: "e" });

const tree = {
  session_details: true,
  failures: [
    {
      cluster_id: 1, name: "Execution errors", description: "d1", affected_session_count: 3,
      sub_categories: [
        {
          cluster_id: 11, name: "Rate limiting", description: "d11", affected_session_count: 3,
          root_causes: [
            { cluster_id: 111, name: "No retry", root_cause: "gives up", recommendation: "retry",
              affected_session_count: 3, sessions: [hit("s1"), hit("s2"), hit("s3")] },
          ],
        },
      ],
    },
    {
      cluster_id: 2, name: "Hallucinations", description: "d2", affected_session_count: 4,
      sub_categories: [
        {
          cluster_id: 21, name: "Parameter hallucination", description: "d21", affected_session_count: 1,
          root_causes: [
            { cluster_id: 211, name: "Made-up id", root_cause: "r", recommendation: "x",
              affected_session_count: 1, sessions: [hit("s1")] },
          ],
        },
        {
          cluster_id: 22, name: "Fabricated tool output", description: "d22", affected_session_count: 3,
          root_causes: [
            { cluster_id: 221, name: "Invents result", root_cause: "r", recommendation: "y",
              affected_session_count: 3, sessions: [hit("s4"), hit("s5"), hit("s6")] },
          ],
        },
      ],
    },
  ],
  user_intents: [
    { cluster_id: 1, name: "Summarise", description: "d", affected_session_count: 2, sessions: [] },
    { cluster_id: 2, name: "Translate", description: "d", affected_session_count: 5, sessions: [] },
  ],
  execution_summaries: [],
};

test("failureRows flattens the tree, biggest first at every level, with depth", () => {
  const rows = failureRows(tree.failures);
  assert.deepEqual(
    rows.map((row) => [row.depth, row.name, row.count]),
    [
      [0, "Hallucinations", 4],
      [1, "Fabricated tool output", 3],
      [2, "Invents result", 3],
      [1, "Parameter hallucination", 1],
      [2, "Made-up id", 1],
      [0, "Execution errors", 3],
      [1, "Rate limiting", 3],
      [2, "No retry", 3],
    ],
  );
});

test("a root-cause row carries the recommendation and its sessions; a category row does not", () => {
  const rows = failureRows(tree.failures);
  const root = rows.find((row) => row.name === "No retry");
  assert.equal(root.recommendation, "retry");
  assert.equal(root.rootCause, "gives up");
  assert.equal(root.sessions.length, 3);
  const category = rows.find((row) => row.name === "Execution errors");
  assert.equal(category.recommendation, null);
  assert.deepEqual(category.sessions, []);
});

test("every row has a key that is unique even when names repeat across levels", () => {
  const dup = [{ ...tree.failures[0], name: "Same" }];
  dup[0].sub_categories = [{ ...dup[0].sub_categories[0], name: "Same" }];
  const keys = failureRows(dup).map((row) => row.key);
  assert.equal(new Set(keys).size, keys.length);
});

test("clusterEntries ranks by session count and labels the unit", () => {
  const entries = clusterEntries(tree.user_intents);
  assert.deepEqual(
    entries.map((entry) => [entry.name, entry.value, entry.display]),
    [["Translate", 5, "5세션"], ["Summarise", 2, "2세션"]],
  );
  assert.equal(entries[0].id, "2");
});

test("distinctAffectedSessions counts a session once however many categories hold it", () => {
  // s1 is in both "No retry" and "Made-up id"; category counts sum to 7, but
  // only six sessions exist.
  assert.equal(distinctAffectedSessions(tree), 6);
});

test("distinctAffectedSessions is null, not 0, when the session lists were stripped", () => {
  const stripped = {
    ...tree,
    session_details: false,
    failures: tree.failures.map((category) => ({
      ...category,
      sub_categories: category.sub_categories.map((sub) => ({
        ...sub,
        root_causes: sub.root_causes.map((root) => ({ ...root, sessions: [] })),
      })),
    })),
  };
  assert.equal(distinctAffectedSessions(stripped), null);
});

test("distinctAffectedSessions is 0 when there were no failures at all", () => {
  assert.equal(distinctAffectedSessions({ ...tree, failures: [] }), 0);
});

test("a root-cause row lists each session once even when AgentCore repeats it per fix type", () => {
  // Measured 2026-09-23 on a live run: one session came back twice under the
  // same root cause, once with fixType OTHERS and once with SYSTEM_PROMPT_FIX,
  // while affected_session_count said 1.
  const failures = [{
    cluster_id: 1, name: "C", description: null, affected_session_count: 1,
    sub_categories: [{
      cluster_id: 11, name: "S", description: null, affected_session_count: 1,
      root_causes: [{
        cluster_id: 111, name: "R", root_cause: "r", recommendation: "x",
        affected_session_count: 1,
        sessions: [
          { session_id: "s1", fix_type: "OTHERS", explanation: "first" },
          { session_id: "s1", fix_type: "SYSTEM_PROMPT_FIX", explanation: "second" },
          { session_id: "s2", fix_type: "OTHERS", explanation: "other" },
        ],
      }],
    }],
  }];
  const root = failureRows(failures).find((row) => row.depth === 2);
  assert.deepEqual(root.sessions.map((hit) => hit.session_id), ["s1", "s2"]);
  // The first hit is kept, so the explanation shown is the first one AgentCore gave.
  assert.equal(root.sessions[0].explanation, "first");
});
