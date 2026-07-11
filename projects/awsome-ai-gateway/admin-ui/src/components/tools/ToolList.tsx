'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useEffect, useState } from 'react';

interface McpTool {
  name: string;
  description?: string;
  inputSchema?: Record<string, unknown>;
}

interface ToolListResponse {
  tools: McpTool[];
  status: 'Complete' | 'Unavailable' | 'Disabled';
}

export function ToolList() {
  const [data, setData] = useState<ToolListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const fetchTools = async () => {
      try {
        setLoading(true);
        const response = await fetch('/api/tools/list');
        if (!response.ok) {
          throw new Error(`API error: ${response.status}`);
        }
        const result = (await response.json()) as ToolListResponse;
        setData(result);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Unknown error');
      } finally {
        setLoading(false);
      }
    };

    fetchTools();
  }, []);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-48 text-sm text-muted-foreground">
        불러오는 중…
      </div>
    );
  }

  if (data?.status === 'Disabled') {
    return (
      <div className="flex items-center justify-center h-48 text-sm text-muted-foreground">
        Tool Gateway가 비활성화되어 있습니다.
      </div>
    );
  }

  if (data?.status === 'Unavailable') {
    return (
      <div className="flex items-center justify-center h-48 text-sm text-muted-foreground">
        Tool Gateway에 연결할 수 없습니다.
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center justify-center h-48 text-sm text-destructive">
        오류: {error}
      </div>
    );
  }

  if (!data || data.tools.length === 0) {
    return (
      <div className="flex items-center justify-center h-48 text-sm text-muted-foreground">
        등록된 tool이 없습니다.
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {data.tools.map((tool) => (
        <div
          key={tool.name}
          className="border rounded-lg p-4 space-y-3 bg-card"
        >
          <div className="flex items-start justify-between gap-4">
            <div className="flex-1 space-y-1">
              <h3 className="font-mono text-sm font-semibold">{tool.name}</h3>
              {tool.description && (
                <p className="text-sm text-muted-foreground">{tool.description}</p>
              )}
            </div>
          </div>

          {tool.inputSchema && (
            <div className="pt-2">
              <p className="text-xs font-semibold text-muted-foreground mb-2">
                Input Schema
              </p>
              <pre className="bg-muted rounded p-3 overflow-auto text-xs font-mono max-h-48">
                {JSON.stringify(tool.inputSchema, null, 2)}
              </pre>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
