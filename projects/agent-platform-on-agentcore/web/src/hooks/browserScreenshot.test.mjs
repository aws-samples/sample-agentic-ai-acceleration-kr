/**
 * A screenshot the agent took should be visible, not described.
 *
 * `browser_screenshot` puts a PNG in S3 and returns JSON pointing at it. The tool
 * box rendered that JSON as a wall of text — a 1.5 KB presigned URL and a byte
 * count — so the one thing the call produced was the one thing nobody could see.
 *
 * The fixtures are the real wire shape, taken from an InvokeHarness run against
 * test_builtin_harness_agent: a single plain JSON string carrying `url`,
 * `expires_in_seconds` and `s3_key`. Worth pinning verbatim, because the shape has
 * two traps — the result is *not* double-encoded (so one JSON.parse is right and
 * two is wrong), and it arrives split across deltas, which is why anything reading
 * a fragment instead of the reassembled whole gets invalid JSON.
 *
 * Run: node --test src/hooks/browserScreenshot.test.mjs
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { parseBrowserScreenshot } from "./browserScreenshot.mjs";

const KEY =
  "screenshots/ap-standalone-ap-standalone-harnes-80fd020a/1786684605893.png";
const URL_ =
  "https://ap-standalone-builtin-tools-123456789012.s3.amazonaws.com/" +
  KEY +
  "?AWSAccessKeyId=ASIA5JMSTZPLTHVQDNBK&Signature=abc&Expires=1786688206";

const WIRE = JSON.stringify({
  session: "ap-standalone-harnes-80fd020a",
  status: "SUCCESS",
  bytes: 280,
  url: URL_,
  expires_in_seconds: 3600,
  s3_key: KEY,
});

test("the real wire shape yields both references", () => {
  assert.deepEqual(parseBrowserScreenshot(WIRE), { url: URL_, s3Key: KEY });
});

test("an already-parsed object works too", () => {
  assert.deepEqual(parseBrowserScreenshot(JSON.parse(WIRE)), {
    url: URL_,
    s3Key: KEY,
  });
});

test("a result from a Lambda predating s3_key still renders live", () => {
  // Deployment order is not guaranteed: the gateway may lag the web build.
  const older = JSON.stringify({ session: "s", status: "SUCCESS", url: URL_ });

  assert.deepEqual(parseBrowserScreenshot(older), { url: URL_, s3Key: null });
});

test("a stored call whose url has expired still resolves by key", () => {
  const stored = JSON.stringify({ session: "s", status: "SUCCESS", s3_key: KEY });

  assert.deepEqual(parseBrowserScreenshot(stored), { url: null, s3Key: KEY });
});

test("a failed capture is not rendered as an image", () => {
  // The box already prints the error text; a broken frame adds nothing.
  const failed = JSON.stringify({
    session: "s",
    error: "navigation failed at keyPress",
    url: URL_,
  });

  assert.equal(parseBrowserScreenshot(failed), null);
});

test("a key outside the screenshots prefix is not addressable", () => {
  // The server route refuses these; disagreeing here would render an <img> that
  // can only 404.
  const sneaky = JSON.stringify({ s3_key: "attachments/t-other/att1.png" });

  assert.equal(parseBrowserScreenshot(sneaky), null);
});

test("a traversal in the key is refused even under the right prefix", () => {
  const sneaky = JSON.stringify({
    s3_key: "screenshots/../attachments/t-other/att1.png",
  });

  assert.equal(parseBrowserScreenshot(sneaky), null);
});

for (const [name, result] of [
  ["other tools' plain text", "3 rows"],
  ["a fragment of a split result", '{"session":"s","status":"SUCC'],
  ["JSON without either reference", '{"session":"s","status":"SUCCESS","bytes":280}'],
  ["a non-string url", JSON.stringify({ url: 42 })],
  ["an array", "[1,2,3]"],
  ["undefined", undefined],
  ["null", null],
  ["empty string", ""],
]) {
  test(`${name} is not a screenshot`, () => {
    assert.equal(parseBrowserScreenshot(result), null);
  });
}
