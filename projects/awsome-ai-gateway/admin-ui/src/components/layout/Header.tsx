'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import type { AdminSession } from '@/types/entities';
import { UserRole } from '@/types/enums';
import { LogOut, User } from 'lucide-react';

interface HeaderProps {
  session: AdminSession | null;
}

const ROLE_LABELS: Record<string, string> = {
  [UserRole.ADMIN]: '관리자',
  [UserRole.TEAM_LEADER]: '팀 리더',
  [UserRole.DEVELOPER]: '개발자',
};

// 디자인 시스템 badge 톤 재사용(globals.css 그라데이션). 관리자는 파스텔 핑크
// (destructive 빨강은 '위험'으로 오인 — 권한 표시엔 부적합). 팀리더=amber, 개발자=neutral.
const ROLE_BADGE_CLASSES: Record<string, string> = {
  [UserRole.ADMIN]: 'badge badge-pink',
  [UserRole.TEAM_LEADER]: 'badge badge-amber',
  [UserRole.DEVELOPER]: 'badge badge-neutral',
};

export function Header({ session }: HeaderProps) {

  return (
    <header className="flex h-16 flex-shrink-0 items-center justify-between border-b border-border bg-background px-6">
      {/* Left — page context (breadcrumb placeholder) */}
      <div className="flex items-center gap-2">
        <span className="text-sm text-muted-foreground">AWSome AI Gateway</span>
      </div>

      {/* Right — user info + logout */}
      <div className="flex items-center gap-4">
        {session ? (
          <>
            <div className="flex items-center gap-3">
              <div className="flex h-8 w-8 items-center justify-center rounded-full bg-muted">
                <User size={16} className="text-muted-foreground" aria-hidden="true" />
              </div>
              <div className="flex flex-col">
                <span className="text-sm font-medium text-foreground leading-none">
                  {session.display_name}
                </span>
                <span className="text-xs text-muted-foreground mt-0.5">{session.email}</span>
              </div>
              {session.role && (
                <span className={ROLE_BADGE_CLASSES[session.role] ?? 'badge badge-neutral'}>
                  {ROLE_LABELS[session.role] ?? session.role}
                </span>
              )}
            </div>
            {/* Form POST (not fetch / not GET): browser submits POST and
                natively follows the 303 redirect with the new Set-Cookie,
                so the cookie is actually cleared. */}
            <form method="POST" action="/api/auth/logout" className="contents">
              <button
                type="submit"
                className="inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm text-muted-foreground hover:bg-accent hover:text-accent-foreground transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                aria-label="로그아웃"
              >
                <LogOut size={15} aria-hidden="true" />
                <span>로그아웃</span>
              </button>
            </form>
          </>
        ) : (
          <span className="text-sm text-muted-foreground">비인증 상태</span>
        )}
      </div>
    </header>
  );
}