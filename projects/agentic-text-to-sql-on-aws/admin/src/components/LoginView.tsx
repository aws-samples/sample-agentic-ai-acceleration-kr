// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

/**
 * 로그인 화면 (화면 1).
 *
 * 비밀번호는 서버 route(`/api/auth/login`)로만 전달되고 브라우저에 저장되지 않는다.
 * 발급된 AccessToken 만 sessionStorage 에 보관한다(`lib/client.ts`).
 */

import { useState } from 'react';
import { login, type SessionInfo } from '@/lib/client';
import { Alert } from './ui';

// 파라미터명에 `_` 접두어: 타입 위치 인자를 미사용으로 오탐하는 base no-unused-vars 회피.
export function LoginView({ onLogin }: { onLogin: (_session: SessionInfo) => void }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      onLogin(await login(username.trim(), password));
    } catch (caught) {
      setError((caught as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="adm-login">
      <div className="adm-login-card">
        <h1>Agentic Text-to-SQL 관리자</h1>
        <p className="adm-desc">Manager 또는 Admin 그룹 계정으로 로그인하세요.</p>
        <Alert kind="error" message={error} />
        <form className="adm-login-form" onSubmit={submit}>
          <div className="adm-field">
            <label className="adm-label" htmlFor="adm-username">
              사용자 이름
            </label>
            <input
              id="adm-username"
              className="adm-input"
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
            />
          </div>
          <div className="adm-field">
            <label className="adm-label" htmlFor="adm-password">
              비밀번호
            </label>
            <input
              id="adm-password"
              className="adm-input"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </div>
          <button className="adm-btn adm-btn-primary" type="submit" disabled={busy}>
            {busy ? '로그인 중…' : '로그인'}
          </button>
        </form>
      </div>
    </div>
  );
}
