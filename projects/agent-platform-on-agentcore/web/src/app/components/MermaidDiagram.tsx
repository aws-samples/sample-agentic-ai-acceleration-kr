"use client";

import React, { useEffect, useId, useState } from "react";
import { CodeBlock } from "@/app/components/CodeBlock";

type Mermaid = typeof import("mermaid")["default"];

let mermaidPromise: Promise<Mermaid> | null = null;

/**
 * Loads and configures mermaid once per page.
 *
 * The import is deferred so mermaid (d3, cytoscape, katex...) stays out of the
 * initial bundle, and `initialize` runs once — calling it per render would keep
 * overwriting global config.
 */
function loadMermaid(): Promise<Mermaid> {
  if (!mermaidPromise) {
    mermaidPromise = import("mermaid").then(({ default: mermaid }) => {
      const prefersDark =
        typeof window !== "undefined" &&
        window.matchMedia("(prefers-color-scheme: dark)").matches;
      mermaid.initialize({
        startOnLoad: false,
        // Diagram source is model output: strict blocks click directives and
        // javascript: URLs, and text-only labels avoid rendering HTML at all.
        securityLevel: "strict",
        htmlLabels: false,
        theme: prefersDark ? "dark" : "default",
      });
      return mermaid;
    });
  }
  return mermaidPromise;
}

export const MermaidDiagram = React.memo<{
  source: string;
  /**
   * False while the source is still streaming in. Mermaid cannot parse a partial
   * diagram, so rendering is held off entirely rather than failing per token.
   */
  settled?: boolean;
}>(({ source, settled = true }) => {
  const reactId = useId();
  // Mermaid uses the id in CSS selectors; React 19 ids contain colons.
  const diagramId = `mermaid-${reactId.replace(/[^a-zA-Z0-9_-]/g, "")}`;

  const [svg, setSvg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!settled || !source.trim()) return;

    let cancelled = false;
    loadMermaid()
      .then((mermaid) => mermaid.render(diagramId, source))
      .then(({ svg: rendered }) => {
        if (cancelled) return;
        setSvg(rendered);
        setError(null);
      })
      .catch((e: unknown) => {
        // On a parse error mermaid leaves its scratch container attached to
        // <body>, which would otherwise show a stray error diagram.
        document.getElementById(`d${diagramId}`)?.remove();
        if (cancelled) return;
        // Keep the last good diagram on screen; only note the failure.
        setError(e instanceof Error ? e.message : String(e));
      });

    return () => {
      cancelled = true;
    };
  }, [source, settled, diagramId]);

  if (svg) {
    return (
      <div className="my-4">
        <div
          className="flex justify-center overflow-x-auto rounded-lg border border-border bg-background p-4 [&_svg]:h-auto [&_svg]:max-w-full"
          // Mermaid sanitizes its output with DOMPurify before returning it.
          dangerouslySetInnerHTML={{ __html: svg }}
        />
        {error && <DiagramError message={error} />}
      </div>
    );
  }

  return (
    <div className="my-4">
      <CodeBlock content={source} language="text" />
      {error && <DiagramError message={error} />}
    </div>
  );
});

MermaidDiagram.displayName = "MermaidDiagram";

function DiagramError({ message }: { message: string }) {
  return (
    <p className="mt-2 text-xs text-muted-foreground">
      다이어그램을 그릴 수 없습니다: {message.split("\n")[0]}
    </p>
  );
}
