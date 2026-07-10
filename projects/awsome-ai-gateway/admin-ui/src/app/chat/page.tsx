// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { ChatLayout } from '@/components/chat/ChatLayout';

export const metadata = {
  title: 'BI Insight — AWSome AI Gateway Admin',
};

export default function ChatPage() {
  // 사이드바 Chat = 심층 분석 모드(§55): plan-first + 항상 검증 + insight-first.
  // 퀵챗(드로어, ChatShell variant="drawer")은 quick 기본값 그대로.
  return <ChatLayout mode="deep" />;
}
