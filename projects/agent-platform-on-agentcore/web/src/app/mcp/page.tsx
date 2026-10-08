"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

/**
 * The MCP inspector moved into Settings (the "MCP Tools" tab). This route stays
 * so an old bookmark or a shared link still lands on the form instead of a 404.
 */
export default function MCPInspectorPage() {
  const router = useRouter();
  useEffect(() => {
    router.replace("/settings?tab=mcp");
  }, [router]);
  return (
    <div className="flex flex-1 items-center justify-center">
      <p className="text-sm text-muted-foreground">Settings 의 MCP Tools 로 이동합니다…</p>
    </div>
  );
}
