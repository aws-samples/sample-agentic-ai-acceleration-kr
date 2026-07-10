// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import type { Metadata } from 'next';
import localFont from 'next/font/local';
import { GeistMono } from 'geist/font/mono';
import { cookies } from 'next/headers';
import { getMessages } from 'next-intl/server';
import { NextIntlClientProvider } from 'next-intl';
import '@/app/globals.css';

// 본문/UI 기본 — Pretendard Variable(한/영 메트릭 호환, self-host). 시스템 기본
// 폰트를 쓰던 것을 고정해 OS 별 자형 차이로 '짜임새'가 깨지던 근본 원인 해소(§60).
const pretendard = localFont({
  src: './fonts/PretendardVariable.woff2',
  variable: '--font-sans',
  display: 'swap',
  weight: '45 920', // variable axis 범위
});
import { parseJWT } from '@/lib/auth';
import { Sidebar } from '@/components/layout/Sidebar';
import { Header } from '@/components/layout/Header';
import { ToastProvider } from '@/components/common/ToastProvider';
import { ThemeProvider } from '@/components/common/ThemeProvider';
import { ChatShell } from '@/components/chat/ChatShell';
import type { AdminSession } from '@/types/entities';

export const metadata: Metadata = {
  title: 'AWSome AI Gateway Admin',
  description: 'AWSome AI Gateway 관리자 대시보드 — API 키, 예산, 모델, 사용량 분석 통합 관리',
};

export default async function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  // Read JWT cookie — session may be null if unauthenticated (middleware handles redirect)
  const cookieStore = cookies();
  const token = cookieStore.get('admin_jwt')?.value;

  let session: AdminSession | null = null;
  if (token) {
    try {
      session = parseJWT(token);
    } catch {
      // Malformed token — middleware will redirect to login
    }
  }

  const messages = await getMessages();

  return (
    <html
      lang="ko"
      suppressHydrationWarning
      className={`${pretendard.variable} ${GeistMono.variable}`}
    >
      <body>
        <ThemeProvider>
          <NextIntlClientProvider messages={messages} locale="ko">
            <ToastProvider>
              <div className="flex h-screen bg-background">
                <Sidebar role={session?.role} />
                {/* ChatShell: 본문과 퀵챗 패널을 flex 형제로 배치(분할뷰). 채팅
                    열리면 본문이 자동으로 좁아짐(overlay 아님). enabled=ADMIN 만
                    채팅 UI 노출 — Provider 는 항상 감싸 페이지 hook 안전성 유지. */}
                <ChatShell enabled={session?.role === 'ADMIN'}>
                  <div className="flex flex-col flex-1 overflow-hidden">
                    <Header session={session} />
                    <main className="aurora-bg flex-1 overflow-auto p-6">
                      {children}
                    </main>
                  </div>
                </ChatShell>
              </div>
            </ToastProvider>
          </NextIntlClientProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
