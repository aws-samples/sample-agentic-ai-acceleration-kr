// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * dev 로그인을 켤 수 있는 조건을 **한 곳에서** 정한다.
 *
 * dev 로그인은 서명 없는 토큰(`dev.<payload>.sig`)을 발급하고 admin-api 는 그 payload 의
 * role 을 그대로 믿는다. 로컬 개발과 dev 환경용 편의 기능이고, 그 밖에서는 인증이 없는 것과
 * 같다. 예전엔 `DEV_LOGIN_ENABLED === 'true'` 하나만 봤다 — 값 하나가 운영 배포에 잘못
 * 들어가면 누구나 ADMIN 으로 로그인하는 폼이 열렸다.
 *
 * 그래서 스위치와 **환경 이름**(`APP_ENV`)을 함께 본다. admin-api 의
 * `core/dev_login.py` 와 같은 허용 목록·같은 기본값이다 — 두 컨테이너의 판정이 어긋나면
 * 폼은 열리는데 API 는 401 인(또는 그 반대) 상태가 된다.
 *
 * ⚠️ 허용 목록이다. 목록에 없는 이름(prod, production, staging, 오타)은 전부 거부한다.
 * ⚠️ `APP_ENV` 가 없으면 admin-api 의 기본값과 같이 'development' 로 본다(로컬 실행).
 *    Helm 차트는 두 컨테이너에 `global.environment` 를 `APP_ENV` 로 넣는다.
 */

export const DEV_LOGIN_ENVIRONMENTS: readonly string[] = ['dev', 'development', 'local', 'test'];

const DEFAULT_APP_ENV = 'development';

export type DevLoginState =
  | { enabled: true }
  | { enabled: false; reason: 'switch_off' }
  | { enabled: false; reason: 'environment'; appEnv: string };

/** `process.env` 와 테스트용 평범한 객체를 모두 받는다. */
type Env = Readonly<Record<string, string | undefined>>;

export function devLoginState(env: Env = process.env): DevLoginState {
  if (env.DEV_LOGIN_ENABLED !== 'true') {
    return { enabled: false, reason: 'switch_off' };
  }
  const appEnv = env.APP_ENV ?? DEFAULT_APP_ENV;
  if (!DEV_LOGIN_ENVIRONMENTS.includes(appEnv.trim().toLowerCase())) {
    return { enabled: false, reason: 'environment', appEnv };
  }
  return { enabled: true };
}

export function isDevLoginEnabled(env: Env = process.env): boolean {
  return devLoginState(env).enabled;
}
