"use client";

import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Copy, Plug } from "lucide-react";
import { toast } from "sonner";
import { copyText } from "@/lib/utils";

/**
 * The region is the one part of the endpoint the IDE config cannot take from the
 * URL verbatim. Read it from `agent-registry.<region>.api.aws`; null when the host
 * has another shape, so the caller can say so instead of guessing.
 */
function regionOf(endpoint: string): string | null {
  try {
    const match = /^agent-registry\.([a-z0-9-]+)\.api\.aws$/.exec(new URL(endpoint).hostname);
    return match ? match[1] : null;
  } catch {
    return null;
  }
}

/** The mcp.json entry for mcp-proxy-for-aws, which bridges stdio IDEs to SigV4 endpoints. */
function mcpJson(endpoint: string, region: string | null): string {
  return JSON.stringify(
    {
      mcpServers: {
        "agent-registry": {
          type: "stdio",
          command: "uvx",
          args: [
            "mcp-proxy-for-aws@latest",
            endpoint,
            "--service",
            "agent-registry",
            "--region",
            region ?? "<region>",
          ],
        },
      },
    },
    null,
    2
  );
}

/**
 * How to point an IDE at the registry's MCP endpoint. The endpoint is SigV4-only,
 * so an IDE cannot call it directly; the snippet runs the proxy that signs requests.
 */
export function McpEndpointCard({ endpoint }: { endpoint: string }) {
  const region = regionOf(endpoint);
  const snippet = mcpJson(endpoint, region);

  const copy = async (text: string, what: string) => {
    if (await copyText(text)) toast.success(`${what} 복사했습니다.`);
    else toast.error("복사하지 못했습니다.");
  };

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button size="sm" variant="outline">
          <Plug className="size-3.5" />
          IDE 연결
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-[28rem] space-y-3 text-sm">
        <div className="space-y-1.5">
          <p className="caps-label-xs text-muted-foreground">MCP endpoint</p>
          <div className="flex items-start gap-2">
            <code className="min-w-0 flex-1 break-all rounded-md bg-muted px-2 py-1.5 font-mono text-xs">
              {endpoint}
            </code>
            <Button
              size="icon-sm"
              variant="ghost"
              aria-label="엔드포인트 복사"
              onClick={() => copy(endpoint, "엔드포인트를")}
            >
              <Copy />
            </Button>
          </div>
        </div>

        <div className="space-y-1.5">
          <div className="flex items-center justify-between gap-2">
            <p className="caps-label-xs text-muted-foreground">mcp.json</p>
            <Button size="sm" variant="ghost" onClick={() => copy(snippet, "설정을")}>
              <Copy className="size-3.5" />
              복사
            </Button>
          </div>
          <pre className="max-h-64 overflow-auto rounded-md bg-muted p-2.5 font-mono text-xs leading-normal">
            {snippet}
          </pre>
          {!region && (
            <p className="text-xs leading-normal text-warning">
              엔드포인트 호스트에서 리전을 읽지 못했습니다. <code>&lt;region&gt;</code>을
              직접 채워 주세요.
            </p>
          )}
        </div>

        <p className="text-xs leading-normal text-muted-foreground">
          호출자에게 <code>agent-registry:InvokeRegistryMcp</code> 와{" "}
          <code>agent-registry:SearchDiscoverableRegistryRecords</code> 권한이
          필요합니다.
        </p>
      </PopoverContent>
    </Popover>
  );
}
