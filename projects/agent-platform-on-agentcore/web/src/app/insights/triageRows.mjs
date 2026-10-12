/**
 * Presentation rules for the AgentCore insights (triage) panel, kept pure so the
 * one that matters can be tested without a browser.
 *
 * That rule: **a count is AgentCore's or it is not shown.** Every cluster carries
 * the service's own `affected_session_count`, and the panel prints those as they
 * are. It never adds them up: one session can sit in several failure categories,
 * so a sum of category counts is a number with no referent. The only total that
 * means something is a distinct count over session ids, and when the ids were
 * stripped (a plain user's view) there is no total — `null`, not a smaller
 * number.
 */

const byCount = (left, right) =>
  (right.affected_session_count ?? 0) - (left.affected_session_count ?? 0);

/**
 * The failure hierarchy as flat rows for an indented list: categories, their
 * sub-categories, their root causes — biggest first at every level. Only a
 * root-cause row has a recommendation and sessions; the levels above are
 * groupings, and giving them a "recommendation" would be inventing one.
 *
 * @returns {Array<{key: string, depth: 0|1|2, name: string, count: number,
 *   description: string|null, rootCause: string|null, recommendation: string|null,
 *   sessions: Array<object>}>}
 */
export function failureRows(failures) {
  const rows = [];
  for (const category of [...(failures ?? [])].sort(byCount)) {
    const categoryKey = `c${category.cluster_id ?? category.name}`;
    rows.push(row(categoryKey, 0, category));
    for (const sub of [...(category.sub_categories ?? [])].sort(byCount)) {
      const subKey = `${categoryKey}/s${sub.cluster_id ?? sub.name}`;
      rows.push(row(subKey, 1, sub));
      for (const root of [...(sub.root_causes ?? [])].sort(byCount)) {
        rows.push({
          ...row(`${subKey}/r${root.cluster_id ?? root.name}`, 2, root),
          rootCause: root.root_cause ?? null,
          recommendation: root.recommendation ?? null,
          sessions: uniqueBySession(root.sessions),
        });
      }
    }
  }
  return rows;
}

/**
 * One hit per session, first one kept. Measured 2026-09-23 on a live run: a
 * session came back twice under the same root cause, once per `fix_type`, while
 * `affected_session_count` said 1 — so the list is per finding, not per session,
 * and drawing it as-is shows two chips for one conversation.
 */
function uniqueBySession(hits) {
  const seen = new Set();
  const unique = [];
  for (const hit of hits ?? []) {
    const id = hit.session_id ?? "";
    if (seen.has(id)) continue;
    seen.add(id);
    unique.push(hit);
  }
  return unique;
}

function row(key, depth, cluster) {
  return {
    key,
    depth,
    name: cluster.name ?? "—",
    count: cluster.affected_session_count ?? 0,
    description: cluster.description ?? null,
    rootCause: null,
    recommendation: null,
    sessions: [],
  };
}

/**
 * Flat clusters (user intents, execution patterns) as ranked-bar entries. The
 * unit is stated on every value because the bars beside them on the page are
 * turns, tokens and dollars.
 */
export function clusterEntries(clusters) {
  return [...(clusters ?? [])].sort(byCount).map((cluster) => ({
    id: String(cluster.cluster_id ?? cluster.name),
    name: cluster.name ?? "—",
    value: cluster.affected_session_count ?? 0,
    display: `${cluster.affected_session_count ?? 0}세션`,
  }));
}

/**
 * How many distinct sessions had at least one failure — or `null` when the
 * session ids are not in the tree, which is what a plain user receives. Zero is
 * reserved for "no failures were found".
 */
export function distinctAffectedSessions(tree) {
  if (!tree || tree.session_details === false) return null;
  const ids = new Set();
  for (const category of tree.failures ?? []) {
    for (const sub of category.sub_categories ?? []) {
      for (const root of sub.root_causes ?? []) {
        for (const hit of root.sessions ?? []) {
          if (hit.session_id) ids.add(hit.session_id);
        }
      }
    }
  }
  return ids.size;
}
