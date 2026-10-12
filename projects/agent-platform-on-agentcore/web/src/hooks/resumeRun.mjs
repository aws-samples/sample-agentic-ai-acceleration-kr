/**
 * The stored transcript minus the turn still in flight. See resumeRun.test.mjs.
 *
 * @param {Array<{id?: string, type?: string}> | undefined} messages
 * @param {string[] | undefined} baselineIds ids that predate the run, from `run_baseline`
 * @returns {Array<{id?: string, type?: string}>}
 */
export function applyRunBaseline(messages, baselineIds) {
  const list = Array.isArray(messages) ? messages : [];
  if (!Array.isArray(baselineIds)) return list;
  const keep = new Set(baselineIds);
  return list.filter((m) => !m?.id || keep.has(m.id));
}
