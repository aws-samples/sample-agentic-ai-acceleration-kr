/**
 * Presentation rules for the thread drill-down, kept pure so the one that matters
 * can be tested without a browser.
 *
 * That rule: **the server's timestamps are UTC and say so nowhere.** They are
 * written with `datetime.utcnow().isoformat()`, which yields
 * "2026-08-16T10:05:00" — no offset and no Z — and `new Date()` reads a string
 * like that as local time. Left alone, a reader in Seoul sees every thread nine
 * hours early with nothing on screen to hint at it.
 */

/** Epoch ms for a server timestamp, or `null` when it cannot be read. */
export function epochOf(iso) {
  if (typeof iso !== "string" || iso === "") return null;
  // Append Z only when the string carries no zone of its own. Testing for a
  // trailing offset rather than always appending, because `+09:00Z` is not a
  // date at all.
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(iso);
  const parsed = Date.parse(hasZone ? iso : `${iso}Z`);
  return Number.isFinite(parsed) ? parsed : null;
}

/**
 * "8/16 10:05" in the reader's own zone.
 *
 * `timeZone` is a parameter so this is testable — the assertions pass "UTC" and
 * the UI passes nothing, which lets Intl use the browser's zone.
 */
export function formatThreadTime(iso, timeZone) {
  const epoch = epochOf(iso);
  if (epoch === null) return "—";
  const parts = new Intl.DateTimeFormat("ko-KR", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone,
  }).formatToParts(new Date(epoch));
  const find = (type) => parts.find((part) => part.type === type)?.value ?? "";
  return `${Number(find("month"))}/${Number(find("day"))} ${find("hour")}:${find("minute")}`;
}

/**
 * The head of a thread id — enough to tell two rows apart without a UUID's worth
 * of noise. The fallback identity for a row whose opening turn carried no readable
 * text; a thread that has one is shown by its first question instead.
 */
export function shortThreadId(threadId) {
  if (typeof threadId !== "string" || threadId === "") return "—";
  return threadId.split("-")[0];
}
