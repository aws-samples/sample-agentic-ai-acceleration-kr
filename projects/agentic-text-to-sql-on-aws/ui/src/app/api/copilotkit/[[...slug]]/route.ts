// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * ============================================================================
 * CopilotKit Runtime(v2) 엔드포인트 = 서버 사이드 프록시
 * ============================================================================
 * 브라우저 → (이 route handler) → SigV4 서명 → AgentCore Runtime /invocations(SSE).
 * 브라우저는 AgentCore 를 직접 호출하지 않는다 (자격증명 노출·CORS 불가).
 *
 * v2 진입점(@copilotkit/runtime/v2)을 사용한다:
 *   - LLM 어댑터가 필요 없다 — AG-UI 에이전트가 곧 백엔드다.
 *   - createCopilotRuntimeHandler 가 (Request) => Promise<Response> 핸들러를 반환.
 *
 * 흐름:
 *   1. CopilotKit 클라이언트가 이 엔드포인트(/api/copilotkit)로 요청(AG-UI RunAgentInput).
 *   2. 요청 헤더에서 브라우저 세션 ID(X-Session-Id)를 읽어 AgentCore 세션 헤더로 전달.
 *   3. buildAguiAgent() 로 AG-UI 에이전트 생성 (SigV4 fetch 주입, 어댑터 선택 가능).
 *   4. CopilotRuntime 이 에이전트 SSE 스트림을 CopilotKit 프로토콜로 변환해 브라우저로 스트리밍.
 */

import { CopilotRuntime, createCopilotRuntimeHandler } from '@copilotkit/runtime/v2';
import { getAgentCoreConfig, AGENTCORE_SESSION_HEADER } from '@/lib/agentcore-endpoint';
import { buildAguiAgent } from '@/lib/agui-adapter';

// AgentCore SigV4 서명은 Node 런타임 자격증명(provider chain)이 필요하므로 edge 금지.
export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const BASE_PATH = '/api/copilotkit';

// AgentCore runtimeSessionId 최소 길이.
const MIN_SESSION_ID_LEN = 33;

/**
 * 세션 ID 를 AgentCore 제약(≥33자)에 맞게 정규화한다.
 * - 33자 이상 값: 그대로 사용(브라우저 UUID 등).
 * - 없거나 짧은 값: 안정적으로 패딩하거나 신규 UUID 로 대체.
 *   (짧은 값은 접미사 패딩으로 원 식별자를 보존하되 길이 요건 충족)
 */
function ensureSessionId(raw: string | null): string {
  if (raw && raw.length >= MIN_SESSION_ID_LEN) return raw;
  if (raw && raw.length > 0) {
    // 원 식별자 보존 + 결정적 패딩(같은 입력 → 같은 세션 유지).
    return `${raw}-${'0'.repeat(MIN_SESSION_ID_LEN)}`.slice(
      0,
      Math.max(MIN_SESSION_ID_LEN, raw.length)
    );
  }
  return crypto.randomUUID(); // 36자
}

function buildHandler(req: Request) {
  // ── 인증 훅 자리 (M3) ─────────────────────────────────────────────────────
  // M3 에서 Cognito JWT 검증을 여기에 추가한다:
  //   const token = req.headers.get('authorization');
  //   const claims = await verifyCognitoJwt(token);   // 실패 시 401
  //   → claims.sub 를 AgentCore 로 전파해 row-level 정책 근거로 사용.
  // M1 에서는 인증을 강제하지 않는다 (ARCHITECTURE.md M3 범위).
  // ──────────────────────────────────────────────────────────────────────────

  const { invocationsUrl } = getAgentCoreConfig();

  // 브라우저 세션당 UUID (프로바이더가 X-Session-Id 헤더로 전달). 없으면 신규 생성.
  // ⚠️ AgentCore 는 runtimeSessionId 길이 ≥33 을 강제한다(미만이면 400 VALIDATION_ERROR).
  // 브라우저 UUID(36자)는 충족하지만, 헤더 누락·짧은 값에 대비해 정규화한다.
  const sessionId = ensureSessionId(req.headers.get('x-session-id'));

  const agent = buildAguiAgent({
    url: invocationsUrl,
    headers: {
      accept: 'text/event-stream',
      [AGENTCORE_SESSION_HEADER]: sessionId,
    },
  });

  const copilotRuntime = new CopilotRuntime({
    agents: {
      // CopilotKit 에이전트 이름. 프론트에서 agentId="text_to_sql" 로 참조.
      text_to_sql: agent,
    },
  });

  return createCopilotRuntimeHandler({
    runtime: copilotRuntime,
    basePath: BASE_PATH,
  });
}

// 세션 헤더는 요청마다 다르므로 핸들러를 요청별로 구성해 위임한다.
export const GET = (req: Request) => buildHandler(req)(req);
export const POST = (req: Request) => buildHandler(req)(req);
export const OPTIONS = (req: Request) => buildHandler(req)(req);
