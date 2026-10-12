/**
 * Recognising a `browser_screenshot` tool result — the rule, separated from the
 * component that renders it.
 *
 * Plain `.mjs` so `node --test` can exercise it without a DOM, following
 * settleToolCalls.mjs. See browserScreenshot.test.mjs for the shapes this has to
 * survive.
 *
 * The screenshot never travels as an image. The built-in-tools gateway Lambda
 * writes the PNG to S3 and returns a JSON object describing it, so the model only
 * ever sees a URL — and the chat, until now, only ever showed that JSON as text.
 * The picture was already at the far end of a link nobody was following.
 *
 * (The *other* browser tool, AgentCore's built-in `agentcore_browser`, does hand
 * the model a real image. That one cannot be shown here at all: InvokeHarness's
 * tool-result block carries only `text` and `json`, so the bytes never leave AWS.)
 */

/** Where the gateway Lambda writes; anything else is not ours to render. */
const SCREENSHOT_PREFIX = "screenshots/";

/**
 * The screenshot a tool result describes, or null if it does not describe one.
 *
 * Returns both references because they fail in opposite ways. `url` is presigned
 * and expires in an hour, but works instantly — including mid-turn, before the
 * message has been persisted anywhere the server could look it up. `s3Key` keeps
 * working for as long as the object does, but only once the turn is stored. The
 * component tries the URL and falls back, so a live capture costs no extra request
 * and a thread reopened tomorrow still renders.
 *
 * Neither lasts indefinitely: the bucket expires screenshots after 7 days, so an
 * old thread shows a broken frame no matter which reference is used. The JSON
 * result stays visible below the image partly for that reason.
 *
 * Every tool's result arrives through this same field, so returning null is the
 * ordinary case rather than an error. A result is parsed leniently and then
 * checked strictly.
 */
export function parseBrowserScreenshot(result) {
  let parsed = result;
  if (typeof parsed === "string") {
    // The harness path delivers one plain JSON string — not double-encoded, and
    // reassembled from its deltas before it gets here.
    try {
      parsed = JSON.parse(parsed);
    } catch {
      return null;
    }
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;

  // An errored capture has no image. Showing a broken frame would be worse than
  // showing the error text the box already prints.
  if (parsed.error) return null;

  const key = typeof parsed.s3_key === "string" ? parsed.s3_key : null;
  const url = typeof parsed.url === "string" ? parsed.url : null;
  if (!url && !key) return null;

  // The prefix is the contract with the server route, which refuses anything
  // else. Checking it here too keeps the two from disagreeing about what is
  // renderable.
  const s3Key = key && key.startsWith(SCREENSHOT_PREFIX) && !key.includes("..")
    ? key
    : null;
  if (!url && !s3Key) return null;

  return { url, s3Key };
}
