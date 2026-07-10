// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

// App (client) → human label. Single source of truth; previously inline in ClientShareDonutClient.
export const CLIENT_LABELS: Record<string, string> = {
  'claude-code': 'Claude Code',
  cowork: 'Cowork',
  codex: 'Codex',
  other: '기타',
};

export function labelFor(client: string): string {
  return CLIENT_LABELS[client] ?? client;
}

// Model display: prefer the curated display_name; fall back to the raw alias when null/empty.
export function modelDisplay(alias: string, displayName?: string | null): string {
  return displayName && displayName.trim() ? displayName : alias;
}
