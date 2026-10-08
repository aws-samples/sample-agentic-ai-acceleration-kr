import test from "node:test";
import assert from "node:assert/strict";

import {
  POLL_INTERVAL_MS,
  shouldPoll,
  stampLabel,
} from "./refreshPolicy.mjs";

test("a hidden tab does not poll", () => {
  // The upstreams are cached 300s and 6h, and Cost Explorer charges per call.
  // Polling behind a hidden tab spends money to refresh nothing anyone sees.
  assert.equal(
    shouldPoll({ visibility: "hidden", lastAt: 0, now: 10 * 60 * 1000 }),
    false,
  );
});

test("a visible tab polls once the interval has elapsed", () => {
  assert.equal(
    shouldPoll({ visibility: "visible", lastAt: 0, now: POLL_INTERVAL_MS }),
    true,
  );
  assert.equal(
    shouldPoll({ visibility: "visible", lastAt: 0, now: POLL_INTERVAL_MS - 1 }),
    false,
  );
});

test("a never-loaded page polls immediately", () => {
  assert.equal(
    shouldPoll({ visibility: "visible", lastAt: null, now: 0 }),
    true,
  );
});

test("a custom interval overrides the default", () => {
  assert.equal(
    shouldPoll({ visibility: "visible", lastAt: 0, now: 5000, intervalMs: 1000 }),
    true,
  );
});

test("the stamp says how stale the number is, and never lies about freshness", () => {
  assert.equal(stampLabel(null, 0), "아직 읽지 않음");
  assert.equal(stampLabel(0, 3000), "방금");
  assert.equal(stampLabel(0, 42000), "42초 전");
  assert.equal(stampLabel(0, 125000), "2분 전");
  assert.equal(stampLabel(0, 3 * 3600 * 1000), "3시간 전");
});
