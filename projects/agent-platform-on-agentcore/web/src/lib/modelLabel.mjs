/**
 * Short label for a Bedrock model id: "global.anthropic.claude-sonnet-5-5" →
 * "Claude Sonnet 5.5", "…claude-haiku-4-5-20251001-v1:0" → "Claude Haiku 4.5".
 * Anything unrecognised is shown as-is, so a new family is never mislabelled.
 *
 * Plain `.mjs` so `node --test` covers it without a bundler; the override popover
 * and the agent picker import it from TypeScript.
 *
 * @param {string | undefined} modelId
 * @returns {string}
 */
export function modelLabel(modelId) {
  const match = String(modelId ?? "").match(
    /claude-(haiku|sonnet|opus)-(\d+)(?:-(\d))?(?:-\d{8})?(?:-v\d+(?::\d+)?)?$/i,
  );
  if (!match) return String(modelId ?? "");
  const [, family, major, minor] = match;
  const name = family[0].toUpperCase() + family.slice(1).toLowerCase();
  return `Claude ${name} ${major}${minor ? `.${minor}` : ""}`;
}
