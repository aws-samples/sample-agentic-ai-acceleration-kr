// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { Sha256 } from '@aws-crypto/sha256-js';
import { fromNodeProviderChain } from '@aws-sdk/credential-providers';
import { HttpRequest } from '@aws-sdk/protocol-http';
import { SignatureV4 } from '@aws-sdk/signature-v4';
import { AGENTCORE_SERVICE } from './agentcore-endpoint';

/**
 * @ag-ui/client `HttpAgent` 의 `fetch` 훅에 주입하는 SigV4 서명 fetch.
 *
 * 브라우저는 AgentCore 를 직접 호출하지 못한다(자격증명 노출·CORS). 대신 이 함수가
 * Next.js 서버 런타임에서 실행되어 요청을 SigV4 로 서명한 뒤 실제 AgentCore Runtime
 * 엔드포인트로 전달한다. 스트리밍(SSE) 응답 body 는 그대로 반환해 상위(AG-UI 파서)로
 * 흘려보낸다.
 *
 * 자격증명은 표준 provider chain 을 사용한다:
 *   - ECS: task role (권장, SigV4)
 *   - 로컬: AWS_PROFILE 또는 AWS_ACCESS_KEY_ID/SECRET/SESSION_TOKEN
 */

const region = process.env.AWS_REGION ?? 'us-west-2';

// 자격증명 provider 는 프로세스 수명 동안 재사용(내부 캐시·자동 갱신).
const credentialsProvider = fromNodeProviderChain();

const signer = new SignatureV4({
  service: AGENTCORE_SERVICE,
  region,
  credentials: credentialsProvider,
  sha256: Sha256,
  // uriEscapePath 는 기본값(true) 유지. AgentCore 는 URL-encoded ARN 경로를 canonical 계산 시
  // 재인코딩(%3A→%253A)한 형태로 서명을 기대한다. 검증 결과 false 로 두면 403(서명 불일치)이
  // 발생한다. (SigV4 표준 동작 — S3 등 일부만 uriEscapePath:false)
});

/**
 * `HttpAgentFetchFn` 시그니처: (url, requestInit) => Promise<Response>.
 * requestInit 에는 HttpAgent 가 구성한 method/headers/body(JSON 문자열)가 들어온다.
 */
export async function sigv4Fetch(url: string, init: RequestInit): Promise<Response> {
  const parsed = new URL(url);

  // 헤더를 평문 객체로 정규화 (SignatureV4 는 Record<string,string> 요구).
  const headers = normalizeHeaders(init.headers);
  // host 헤더는 서명 대상이며 fetch 가 자동 설정하지 못하는 경우가 있어 명시.
  headers['host'] = parsed.host;

  // RunAgentInput body 에 actorId 를 주입한다 (orchestrator 확정 계약: forwardedProps.actorId
  // = 사용자 식별자, AgentCore Memory 사용자 격리 근거). M1 은 고정값, M3 에서 Cognito sub 로 교체.
  const body =
    typeof init.body === 'string' ? injectActorId(init.body) : (init.body as string | undefined);

  const query: Record<string, string> = {};
  parsed.searchParams.forEach((value, key) => {
    query[key] = value;
  });

  const httpRequest = new HttpRequest({
    method: init.method ?? 'POST',
    protocol: parsed.protocol,
    hostname: parsed.hostname,
    port: parsed.port ? Number(parsed.port) : undefined,
    path: parsed.pathname,
    query,
    headers,
    body,
  });

  const signed = await signer.sign(httpRequest);

  // 서명된 헤더로 실제 요청 수행. body 는 서명 대상과 동일한 문자열 그대로.
  return fetch(url, {
    method: signed.method,
    headers: signed.headers as HeadersInit,
    body,
    // Node fetch 로 스트리밍 응답을 받기 위한 옵션 (undici 는 자동 스트리밍).
  });
}

/** M1 고정 actorId. M3 에서 Cognito sub 로 교체 (프록시 인증 훅 참조). */
const DEFAULT_ACTOR_ID = process.env.AGENT_ACTOR_ID ?? 'demo-user';

/**
 * RunAgentInput JSON body 의 `forwardedProps.actorId` 를 보장 주입한다.
 * 이미 값이 있으면 존중한다. body 가 JSON 이 아니면 원본 그대로 반환.
 */
function injectActorId(rawBody: string): string {
  try {
    const parsed = JSON.parse(rawBody) as Record<string, unknown>;
    const fwd = (parsed.forwardedProps ?? {}) as Record<string, unknown>;
    if (fwd.actorId == null) fwd.actorId = DEFAULT_ACTOR_ID;
    parsed.forwardedProps = fwd;
    return JSON.stringify(parsed);
  } catch {
    return rawBody;
  }
}

/**
 * SigV4 서명 대상 헤더를 정규화한다.
 *
 * ⚠️ 핵심: 서명한 헤더와 undici fetch 가 실제 전송하는 헤더가 정확히 일치해야 한다.
 * @ag-ui/client `HttpAgent` 는 대소문자가 다른 중복 헤더(`accept` + `Accept`,
 * `Content-Type` 등)를 넘긴다. 이를 그대로 서명하면, undici 가 전송 시 헤더를
 * 소문자로 병합·정규화하면서 canonical headers 가 달라져 403(SignatureDoesNotMatch)이
 * 발생한다. 따라서 **소문자로 통일·중복 제거**한 뒤 서명한다.
 * 또한 내부 전용 헤더(x-session-id)는 AgentCore 로 보내지 않으므로 제거한다.
 */
const INTERNAL_HEADERS = new Set(['x-session-id']);

function normalizeHeaders(input: HeadersInit | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  const put = (key: string, value: string) => {
    const k = key.toLowerCase();
    if (INTERNAL_HEADERS.has(k)) return;
    out[k] = value;
  };
  if (!input) return out;
  if (input instanceof Headers) {
    input.forEach((value, key) => put(key, value));
  } else if (Array.isArray(input)) {
    for (const [key, value] of input) put(key, value);
  } else {
    for (const [key, value] of Object.entries(input)) put(key, String(value));
  }
  return out;
}
