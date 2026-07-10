// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

/**
 * server component 페이지가 화면 컨텍스트를 **선언적으로** 등록하는 얇은 client
 * 컴포넌트. 렌더 결과 없음(null) — 마운트되며 useRegisterScreenContext 만 호출.
 *
 * server 페이지는 React Context 에 직접 등록 못 하므로(client hook), 페이지가
 * 이 컴포넌트를 데이터와 함께 렌더한다. 기존 표시 컴포넌트는 무수정(관심사 분리).
 *
 * 예(monitoring/page.tsx):
 *   <RegisterScreenContext page="운영 모니터링" period="최근 1시간" data={summary} />
 */

import { useRegisterScreenContext, type ScreenContext } from './ChatProvider';

export function RegisterScreenContext(ctx: ScreenContext) {
  useRegisterScreenContext(ctx);
  return null;
}
