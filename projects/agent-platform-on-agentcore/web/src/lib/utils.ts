import { type ClassValue, clsx } from "clsx";
import { extendTailwindMerge } from "tailwind-merge";

/**
 * `cn`, taught about our custom font sizes.
 *
 * tailwind-merge resolves conflicts from a built-in table of class groups, and
 * anything it does not recognise as a font size falls into the same group as
 * `text-<color>`. `text-xxs` is ours, so `cn("text-xxs", "text-primary")`
 * silently dropped the *size* and kept only the colour — every badge inherited
 * 15px body text and rendered larger than the card titles above it.
 *
 * Registering the scale here fixes it at the seam rather than at each call site,
 * so adding another custom size only needs this list updated.
 */
const twMerge = extendTailwindMerge({
  extend: {
    classGroups: {
      "font-size": [{ text: ["xxs"] }],
    },
  },
});

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/**
 * A v4 UUID, without requiring a secure context.
 *
 * `crypto.randomUUID` only exists over HTTPS or on localhost, so calling it
 * directly works in development and breaks behind a plain-HTTP load balancer.
 * `getRandomValues` has no such restriction; the manual formatting is only the
 * version and variant bits.
 */
export function randomId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }

  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;

  const hex = [...bytes].map((b) => b.toString(16).padStart(2, "0"));
  return [
    hex.slice(0, 4).join(""),
    hex.slice(4, 6).join(""),
    hex.slice(6, 8).join(""),
    hex.slice(8, 10).join(""),
    hex.slice(10, 16).join(""),
  ].join("-");
}

/**
 * Copy text to the clipboard, falling back when the async Clipboard API is
 * unavailable.
 *
 * Like `crypto.randomUUID`, `navigator.clipboard` is gated behind a secure
 * context, so it is missing entirely when the app is served over plain HTTP —
 * as it is behind the ALB. The fallback is the older `execCommand` route, which
 * needs a real (if invisible) selection to copy from.
 */
export async function copyText(text: string): Promise<boolean> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Permission denied or a detached document; try the fallback below.
    }
  }

  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  // Off-screen rather than `display: none`, which cannot hold a selection.
  area.style.position = "fixed";
  area.style.top = "-9999px";
  document.body.appendChild(area);
  area.select();
  try {
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    document.body.removeChild(area);
  }
}
