"use client";

import JsonView from "@uiw/react-json-view";
import { cn } from "@/lib/utils";

/**
 * A JSON value as a collapsible tree, in the app's colours.
 *
 * `@uiw/react-json-view` hardcodes a Solarized palette as the fallback of every
 * `--w-rjv-*` variable, so an unstyled tree rendered near-black text on the dark
 * theme's near-black card. The `json-tree` class in globals.css re-points those
 * variables at design tokens; going through this component is what guarantees a
 * tree never ships without them.
 *
 * Sizes: `sm` for full-width panels, `xs` for the 640px record sheet, where the
 * default 0.875rem plus indentation pushed every value past the right edge.
 */
export function JsonTree({
  value,
  collapsed = 2,
  size = "sm",
  className,
}: {
  value: object;
  /** Levels open on first render. `true` expands the whole tree. */
  collapsed?: number | true;
  size?: "sm" | "xs";
  className?: string;
}) {
  const compact = size === "xs";
  const depth = collapsed === true ? Number.POSITIVE_INFINITY : collapsed;

  return (
    <JsonView
      value={value}
      /*
       * Depth is expressed through `shouldExpandNodeInitially`, not through the
       * library's own `collapsed={n}`. Passing a number there makes the library
       * override `shouldExpandNodeInitially` with `() => false`
       * (index.js: `collapsed === false ? shouldExpandNodeInitially : () => false`),
       * which collapses *every* node including the root — so `collapsed={2}`
       * rendered a bare `{...}` and cost a click to see anything at all. Keeping
       * `collapsed` at `false` is what leaves this callback in charge.
       */
      collapsed={false}
      shouldExpandNodeInitially={(_isExpanded, { level }) => level <= depth}
      // The type prefixes (`string`, `int`) double the width of every row and
      // say what the colour already says.
      displayDataTypes={false}
      // Tighter than the 15px default: at three levels deep in a narrow panel
      // the indent cost more horizontal room than the keys did.
      indentWidth={compact ? 10 : 13}
      /*
       * Well past the 30-char default, which cut off ARNs, URLs and timestamps
       * mid-token — the three things most worth reading in a probe. Not disabled
       * outright (`0`), because a search result carrying a page of scraped text
       * as one string then filled the whole pane and buried its siblings. The
       * clamp is click-to-expand, not truncation, so nothing is unreachable.
       */
      shortenTextAfterLength={compact ? 220 : 400}
      className={cn("json-tree", compact ? "text-xxs" : "text-sm", className)}
      style={{
        // Wrapping is set here rather than in CSS because the library renders
        // each value as an inline span with its own nowrap default.
        whiteSpace: "pre-wrap",
        wordBreak: "break-all",
      }}
    />
  );
}
