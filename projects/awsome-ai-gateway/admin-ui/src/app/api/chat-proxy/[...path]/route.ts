// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * admin-chat-agent server-side proxy.
 *
 * chat 컴포넌트(client)는 브라우저에서 직접 admin-api 에 닿을 수 없으므로
 * (CORS / 내부 DNS / 인증 쿠키), 기존 teams-proxy 와 동일하게 admin-ui 의
 * server route 가 admin_jwt 쿠키를 admin-api 로 전달한다.
 *
 * 다른 proxy route 와 달리 chat 의 messages 엔드포인트는 SSE(text/event-stream)
 * 스트림이라, 응답 body(ReadableStream)를 버퍼링 없이 그대로 흘려보낸다.
 *
 * 경로 매핑: /api/chat-proxy/admin/chat/...  →  $ADMIN_API_URL/admin/chat/...
 */

import { cookies } from 'next/headers';
import { NextRequest } from 'next/server';

// SSE pass-through 라우트 — Next.js 가 응답을 버퍼링/정적최적화하지 않도록 강제.
// 이 설정이 없으면 standalone 런타임이 ReadableStream(특히 admin-api 의 10초
// `: keepalive` 코멘트)을 즉시 flush 하지 않아, AgentCore 버퍼링으로 인한 긴 침묵
// 구간(case02: sub-agent ~59초)에서 브라우저↔서버 연결이 idle 로 끊긴다(§51).
//   - force-dynamic: 라우트를 매 요청 동적 실행(정적/캐시 최적화 비활성)
//   - runtime nodejs: edge 가 아닌 node 스트리밍(ReadableStream 즉시 전달)
//   - fetchCache no-store: upstream fetch 결과 캐시/버퍼 금지
//   - maxDuration: 리포트 등 장시간(최대 ~300s) 스트림 중 라우트 강제종료 방지
export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';
export const fetchCache = 'force-no-store';
export const maxDuration = 300;

const ADMIN_API_URL = process.env.ADMIN_API_URL || 'http://admin-api:8080';

async function forward(req: NextRequest, pathParts: string[]): Promise<Response> {
  const jwt = cookies().get('admin_jwt')?.value;
  const search = req.nextUrl.search || '';
  const target = `${ADMIN_API_URL}/${pathParts.join('/')}${search}`;

  const headers: Record<string, string> = {
    'Content-Type': req.headers.get('content-type') || 'application/json',
    Accept: req.headers.get('accept') || 'application/json',
  };
  if (jwt) headers.Cookie = `admin_jwt=${jwt}`;

  const init: RequestInit = {
    method: req.method,
    headers,
    cache: 'no-store',
    // @ts-expect-error — Node fetch 에서 streaming 요청 body 처리용
    duplex: 'half',
  };
  if (req.method !== 'GET' && req.method !== 'HEAD') {
    init.body = await req.text();
  }

  const upstream = await fetch(target, init);

  // SSE 등 스트리밍 응답은 body 를 그대로 pass-through (버퍼링 금지)
  return new Response(upstream.body, {
    status: upstream.status,
    headers: {
      'Content-Type':
        upstream.headers.get('content-type') || 'application/json',
      'Cache-Control': 'no-cache, no-transform',
      'X-Accel-Buffering': 'no',
    },
  });
}

export async function GET(
  req: NextRequest,
  { params }: { params: { path: string[] } }
) {
  return forward(req, params.path);
}

export async function POST(
  req: NextRequest,
  { params }: { params: { path: string[] } }
) {
  return forward(req, params.path);
}
