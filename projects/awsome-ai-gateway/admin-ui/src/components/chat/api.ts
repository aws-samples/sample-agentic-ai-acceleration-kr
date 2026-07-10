// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

import type { ChatSession, ChatMessage } from './types';

// admin-ui server-side proxy (src/app/api/chat-proxy/[...path]) 경유.
// 브라우저 → admin-ui /api/chat-proxy/admin/chat/* → admin-api 내부 DNS.
const API_BASE = '/api/chat-proxy';

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers || {}),
    },
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`${response.status}: ${text}`);
  }
  return response.json() as Promise<T>;
}

export async function createSession(): Promise<{ session_id: string; expires_at: string }> {
  return fetchJson('/admin/chat/sessions', {
    method: 'POST',
    body: JSON.stringify({}),
  });
}

export async function listSessions(): Promise<ChatSession[]> {
  const raw = await fetchJson<
    Array<{
      id: string;
      title: string | null;
      status: 'active' | 'expired' | 'archived';
      updated_at: string;
      message_count: number;
    }>
  >('/admin/chat/sessions');
  return raw.map((r) => ({
    id: r.id,
    title: r.title,
    status: r.status,
    updatedAt: r.updated_at,
    messageCount: r.message_count,
  }));
}

export async function getHistory(sessionId: string): Promise<ChatMessage[]> {
  const raw = await fetchJson<{
    messages: Array<{
      id: string;
      role: string;
      content: string | null;
      tool_calls: unknown;
      charts: unknown;
      validator: unknown;
      cost_usd: number | null;
      duration_ms: number | null;
      created_at: string | null;
    }>;
  }>(`/admin/chat/sessions/${sessionId}/history`);

  return raw.messages.map((m) => ({
    id: m.id,
    role: m.role as 'user' | 'assistant' | 'tool',
    content: m.content || '',
    toolCalls: (m.tool_calls as ChatMessage['toolCalls']) || undefined,
    charts: (m.charts as ChatMessage['charts']) || undefined,
    validator: (m.validator as ChatMessage['validator']) || undefined,
    costUsd: m.cost_usd ?? undefined,
    durationMs: m.duration_ms ?? undefined,
    createdAt: m.created_at ?? undefined,
  }));
}
