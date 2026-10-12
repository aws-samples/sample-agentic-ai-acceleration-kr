/**
 * Refill the compose form from an existing harness.
 *
 * A harness stores resolved sources — gateway ARNs, MCP URLs, skill S3 prefixes,
 * built-in tool types — while the form works in catalogue selections (record
 * ids, knowledge bases, bucket skills). This maps one onto the other so "edit"
 * starts from what the harness actually has.
 *
 * Plain `.mjs` so `node --test` can exercise it without a DOM: the interesting
 * part is which selection each source lands in, and what happens to a source no
 * catalogue row explains any more.
 *
 * The catalogue's `url` / `gateway_arn` / `s3_uri` fields exist for this; tool
 * *names* are sanitised record names and are not reliably reversible.
 */

/**
 * @typedef {object} Composition
 * @property {string[]} mcpIds           MCP records (by URL or by gateway ARN)
 * @property {string[]} skillIds         registry skill records (by S3 prefix)
 * @property {string[]} skillBucketUris  uploaded bundles (by S3 prefix)
 * @property {string[]} awsSkillPaths    AWS toolkit skill globs, passed through
 * @property {string[]} builtins         built-in tool types
 * @property {string[]} gatewayArns      knowledge bases *and* any gateway no row
 *                                       explains — those must be re-sent as-is
 *                                       or a save would drop them
 * @property {{ mcpUrls: string[], skillUris: string[] }} unmatched
 *                                       sources that will be lost on save; the
 *                                       form shows these as a warning
 */

/**
 * @param {{
 *   gateway_arns?: string[] | null,
 *   mcp_urls?: string[] | null,
 *   builtin_tools?: string[] | null,
 *   skill_uris?: string[] | null,
 *   aws_skill_paths?: string[] | null,
 * }} harness   GetHarness summary (detail fields present)
 * @param {{
 *   mcp_servers: Array<{ record_id: string, url?: string|null, gateway_arn?: string|null }>,
 *   skills: Array<{ record_id: string, s3_uri?: string|null }>,
 *   bucket_skills: Array<{ uri: string }>,
 * }} catalog
 * @param {Array<{ gateway_arn: string }>} knowledgeBases  attachable KBs
 * @returns {Composition}
 */
export function compositionFromHarness(harness, catalog, knowledgeBases) {
  const mcpIds = [];
  const gatewayArns = [];
  const unmatchedMcpUrls = [];

  const kbArns = new Set((knowledgeBases ?? []).map((kb) => kb.gateway_arn));
  const mcpByGateway = new Map();
  const mcpByUrl = new Map();
  for (const record of catalog?.mcp_servers ?? []) {
    if (record.gateway_arn) mcpByGateway.set(record.gateway_arn, record.record_id);
    if (record.url) mcpByUrl.set(normaliseUrl(record.url), record.record_id);
  }

  for (const arn of harness.gateway_arns ?? []) {
    if (kbArns.has(arn)) {
      gatewayArns.push(arn);
    } else if (mcpByGateway.has(arn)) {
      pushOnce(mcpIds, mcpByGateway.get(arn));
    } else {
      // Attached directly (e.g. the platform gateway). Nothing in the form owns
      // it, so it rides in gatewayArns unchanged — the server accepts raw ARNs.
      gatewayArns.push(arn);
    }
  }

  for (const url of harness.mcp_urls ?? []) {
    const id = mcpByUrl.get(normaliseUrl(url));
    if (id) pushOnce(mcpIds, id);
    else unmatchedMcpUrls.push(url);
  }

  const skillIds = [];
  const skillBucketUris = [];
  const unmatchedSkillUris = [];
  const skillByUri = new Map();
  for (const record of catalog?.skills ?? []) {
    if (record.s3_uri) skillByUri.set(normalisePrefix(record.s3_uri), record.record_id);
  }
  const bucketUris = new Set(
    (catalog?.bucket_skills ?? []).map((skill) => normalisePrefix(skill.uri))
  );

  for (const uri of harness.skill_uris ?? []) {
    const key = normalisePrefix(uri);
    if (skillByUri.has(key)) pushOnce(skillIds, skillByUri.get(key));
    else if (bucketUris.has(key)) pushOnce(skillBucketUris, uri);
    else unmatchedSkillUris.push(uri);
  }

  return {
    mcpIds,
    skillIds,
    skillBucketUris,
    awsSkillPaths: [...(harness.aws_skill_paths ?? [])],
    builtins: [...(harness.builtin_tools ?? [])],
    gatewayArns,
    unmatched: { mcpUrls: unmatchedMcpUrls, skillUris: unmatchedSkillUris },
  };
}

function pushOnce(list, value) {
  if (!list.includes(value)) list.push(value);
}

/** S3 prefixes are compared without a trailing slash: both spellings occur. */
function normalisePrefix(uri) {
  return String(uri).replace(/\/+$/, "");
}

function normaliseUrl(url) {
  return String(url).replace(/\/+$/, "");
}
