/**
 * The rule this file exists to protect: the server's timestamps are UTC but say
 * so nowhere.
 *
 * `datetime.utcnow().isoformat()` produces "2026-08-16T10:05:00" — no offset, no
 * Z. `new Date()` reads that as *local* time, so a reader in Seoul sees every
 * thread nine hours early, and nothing about the rendered string reveals it. The
 * existing sidebar (`useThreads`) does exactly this; the drill-down must not.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { epochOf, formatThreadTime, shortThreadId } from "./threadRows.mjs";

test("a naive server timestamp is read as UTC, not as local time", () => {
  assert.equal(epochOf("2026-08-16T10:05:00"), Date.parse("2026-08-16T10:05:00Z"));
});

test("an explicit offset is respected rather than overridden", () => {
  assert.equal(epochOf("2026-08-16T19:05:00+09:00"), Date.parse("2026-08-16T10:05:00Z"));
  assert.equal(epochOf("2026-08-16T10:05:00Z"), Date.parse("2026-08-16T10:05:00Z"));
});

test("an unusable timestamp is null, so the caller can say so instead of showing 1970", () => {
  assert.equal(epochOf(""), null);
  assert.equal(epochOf(null), null);
  assert.equal(epochOf("나중에"), null);
});

test("formatThreadTime renders the date and time it was given", () => {
  // Formatted in UTC so the assertion does not depend on the box's zone; the UI
  // passes the reader's zone.
  assert.equal(
    formatThreadTime("2026-08-16T10:05:00", "UTC"),
    "8/16 10:05",
  );
  assert.equal(formatThreadTime("2026-08-16T10:05:00", "Asia/Seoul"), "8/16 19:05");
  assert.equal(formatThreadTime("", "UTC"), "—");
});

test("shortThreadId keeps enough of the id to tell two threads apart", () => {
  assert.equal(shortThreadId("3f8a9c11-2d4e-4b7a-9c33-0e1f2a3b4c5d"), "3f8a9c11");
  assert.equal(shortThreadId("short"), "short");
  assert.equal(shortThreadId(""), "—");
  assert.equal(shortThreadId(null), "—");
});
