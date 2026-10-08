/**
 * Editing must start from what the harness has, and must say what it will lose.
 *
 * Tools live on the harness as resolved sources; the form works in catalogue
 * selections. Two mistakes matter here: putting a source in the wrong bucket
 * (a knowledge base offered as an MCP record), and silently dropping a source
 * no catalogue row explains — the save replaces the tool list, so anything not
 * carried back is gone.
 *
 * Run: node --test src/app/harness/harnessComposition.test.mjs
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { compositionFromHarness } from "./harnessComposition.mjs";

const KB_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:gateway/kb-gw";
const MCP_GW_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:gateway/mcp-gw";
const PLATFORM_GW_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:gateway/bap-gateway";

const catalog = {
  mcp_servers: [
    { record_id: "mcp-url", url: "https://docs.example/mcp/" },
    { record_id: "mcp-gw", gateway_arn: MCP_GW_ARN },
  ],
  skills: [{ record_id: "skill-writer", s3_uri: "s3://bap-skills/skills/writer/" }],
  bucket_skills: [{ uri: "s3://bap-skills/skills/uploaded/" }],
};
const knowledgeBases = [{ gateway_arn: KB_ARN }];

test("each gateway ARN lands where the form can re-select it", () => {
  const c = compositionFromHarness(
    { gateway_arns: [KB_ARN, MCP_GW_ARN, PLATFORM_GW_ARN] },
    catalog,
    knowledgeBases
  );
  assert.deepEqual(c.gatewayArns, [KB_ARN, PLATFORM_GW_ARN]);
  assert.deepEqual(c.mcpIds, ["mcp-gw"]);
});

test("a gateway nobody explains is carried back rather than dropped", () => {
  const c = compositionFromHarness({ gateway_arns: [PLATFORM_GW_ARN] }, catalog, []);
  assert.deepEqual(c.gatewayArns, [PLATFORM_GW_ARN]);
  assert.deepEqual(c.unmatched.mcpUrls, []);
});

test("remote MCP URLs map to records, trailing slash or not", () => {
  const c = compositionFromHarness(
    { mcp_urls: ["https://docs.example/mcp"] },
    catalog,
    []
  );
  assert.deepEqual(c.mcpIds, ["mcp-url"]);
});

test("an MCP URL whose record is gone is reported, since saving will lose it", () => {
  const c = compositionFromHarness(
    { mcp_urls: ["https://gone.example/mcp"] },
    catalog,
    []
  );
  assert.deepEqual(c.mcpIds, []);
  assert.deepEqual(c.unmatched.mcpUrls, ["https://gone.example/mcp"]);
});

test("skill prefixes split into registry records, bucket bundles and unknowns", () => {
  const c = compositionFromHarness(
    {
      skill_uris: [
        "s3://bap-skills/skills/writer",
        "s3://bap-skills/skills/uploaded/",
        "s3://other/skills/x/",
      ],
    },
    catalog,
    []
  );
  assert.deepEqual(c.skillIds, ["skill-writer"]);
  assert.deepEqual(c.skillBucketUris, ["s3://bap-skills/skills/uploaded/"]);
  assert.deepEqual(c.unmatched.skillUris, ["s3://other/skills/x/"]);
});

test("built-ins and AWS skill globs pass straight through", () => {
  const c = compositionFromHarness(
    { builtin_tools: ["agentcore_browser"], aws_skill_paths: ["core-skills/*"] },
    catalog,
    []
  );
  assert.deepEqual(c.builtins, ["agentcore_browser"]);
  assert.deepEqual(c.awsSkillPaths, ["core-skills/*"]);
});

test("a listing row (detail fields absent) yields an empty selection", () => {
  const c = compositionFromHarness({}, catalog, knowledgeBases);
  assert.deepEqual(c, {
    mcpIds: [],
    skillIds: [],
    skillBucketUris: [],
    awsSkillPaths: [],
    builtins: [],
    gatewayArns: [],
    unmatched: { mcpUrls: [], skillUris: [] },
  });
});

test("one record reached two ways is selected once", () => {
  const c = compositionFromHarness(
    { gateway_arns: [MCP_GW_ARN, MCP_GW_ARN] },
    catalog,
    []
  );
  assert.deepEqual(c.mcpIds, ["mcp-gw"]);
});
