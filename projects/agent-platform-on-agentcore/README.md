# Agentic AI Platform on Amazon Bedrock AgentCore

**Amazon Bedrock AgentCore** 위에 조직용 에이전트 플랫폼을 올리는 레퍼런스 구현입니다.
에이전트를 코드로 배포하는 경로(**AgentCore Runtime**)와 코드 없이 조합하는 경로
(**AgentCore Harness**)를 한 채팅 화면에서 함께 쓰고, 에이전트·MCP 서버·스킬은
**AgentCore Registry**를 카탈로그로 삼아 찾고 승인합니다. 툴은 **AgentCore Gateway**
한 곳으로 모으고, 안전성은 **Amazon Bedrock Guardrails**, 품질은 **AgentCore
Evaluations**, 사용량과 비용은 **Amazon CloudWatch**와 **AWS Cost Explorer**로
측정합니다. 전체 인프라는 Terraform으로 코드화되어 있고 인스톨러 TUI로 한 번에
배포합니다.

<p align="center">
  <a href="https://www.youtube.com/watch?v=CrrE89tJL7g">
    <img src="docs/images/demo-video-thumbnail.png" alt="전체 데모 영상 보기 (YouTube) — Harness 에이전트가 AgentCore Code Interpreter 로 만든 PPTX 를 채팅 옆 artifact 패널에 띄운 화면" width="900">
  </a><br>
  <sub>▶ <a href="https://www.youtube.com/watch?v=CrrE89tJL7g">전체 데모 영상 보기 (YouTube)</a> · Harness 에이전트가 AgentCore Code Interpreter 로 만든 PPTX 를 채팅 옆 artifact 패널에 띄운 화면</sub>
</p>

## 목차

- [이 샘플이 다루는 것](#이-샘플이-다루는-것)
- [아키텍처](#아키텍처)
- [사용한 AWS 서비스](#사용한-aws-서비스)
- [기능](#기능)
- [채팅 한 턴의 흐름](#채팅-한-턴의-흐름)
- [보안 설계](#보안-설계)
- [저장소 구조](#저장소-구조)
- [배포](#배포)
- [로컬 개발](#로컬-개발)
- [정리](#정리)
- [문서](#문서)

## 이 샘플이 다루는 것

에이전트를 한두 개 만드는 것과 조직 안에서 여러 팀이 에이전트를 만들고 나눠 쓰는 것은
다른 문제입니다. 이 샘플은 후자에서 반복되는 질문에 AgentCore 서비스로 답합니다.

| 질문 | 이 샘플의 답 |
| --- | --- |
| 에이전트를 어디서 실행하나 | 코드형은 AgentCore Runtime, 구성형은 AgentCore Harness. 서버가 두 경로를 하나의 스트림으로 정규화 |
| 어떤 에이전트·툴·스킬이 있는지 어떻게 아나 | AgentCore Registry 를 카탈로그로 쓰고 하이브리드 검색과 승인 상태로 관리 |
| 툴을 에이전트마다 붙여야 하나 | AgentCore Gateway 하나에 Lambda 툴·내장 툴·Knowledge Base 를 MCP 로 노출하고 IAM(SigV4)으로 호출 |
| 사내 문서는 어떻게 붙이나 | 사용자가 만든 Amazon Bedrock Knowledge Base 를 게이트웨이 툴로 감싸 Harness 에 부착 |
| 안전한가, 잘 동작하나, 얼마 드나 | Guardrails 개입 집계, AgentCore Evaluations 점수·실패 분석, 턴 단위 사용량 원장과 청구서 대조 |
| 누가 무엇을 할 수 있나 | Amazon Cognito 또는 Microsoft Entra ID 로 로그인, 서버에서 역할(admin/user)과 리소스 소유권 강제 |
| 팀마다 쓸 수 있는 모델·툴을 어떻게 가르나 | Cognito 그룹 `team:<name>` 을 팀으로 읽어 팀별 Harness 실행 역할·허용 모델·레코드 가시성을 두고, Gateway 툴 권한은 AgentCore Policy(Cedar)로 강제 |

## 아키텍처

<p align="center">
  <img src="docs/images/architecture.png" alt="전체 구성 — ALB 뒤의 web·server(ECS Fargate)가 AgentCore Runtime·Harness를 호출하고, 둘 다 같은 AgentCore Gateway를 SigV4로 씁니다" width="900"><br>
  <sub>전체 구성 — ALB 뒤의 web·server(ECS Fargate)가 AgentCore Runtime·Harness를 호출하고, 둘 다 같은 AgentCore Gateway를 SigV4로 씁니다</sub>
</p>

```mermaid
flowchart TB
    User([사용자])

    subgraph edge["엣지"]
        ALB["Application Load Balancer<br/>:80/:443 앱 · :8081/:8443 MCP Apps 샌드박스"]
    end

    subgraph app["애플리케이션 계층 · VPC 프라이빗 서브넷 · Amazon ECS on AWS Fargate"]
        Web["web · Next.js"]
        Server["server · FastAPI"]
    end

    subgraph idp["인증"]
        Cognito["Amazon Cognito<br/>User Pool"]
        Entra["Microsoft Entra ID<br/>(OIDC, 선택)"]
    end

    subgraph agentcore["에이전트 실행 계층 · Amazon Bedrock AgentCore"]
        Registry["Registry<br/>A2A · MCP · Agent Skills"]
        Runtime["Runtime<br/>Strands 에이전트"]
        Harness["Harness<br/>코드 없는 에이전트"]
        Memory["Memory"]
        Gateway["Gateway<br/>(AWS_IAM authorizer)"]
        Eval["Evaluations"]
    end

    subgraph tools["툴 계층"]
        Lambda["AWS Lambda 툴<br/>web_search · fetch_url · time · calc"]
        CI["Code Interpreter"]
        Browser["Browser"]
        WebSearch["Web Search 커넥터"]
        KB["Amazon Bedrock<br/>Knowledge Bases"]
    end

    subgraph model["모델 계층 · Amazon Bedrock"]
        FM["Anthropic Claude<br/>(global 추론 프로파일)"]
        GR["Guardrails"]
    end

    subgraph data["데이터 계층"]
        DDB[("Amazon DynamoDB<br/>threads · artifacts · usage · users …")]
        S3[("Amazon S3<br/>artifacts · attachments · knowledge · skills")]
    end

    subgraph ops["운영 계층"]
        CW["Amazon CloudWatch<br/>Metrics · Logs · Alarms"]
        CE["AWS Cost Explorer<br/>· Price List"]
    end

    User --> ALB --> Web --> Server
    Server --> Cognito & Entra
    Server --> Registry
    Server -->|InvokeAgentRuntime| Runtime
    Server -->|InvokeHarness| Harness
    Server --> DDB & S3
    Server --> Eval
    Server -.-> CW & CE
    Runtime --> Memory
    Runtime -->|SigV4| Gateway
    Harness -->|SigV4| Gateway
    Gateway --> Lambda & CI & Browser & WebSearch & KB
    Runtime --> FM
    Harness --> FM
    FM --- GR
```

플랫폼은 여섯 개 계층으로 나뉩니다.

- **엣지** — Application Load Balancer 가 `web` 하나만 외부에 노출합니다. 두 번째
  리스너는 MCP Apps 의 HTML 을 다른 출처(origin)의 iframe 에 띄우기 위한 샌드박스
  출처입니다. ACM 인증서를 주면 HTTPS 리스너가 생기고 HTTP 는 301 로 리다이렉트됩니다.
- **애플리케이션** — Next.js(`web`)와 FastAPI(`server`)가 프라이빗 서브넷의 Fargate
  태스크로 돕니다. `web` 은 `/api/*`·`/threads/*` 를 내부 DNS 로 `server` 에 프록시하고,
  `server` 가 인증·권한·스트리밍 정규화·저장을 맡습니다. 브라우저는 AWS 자격 증명을
  갖지 않습니다.
- **에이전트 실행** — 모든 추론은 AgentCore 에서 일어납니다. `server` 는 스레드에 고정된
  Registry 레코드를 보고 Runtime 또는 Harness 를 호출하고, 결과 이벤트를 하나의 SSE
  형식으로 바꿔 내보냅니다.
- **툴** — Runtime 과 Harness 는 같은 AgentCore Gateway 를 각자의 실행 역할로 SigV4
  서명해 호출합니다. 툴 구현(Lambda, 내장 툴, Knowledge Base)은 게이트웨이 뒤에 숨습니다.
- **모델** — Amazon Bedrock 의 Anthropic Claude 모델을 global 교차 리전 추론
  프로파일로 호출하고, 런타임의 모든 모델 호출에 Bedrock Guardrail 을 붙입니다.
- **데이터·운영** — 대화·artifact 메타데이터·사용량 원장은 DynamoDB, 파일 바이트는
  S3 에 둡니다. 사용량·비용·품질 지표는 CloudWatch, Cost Explorer, AgentCore Evaluations
  에서 읽어 관리자 화면(Insights)에 모읍니다.

백엔드 내부 구조(routes → services → repositories + agents)는
[`server/ARCHITECTURE.md`](server/ARCHITECTURE.md)에 있습니다.

## 사용한 AWS 서비스

| 영역 | 서비스 | 이 샘플에서의 역할 |
| --- | --- | --- |
| 에이전트 | **Amazon Bedrock AgentCore Runtime** | Strands Agents SDK 로 만든 에이전트(`bap_default`)를 컨테이너로 호스팅. 세션 단위 격리 |
| | **Amazon Bedrock AgentCore Harness** | 시스템 프롬프트·모델·툴·스킬만으로 에이전트를 생성·수정(`CreateHarness`/`UpdateHarness`) |
| | **Amazon Bedrock AgentCore Registry** | A2A 에이전트·MCP 서버·Agent Skills 레코드의 카탈로그, 하이브리드 검색, 승인 워크플로 |
| | **Amazon Bedrock AgentCore Gateway** | Lambda·내장 툴·Knowledge Base 를 MCP 엔드포인트로 노출, `AWS_IAM` 인증, REQUEST 인터셉터 |
| | **Amazon Bedrock AgentCore Memory** | Runtime 에이전트의 단기 대화 이벤트와 세션 요약(SUMMARIZATION 전략) |
| | **Amazon Bedrock AgentCore Code Interpreter / Browser** | 대화별 샌드박스에서 코드 실행·파일 생성, 헤드리스 브라우징과 스크린샷 |
| | **Amazon Bedrock AgentCore Evaluations** | 레코드 단위 Batch Evaluation(내장 평가자)과 실패 원인 분석(Insights, preview) |
| | **Amazon Bedrock AgentCore Observability** | 런타임 span·vended `USAGE_LOGS` 를 CloudWatch Logs 로 전달 |
| 모델 | **Amazon Bedrock** | Anthropic Claude Sonnet 5.5 / Opus 5.5(global 추론 프로파일), 프롬프트 캐싱, 리즈닝 |
| | **Amazon Bedrock Guardrails** | 콘텐츠 필터(HATE·INSULTS·SEXUAL·VIOLENCE·MISCONDUCT)와 프롬프트 공격 차단 |
| | **Amazon Bedrock Knowledge Bases** | 사용자별 관리형(MANAGED) 지식 베이스, S3 데이터 소스 인덱싱 |
| 컴퓨팅·네트워크 | **Amazon ECS on AWS Fargate**, **Amazon ECR** | `web`·`server` 컨테이너 실행과 이미지 저장 |
| | **Elastic Load Balancing (ALB)**, **Amazon VPC** | 공개 진입점, 프라이빗 서브넷 + NAT 게이트웨이 |
| | **AWS Lambda** | 게이트웨이 툴, 내장 툴 브리지, 호출자 식별 인터셉터 |
| 인증 | **Amazon Cognito** | User Pool 로그인, `admin` 그룹 기반 역할 |
| 데이터 | **Amazon DynamoDB** | 스레드·메시지, artifact 메타데이터, Knowledge Base 상태, 사용량 원장, 사용자 설정 (PITR·삭제 보호) |
| | **Amazon S3** | artifact·첨부 파일·지식 문서·스킬 번들 원본 |
| 운영·비용 | **Amazon CloudWatch** | 지표·Logs Insights 드릴다운, ALB 5xx·헬스·DynamoDB 스로틀 알람 |
| | **Amazon SNS** | 알람 알림 |
| | **AWS Cost Explorer**, **AWS Price List API** | 실제 청구액 대조와 모델 요율 학습, `Platform` 비용 할당 태그로 스택 단위 스코핑 |
| | **AWS IAM** | 런타임·Harness·게이트웨이·Knowledge Base·ECS 태스크별 최소 권한 역할 |

외부 서비스로는 Microsoft Entra ID(OIDC 로그인, 선택)를 쓸 수 있습니다. 웹 검색은 외부 API
키 없이 AgentCore Web Search 로 합니다.

## 기능

### 1. 두 가지 실행 경로 — AgentCore Runtime 과 Harness

<p align="center">
  <img src="docs/images/execution-paths.png" alt="두 실행 경로 — Runtime(코드 배포)과 Harness(코드 없는 조합)의 이벤트를 server가 하나의 SSE 스트림으로 정규화합니다" width="900"><br>
  <sub>두 실행 경로 — Runtime(코드 배포)과 Harness(코드 없는 조합)의 이벤트를 server가 하나의 SSE 스트림으로 정규화합니다</sub>
</p>

- **AgentCore Runtime** — `agent-runtime/` 의 Strands 에이전트를 컨테이너로 배포합니다.
  `scripts/deploy.sh` 한 번이 configure → launch → READY 대기 → Registry 등록까지 합니다.

  기본 에이전트 `bap_default` 는 Gateway 툴(AgentCore Web Search, `fetch_url` 등)과 artifact
  툴을 쓰는 단일 Strands 에이전트입니다. `agents/` 에 모듈을 추가하면 같은 스크립트로
  별도 Runtime 에 배포됩니다.

  런타임은 AgentCore Memory 에서 최근 대화와 세션 요약을 복원하고, 사용자(`actor_id`)마다
  메모리를 분리합니다. 호출 페이로드로 턴 단위 모델·시스템 프롬프트를 받으므로, 채팅의
  **스레드 오버라이드**로 런타임 에이전트도 harness 처럼 이 대화만 다른 모델로 돌릴 수
  있습니다(운영자 허용 목록 `allowed_models` 안에서). 기본 런타임을 가리키는 레코드가
  **기본 에이전트**로 선택기 맨 위에 고정되고, 아무것도 고르지 않은 새 채팅은 그
  에이전트로 시작합니다.
- **AgentCore Harness** — 코드 없이 시스템 프롬프트·모델·툴·스킬·반복 횟수로 정의하는
  관리형 에이전트입니다. 메모리와 실행 루프를 AgentCore 가 소유합니다. `InvokeHarness`
  의 요청별 `model`/`systemPrompt` 오버라이드를 써서, harness 정의는 그대로 둔 채 한
  스레드에서만 모델이나 지시를 바꿔 볼 수 있습니다.
- 두 경로의 이벤트(텍스트, 리즈닝, 툴 호출·결과, 가드레일 개입, 파일)는 서버가 Strands
  이벤트 형식으로 정규화해 `POST /threads/{id}/runs/stream` 하나로 내보냅니다. 프론트는
  어느 경로인지 몰라도 같은 화면을 그립니다.
- 런은 응답 연결이 아니라 서버의 백그라운드 태스크가 끝까지 소비합니다(`RunBroker`).
  탭을 닫거나 다른 스레드로 가도 에이전트는 계속 돌고 턴은 DynamoDB 에 저장됩니다.
  `busy` 인 스레드를 다시 열면 `GET /threads/{id}/runs/stream` 이 지금까지의 이벤트를
  재생한 뒤 라이브로 이어 주고, Stop 은 `POST /threads/{id}/runs/cancel` 로 런을 실제로
  중단합니다(`interrupted`).

<table>
  <tr>
    <td width="50%"><img src="docs/images/chat-runtime-tools-reasoning.png" alt="툴 호출 박스와 리즈닝 블록"></td>
    <td width="50%"><img src="docs/images/chat-guardrail-blocked.png" alt="Bedrock Guardrail이 프롬프트 공격을 차단한 턴"></td>
  </tr>
  <tr>
    <td align="center"><sub>툴 호출 박스와 리즈닝 블록</sub></td>
    <td align="center"><sub>Bedrock Guardrail 이 프롬프트 공격을 차단한 턴</sub></td>
  </tr>
</table>

### 2. 에이전트 카탈로그 — AgentCore Registry

- A2A 에이전트, MCP 서버, Agent Skills 세 가지 레코드를 하이브리드 검색(semantic +
  keyword)으로 찾습니다. 레코드 설명이 검색 관련도를 결정합니다.
- 레코드는 DRAFT → 제출 → 승인/반려 → 폐기의 상태를 가지며, 상태 변경은 admin 만
  합니다. 채팅에서 고를 수 있는 에이전트는 Registry 레코드입니다.
- Registry 에는 배포 이벤트 훅이 없으므로 등록 경로를 플랫폼이 붙입니다: Harness 생성 시
  자동 등록, Runtime 배포 스크립트의 sync, 화면의 **Sync deployed**.
- Agent Skills 는 마크다운 또는 zip 번들로 올리고 AgentSkills 규격으로 검증합니다. 번들
  원본은 S3 스킬 버킷에 두고 레코드는 그 위치만 가리킵니다.
- 레코드 상세에서 그 에이전트의 사용량과 AgentCore Evaluations 점수를 보고, MCP 레코드의
  툴은 스키마대로 인자를 채워 시험 호출합니다.
- 레지스트리의 커스텀 메타데이터 스키마(owner·team·tier 등)를 폼으로 받아 레코드에 붙이고
  검색 필터로 씁니다. MCP·A2A 레코드는 엔드포인트에서 정의를 동기화할 수 있고, 승인된
  레코드를 편집해도 승인본이 계속 제공되는 동안 채팅이 끊기지 않습니다.
- 레지스트리 자체가 MCP 서버입니다. 플랫폼 게이트웨이에 `registry` 타깃으로 붙어 에이전트가
  대화 중에 카탈로그를 검색하고, 화면의 **IDE 연결**로 Kiro·Claude Code 에서도 같은 엔드포인트에
  붙습니다.

<table>
  <tr>
    <td width="50%"><img src="docs/images/registry-search.png" alt="자연어 하이브리드 검색 — 에이전트·스킬·MCP 레코드"></td>
    <td width="50%"><img src="docs/images/registry-detail-usage.png" alt="레코드 상세 — 사용량과 Batch Evaluation 점수"></td>
  </tr>
  <tr>
    <td align="center"><sub>자연어 하이브리드 검색 — 에이전트·스킬·MCP 레코드</sub></td>
    <td align="center"><sub>레코드 상세 — 사용량과 Batch Evaluation 점수</sub></td>
  </tr>
</table>

### 3. 코드 없는 에이전트 조합 — AgentCore Harness + Registry

<p align="center">
  <img src="docs/images/harness-compose-flow.png" alt="Harness 조합 — Registry의 재료로 CreateHarness를 호출하고, 결과를 A2A 레코드로 다시 등록합니다" width="900"><br>
  <sub>Harness 조합 — Registry 의 재료로 CreateHarness 를 호출하고, 결과를 A2A 레코드로 다시 등록합니다</sub>
</p>

- `/harness` 화면에서 Registry 의 MCP 서버·Agent Skills, AWS Agent Toolkit 스킬 카탈로그,
  내장 툴(Code Interpreter·Browser), 사용자의 Knowledge Base 게이트웨이를 골라
  `CreateHarness` 를 호출합니다. 만들어진 harness 는 A2A 레코드로 Registry 에 다시
  등록되어 곧바로 채팅에서 선택됩니다.
- admin 은 `UpdateHarness` 로 기존 harness 를 제자리에서 수정합니다. AgentCore 가 새
  버전을 만들고 READY 가 되면 DEFAULT 엔드포인트를 옮기므로 ARN 과 Registry 레코드는
  그대로입니다.

<p align="center">
  <img src="docs/images/harness-compose.png" alt="/harness 조합 폼 — Registry 스킬·Knowledge Base·내장 툴을 골라 에이전트를 만듭니다" width="900"><br>
  <sub>/harness 조합 폼 — Registry 스킬·Knowledge Base·내장 툴을 골라 에이전트를 만듭니다</sub>
</p>

### 4. 툴 — AgentCore Gateway

툴은 모두 AgentCore Gateway 의 MCP 엔드포인트로 제공되고, authorizer 는 `AWS_IAM` 입니다.
Runtime 과 Harness 는 각자의 실행 역할로 SigV4 서명해 호출하므로 툴 호출에 사용자
토큰이 흐르지 않습니다.

| 게이트웨이 | 타깃 | 툴 |
| --- | --- | --- |
| Lambda 툴 게이트웨이 (Terraform) | AWS Lambda + Web Search 커넥터 | 웹 검색(**AgentCore Web Search**), `fetch_url`, `current_time`, `calculate`, artifact 툴 |
| 내장 툴 게이트웨이 (boto3 스크립트) | Web Search 커넥터 + Lambda 브리지 | 웹 검색, **AgentCore Code Interpreter**, **AgentCore Browser** |
| Knowledge Base 게이트웨이 (KB 마다 하나) | Bedrock Knowledge Bases 커넥터 | 해당 KB 검색 |

- Code Interpreter 와 Browser 는 게이트웨이 타깃 타입이 없는 데이터 플레인 API 라서
  Lambda 가 MCP 툴로 감쌉니다. 리소스는 하나를 공유하고 **세션**이 격리 단위이며,
  게이트웨이의 REQUEST 인터셉터가 호출자 식별자를 실어 대화마다 별도 세션에 붙게 합니다.
- Code Interpreter 가 만든 파일(pptx·docx·pdf·이미지)은 artifact 로, Browser 의
  스크린샷은 툴 결과 박스 안의 이미지로 채팅에 뜹니다.

<p align="center">
  <img src="docs/images/chat-browser-screenshot.png" alt="게이트웨이 Browser 툴이 찍은 스크린샷이 채팅과 패널에 뜬 턴" width="900"><br>
  <sub>게이트웨이 Browser 툴이 찍은 스크린샷이 채팅과 패널에 뜬 턴</sub>
</p>

### 5. 사내 문서 연결 — Amazon Bedrock Knowledge Bases

- `/knowledge` 에서 사용자가 지식 베이스를 만들고 문서를 올리거나 지정한 S3 버킷을
  동기화합니다. 서버가 백그라운드에서 **관리형(MANAGED) Knowledge Base → S3 데이터
  소스 → 전용 AgentCore Gateway → Knowledge Base 커넥터 타깃**을 멱등하게 프로비저닝합니다.
- 게이트웨이 타깃은 해당 Knowledge Base ID 로 고정되므로, 한 사용자의 게이트웨이로는
  다른 사용자의 지식 베이스를 조회할 수 없습니다. 완성된 게이트웨이는 Harness 에 툴로
  부착됩니다.
- 지식 베이스는 소유자만 읽고 쓰며, `shared` 로 표시하면 모두 읽을 수 있습니다.

<p align="center">
  <img src="docs/images/knowledge-detail.png" alt="Knowledge Base 상세 — 올린 문서와 인덱싱 상태" width="900"><br>
  <sub>Knowledge Base 상세 — 올린 문서와 인덱싱 상태</sub>
</p>

### 6. 산출물 — Artifacts (Amazon S3)

- 에이전트가 만든 문서(markdown·code·html·svg·mermaid·csv·json)와 샌드박스 파일은 채팅
  본문이 아니라 우측 패널에 렌더링되고, **서버가** S3 에 저장해 presigned URL 로
  공유합니다. 저장을 서버가 하므로 런타임에 S3 권한을 줄 필요가 없습니다.
- 첨부한 이미지·문서는 S3 에 저장하고 메시지에는 참조만 남긴 채 모델 입력으로 전달합니다.

<table>
  <tr>
    <td width="50%"><img src="docs/images/chat-artifact-mermaid.png" alt="mermaid artifact — 버전(v1·v2)과 Preview/Source"></td>
    <td width="50%"><img src="docs/images/chat-code-interpreter-svg.png" alt="Code Interpreter 로 계산하고 SVG 그래프를 패널에 띄운 턴"></td>
  </tr>
  <tr>
    <td align="center"><sub>mermaid artifact — 버전(v1·v2)과 Preview/Source</sub></td>
    <td align="center"><sub>Code Interpreter 로 계산하고 SVG 그래프를 패널에 띄운 턴</sub></td>
  </tr>
</table>

### 7. 대화형 툴 UI — MCP Apps

- MCP Apps(SEP-1865)는 MCP 서버가 제공하는 HTML UI 를 호스트가 렌더링하는 MCP 공식
  확장입니다. 앱은 `_meta.ui.resourceUri` 를 선언한 툴의 호출에 붙고, 규격대로 ALB 의
  별도 리스너(다른 출처)의 샌드박스 iframe 에서 앱마다 계산한 CSP 로 렌더링됩니다.
- 데모 서버 `mcp-apps-server/` 는 CloudWatch 지표(모델별 토큰, 에이전트·툴별 호출과
  지연)를 차트로 보여 주는 플랫폼 텔레메트리 앱을 제공하며, AgentCore Runtime 에 배포해
  게이트웨이 타깃으로 붙입니다.

<table>
  <tr>
    <td width="50%"><img src="docs/images/mcp-apps-sequence.png" alt="툴 호출에서 iframe 마운트까지의 메시지 흐름"></td>
    <td width="50%"><img src="docs/images/chat-mcp-app-telemetry.png" alt="채팅 안에 렌더링된 플랫폼 텔레메트리 앱"></td>
  </tr>
  <tr>
    <td align="center"><sub>툴 호출에서 iframe 마운트까지의 메시지 흐름</sub></td>
    <td align="center"><sub>채팅 안에 렌더링된 플랫폼 텔레메트리 앱</sub></td>
  </tr>
</table>

### 8. 거버넌스·관측 — Insights

`/insights`(admin)는 사용량·비용·안전·품질을 한 화면에 모읍니다.

<p align="center">
  <img src="docs/images/insights-overview.png" alt="/insights — 핵심 지표와 에이전트별 사용량" width="900"><br>
  <sub>/insights — 핵심 지표와 에이전트별 사용량</sub>
</p>

- **사용량과 모델 비용** — 턴마다 토큰 사용량을 DynamoDB 원장에 멱등하게 기록하고,
  쓰기 시점의 요율로 가격을 매깁니다. 요율표는 **AWS Cost Explorer** 의 실제 청구액과
  주기적으로 대조해 자동으로 보정되며(학습된 요율), 비어 있는 요율은 관리자가
  청구서·**AWS Price List API**·직접 입력으로 채웁니다. 청구 합계는 `Platform` 비용
  할당 태그로 이 스택만 읽습니다.
- **런타임 비용** — AgentCore Runtime 의 vCPU·메모리 사용량을 CloudWatch 지표에서 일
  단위로 수집하고, vended `USAGE_LOGS` 로 세션(스레드) 단위까지 나눕니다.
- **안전** — 스트림의 **Bedrock Guardrails** 트레이스에서 개입률, 필터별·에이전트별·
  사용자별 개입, 가드레일이 적용되지 않은 에이전트를 집계합니다. 매치된 텍스트는
  저장하지 않습니다.
- **품질** — **AgentCore Evaluations** 의 Batch Evaluation 으로 에이전트별 내장 평가자
  점수를 받고, 같은 API 의 Insights(preview)로 실패 원인·사용자 의도·실행 패턴
  클러스터를 받습니다. 스레드 단위 span 은 **CloudWatch Logs Insights** 로 드릴다운합니다.
- **사람 단위 거버넌스** — 사용자별 사용량, 어떤 MCP·스킬·툴이 어느 에이전트에
  재사용되는지, 하루 비용이 평소 중앙값에서 크게 벗어난 날의 이상치 알림.

`/settings`(admin)에서는 일반 사용자에게 보일 메뉴, 모델 요율표, MCP Inspector(임의의 MCP
서버에 연결해 툴을 직접 호출해 보는 도구)를 관리합니다.

<p align="center">
  <img src="docs/images/registry-mcp-tool-tryout.png" alt="MCP 툴 시험 호출 — 스키마대로 인자를 채워 실행하고 원본 결과를 봅니다" width="900"><br>
  <sub>MCP 툴 시험 호출 — 스키마대로 인자를 채워 실행하고 원본 결과를 봅니다</sub>
</p>

## 채팅 한 턴의 흐름

```mermaid
sequenceDiagram
    autonumber
    actor U as 사용자
    participant W as web (Next.js)
    participant S as server (FastAPI)
    participant D as DynamoDB / S3
    participant A as AgentCore Runtime / Harness
    participant G as AgentCore Gateway
    participant B as Bedrock (Claude + Guardrail)

    U->>W: 메시지 전송
    W->>S: POST /threads/{id}/runs/stream (Bearer JWT)
    S->>S: JWT 검증 (iss 로 Cognito / Entra JWKS 선택)
    S->>D: 스레드 소유권 확인, 고정된 Registry 레코드 조회
    S->>A: InvokeAgentRuntime 또는 InvokeHarness
    loop 에이전트 루프
        A->>B: 모델 호출 (Guardrail 적용)
        B-->>A: 텍스트 · 리즈닝 · 툴 호출
        A->>G: tools/call (실행 역할 SigV4)
        G-->>A: 툴 결과
    end
    A-->>S: 이벤트 스트림
    S-->>W: 정규화된 SSE (텍스트 · 툴 · artifact · 가드레일)
    S->>D: 메시지 · artifact(S3) · 사용량 원장 저장
    W-->>U: 답변과 artifact 패널 렌더링
```

## 보안 설계

- **인증** — Amazon Cognito User Pool 또는 Microsoft Entra ID(OIDC, Authorization Code +
  PKCE). 서버는 토큰의 `iss` 로 공급자를 가려 각자의 JWKS 로 서명을 검증합니다.
- **인가** — 역할(admin/user)과 리소스 소유권은 서버에서 강제합니다. 스레드·지식 베이스는
  생성자만 읽고 쓰며, Registry 상태 변경·Harness 수정과 삭제·Insights·Settings 는 admin
  입니다. 프론트의 역할 게이트는 화면 편의일 뿐입니다.
- **실행 대상 고정** — 어떤 런타임·모델로 실행할지는 서버가 Registry 레코드와 허용 목록으로
  정합니다. 브라우저가 보낸 ARN 이나 모델 ID 는 쓰지 않습니다.
- **네트워크** — `web`·`server` 는 프라이빗 서브넷에 있고 ALB 만 공개됩니다. MCP Apps 의
  HTML 은 별도 출처의 샌드박스 iframe 에서 실행됩니다.
- **최소 권한 IAM** — ECS 태스크, Runtime 실행 역할, Harness 역할, 게이트웨이 역할,
  Knowledge Base 서비스 역할을 각각 분리합니다. 게이트웨이는 `AWS_IAM` 인증이라
  사용자 토큰이 툴 계층으로 내려가지 않습니다.
- **모델 안전** — Bedrock Guardrail 이 런타임의 모든 모델 호출 입력·출력에 적용됩니다.
- **데이터 보호** — DynamoDB 테이블은 PITR 과 삭제 보호가 켜져 있고, artifact 공유는
  만료되는 presigned URL 로만 합니다.

> [!IMPORTANT]
> 기본 배포는 ALB 를 평문 HTTP 로 엽니다. 실제 사용자에게 열기 전에 ACM 인증서
> (`certificate_arn`)와 도메인(`public_host`)을 설정해 HTTPS 로 전환하고, ALB 보안
> 그룹의 허용 대역을 좁히세요. 로컬 개발용 인증 우회 플래그(`AUTH_ENFORCED=false`,
> `NEXT_PUBLIC_AUTH_DISABLED=true`)는 배포 환경에서 쓰지 않습니다.

## 저장소 구조

| 경로 | 내용 | 스택 |
| --- | --- | --- |
| `web/` | 채팅 UI, Registry·Harness·Knowledge·Insights·Settings 화면, 로그인 | Next.js 16, React 19, TypeScript, Radix UI, Tailwind |
| `server/` | 인증·권한, 스트리밍 정규화, Registry·Harness·Knowledge Base·MCP·Insights API | FastAPI, boto3 |
| `agent-runtime/` | AgentCore Runtime 기본 에이전트와 배포 스크립트 | Strands Agents SDK |
| `mcp-apps-server/` | MCP Apps 데모 서버(플랫폼 텔레메트리 앱) | MCP Python SDK (streamable-http), Chart.js |
| `infra/` | 전체 AWS 인프라와 내장 툴 게이트웨이 배포 스크립트 | Terraform, boto3 |
| `installer/` | 배포 자동화 TUI (`./install.sh`) | Python 3.13, Textual |

## 배포

**[`DEPLOYMENT.md`](DEPLOYMENT.md)** 에 전체 절차가 있습니다. 기반 배포(state 백엔드 →
인프라 → 이미지)만으로 Harness 에이전트·Registry·Gateway 툴·Knowledge Base·Insights 가
동작하고, Runtime 에이전트·내장 툴 게이트웨이·MCP 서버(MCP Apps 예제 포함)는 필요할 때 추가로
배포합니다. 아래는 요약입니다.

### 사전 요구사항

- AWS 계정과 관리자 수준 자격 증명, 리전 `ap-northeast-1`(도쿄, 기본값)
- Amazon Bedrock 에서 Anthropic Claude 모델 접근 권한
- Terraform, AWS CLI v2, Docker(`linux/amd64` 빌드 가능), Python 3.13, Node.js + Yarn

### 인스톨러 TUI (권장)

```bash
./install.sh                 # 배포 TUI 실행
./install.sh --dry-run       # 실행 없이 조립된 명령만 확인
```

인스톨러는 Terraform 변수를 폼으로 받아 `terraform.tfvars` 와 `.env` 를 채우고, state
백엔드·인프라 적용·이미지 빌드·AgentCore Memory 생성을 순서대로 실행합니다. Runtime
에이전트 배포는 그다음 `agent-runtime/scripts/deploy.sh` 로 합니다. 진행 상태를 따로
저장하지 않고 매번 실제 리소스를 조회하므로, CLI 로 중간까지 진행한 환경에서도
이어집니다. 접속 주소는 `terraform output alb_url` 입니다. 화면 캡처와 함께 단계별로
따라가는 안내는 [`docs/installer-guide.md`](docs/installer-guide.md) 에 있습니다.

인스톨러 없이 진행하는 수동 절차, Entra ID 설정, 모듈 목록은 [`infra/README.md`](infra/README.md)에 있습니다.

### 비용

이 샘플은 사용하지 않아도 비용이 나는 리소스(NAT 게이트웨이, ALB, Fargate 태스크)를
포함하고, 모델 호출·AgentCore Runtime·Evaluations·Knowledge Bases 는 사용량에 따라
과금됩니다. 쓰지 않는 동안은 다음으로 태스크를 0 으로 내리고 NAT·EIP 를 지울 수
있습니다(데이터·ALB·AgentCore 리소스는 보존).

```bash
infra/scripts/park.sh down   # 일시 중지
infra/scripts/park.sh up     # 재개
```

## 로컬 개발

```bash
cp server/env.example server/.env    # 값 채우기 (배포 스택 값은 terraform output 에서)
./run.sh                             # backend(:8000) + frontend(:3000) + 샌드박스(:3001)
./run.sh --install                   # 의존성(pip/yarn) 설치 후 실행
```

브라우저는 `http://localhost:3000` 만 엽니다. 각 기능은 환경 변수로 켜지므로 값이 없는
기능만 꺼지고 나머지는 그대로 뜹니다. 필요한 값과 효과는
[`server/env.example`](server/env.example)에 있습니다.

## 정리

Terraform 밖에서 만든 리소스를 먼저 지웁니다: 화면에서 만든 Harness·Knowledge Base 는
각 화면의 삭제로, 내장 툴 게이트웨이는 `infra/builtin_tools_gateway/deploy.py down` 으로,
`deploy.sh` 로 배포한 Runtime 에이전트는 AgentCore 콘솔이나 CLI 로 지웁니다.

그다음 스택을 지웁니다. DynamoDB 테이블은 삭제 보호가 켜져 있어 백업 → 보호 해제 →
삭제의 순서를 거칩니다([`infra/README.md`](infra/README.md)).

```bash
infra/scripts/backup.sh
cd infra/envs/standalone
terraform apply -var allow_destroy=true   # 1단계: 삭제 보호 해제
terraform destroy                          # 2단계
```

## 문서

- 인프라 배포: [`infra/README.md`](infra/README.md)
- 백엔드 아키텍처: [`server/ARCHITECTURE.md`](server/ARCHITECTURE.md)
- 에이전트 런타임: [`agent-runtime/README.md`](agent-runtime/README.md)
- MCP Apps 데모 서버: [`mcp-apps-server/README.md`](mcp-apps-server/README.md)
- Lambda 툴 게이트웨이: [`infra/modules/mcp_gateway/README.md`](infra/modules/mcp_gateway/README.md)
- 내장 툴 게이트웨이: [`infra/builtin_tools_gateway/README.md`](infra/builtin_tools_gateway/README.md)

## License

This project is licensed under the MIT-0 License. See the [LICENSE](../../LICENSE) file.
