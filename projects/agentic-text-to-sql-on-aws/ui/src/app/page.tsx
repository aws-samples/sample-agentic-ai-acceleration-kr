// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { Providers } from '@/components/Providers';
import { T2SChat } from '@/components/T2SChat';

export default function HomePage() {
  return (
    <div className="t2s-shell">
      <header className="t2s-header">
        <h1>Agentic Text-to-SQL</h1>
        <p>자연어로 질문하면 에이전트가 SQL 을 생성·실행하고 결과를 설명합니다.</p>
      </header>
      <Providers>
        <div className="t2s-chat">
          <T2SChat />
        </div>
      </Providers>
    </div>
  );
}
