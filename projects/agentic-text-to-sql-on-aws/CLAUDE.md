# agentic-text-to-sql-on-aws — 프로젝트 컨텍스트

Amazon Bedrock AgentCore 기반 agentic Text-to-SQL 솔루션. **설계는 합의 완료, 구현 단계.**

## 필독 문서 (설계의 단일 진실 원천)

1. `ARCHITECTURE.md` — 전체 설계 확정본. 페르소나(Admin/Manager/User), 확정 결정 D1~D9,
   5계층 설계, 개선 파이프라인(Track A/B), 버저닝 전략(§5.3), 마일스톤 M1~M5, 리스크.
2. `docs/well-architected-checklist.md` — Agentic AI Lens 기반 구현 체크리스트.
   최상위 우선 10개 항목의 마일스톤 매핑 포함. 마일스톤 완료 시 점검.
3. `docs/architecture-review.html` — 사용자와 합의된 인터랙티브 아키텍처 다이어그램.
   구조 변경 시 이 다이어그램도 갱신할 것.

## 핵심 제약 (위반 금지)

- **리전 us-west-2**, 브랜치 `feature/agentic-text-to-sql`
- **IaC는 CDK (TypeScript)**, Python은 uv
- **Tool layer에 Lambda 금지** — 도구는 AgentCore Runtime 호스팅 MCP 서버 → Gateway MCP target
  (예외: Evaluations code-based evaluator만 Lambda — 서비스 규격)
- **Runtime 배포는 컨테이너(ECR) 방식** — direct code upload 금지. docker 빌드, 실패 시 finch 폴백
- **최소 권한**: 컴포넌트별 IAM role 분리, Cedar default-deny, read-only DB 사용자
- **READ-ONLY 4중 방어**: Cedar + LLM 밖 SQL AST validator(SQLGlot) + read-only IAM + DB SELECT-only grant
- 복잡한 모듈은 OOP (추상 base class + 구현체)
- semantic layer 쓰기는 DynamoDB 한 곳만 (dual-write 금지), candidate/published 분리
- 한국어 README, 시크릿은 `.example` 파일, cleanup 섹션 필수
- 사용자를 지칭할 때 페르소나 용어: Admin / Manager / User

## 현재 상태

- [x] 설계 합의 (ARCHITECTURE.md + 다이어그램)
- [ ] M1: CDK 인프라 + 코어 파이프라인 E2E (Aurora 샘플 데이터, OpenSearch 최소형, AG-UI 통합 스파이크)
- [ ] M2: Semantic layer 완성(Neptune·DynamoDB·동기화) + clarification E2E
- [ ] M3: Gateway·Identity·Cedar·Redshift·가드레일 전체
- [ ] M4: Admin panel
- [ ] M5: 개선 파이프라인 (Track A + Track B)
- 향후: `demo/` Jupyter notebook 실습 구조 (ARCHITECTURE.md §10)
