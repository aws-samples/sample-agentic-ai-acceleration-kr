# Amazon Bedrock 위에 사내 LLM Gateway 구축하기: Claude Code·Codex를 위한 인증·비용·거버넌스

> *"개발자 전원에게 Claude Code를 열어주고 싶습니다. 그런데 누가 얼마를 쓰는지, 어떤 모델에 접근하는지, 예산을 넘기면 어떻게 막을지를 모른 채로 열어도 될지 확신이 서지 않습니다."*

이 고민은 2026년 현재 거의 모든 기술 조직이 마주하는 현실입니다. 생성형 AI 코딩 도구는 더 이상 실험이 아니라 일상 도구가 되었고, [Claude Code](https://www.anthropic.com/claude-code)는 환경변수 하나(`CLAUDE_CODE_USE_BEDROCK=1`)만으로 Amazon Bedrock 위에서 곧바로 동작합니다. 그리고 이제는 Claude만의 이야기가 아닙니다. 2026년 6월 [OpenAI의 GPT-5.5·GPT-5.4 모델과 Codex가 Amazon Bedrock에서 정식 사용 가능](https://aws.amazon.com/blogs/aws/get-started-with-openai-gpt-5-5-gpt-5-4-models-and-codex-on-amazon-bedrock/)해지면서, OpenAI의 Codex 같은 코딩 에이전트도 최신 프런티어 모델로 같은 Bedrock 백엔드 위에서 동작합니다. 어떤 에이전트를 쓰든 개인 개발자에게는 더없이 편리합니다. 하지만 수백 명 규모의 조직에 그대로 풀어놓는 순간 네 가지 통제 불능 영역이 생깁니다. 누가 쓰는지 알 수 없고(인증), 얼마를 쓰는지 사후에도 분해할 수 없으며(비용), 어떤 모델에 접근하는지 제어할 수 없고(거버넌스), 그 모든 활동을 추적·감사할 수단이 없습니다(관측성).

이 글은 바로 이 네 가지 공백을 메우기 위해 우리가 직접 구축한 **사내 LLM Gateway**의 설계를 데이터 평면과 컨트롤 평면, 그리고 각 서비스(API)의 역할까지 따라가 봅니다. 게이트웨이는 사내 OIDC 인증으로 Virtual Key를 자동 발급하고, 모든 LLM 요청을 Bedrock으로 프록시하면서 팀·사용자별 예산, Rate Limit, 모델 접근 제어, 그리고 토큰 단위의 정확한 사용량 추적을 수행합니다.

> **Disclaimer**: 본 글은 자사 PoC 구현을 정리한 것으로 특정 서드파티 제품을 권장하지 않으며, 인용된 외부 통계는 각 출처의 발표 시점을 기준으로 합니다. 코드 인용은 구조 설명을 위해 단순화되었고, 실제 비용·보안 설정은 각 조직 환경에 맞게 검토하시기 바랍니다.

---

## 1. 지금 거버넌스가 중요해진 이유

기술적 설계로 들어가기 전에, 이 문제가 왜 더 이상 미룰 수 없는 과제가 되었는지 먼저 짚겠습니다. 세 가지 흐름이 동시에 일어나고 있기 때문입니다.

**첫째, 사용은 이미 폭발했지만 통제는 따라가지 못하고 있습니다.** Microsoft와 LinkedIn이 2024년 발표한 [Work Trend Index](https://www.microsoft.com/en-us/worklab/work-trend-index/ai-at-work-is-here-now-comes-the-hard-part)에 따르면, 지식 근로자의 **75%가 이미 업무에 AI를 사용**하고 있고, 그중 **78%는 회사가 제공하지 않은 자신의 AI 도구를 몰래 들고 와서 쓰고 있습니다(BYOAI, Bring Your Own AI)**. 다시 말해 대부분의 AI 사용이 조직의 시야 바깥, 이른바 "Shadow AI" 영역에서 일어나고 있습니다. 통제되지 않은 도구로 사내 코드와 데이터가 빠져나가도 조직은 그 사실조차 알기 어렵습니다. 중앙 게이트웨이는 이 사용을 *양지로 끌어올려* 가시성과 통제권을 회복하는 첫걸음입니다.

**둘째, 비용 구조가 역설적으로 위험해졌습니다.** Deloitte의 [Tech Trends 2026](https://www.deloitte.com/us/en/insights/topics/technology-management/tech-trends.html) 보고서는 Stanford HAI의 AI Index를 인용해 **토큰 단가가 2년 만에 약 280배 하락**했다고 전합니다. 토큰이 싸졌으니 안심할 것 같지만, 같은 보고서는 바로 그 때문에 **일부 기업이 월 수천만 달러 규모의 LLM 청구서**를 받고 있다고 지적합니다. 단가 하락이 사용량 폭증을 부르고, 사용량 폭증이 총비용을 끌어올리는 구조입니다. 이때 비용을 *요청 단위로 누구에게 귀속*시킬 수단이 없으면, 조직은 거대한 청구서를 받아 들고도 그것이 어느 팀·어느 프로젝트에서 나온 것인지 분해할 수 없습니다.

**셋째, 실패율과 규제가 동시에 압박하고 있습니다.** Gartner는 (앞의 Deloitte 보고서에 인용된 바에 따르면) **2027년 말까지 에이전트형 AI 프로젝트의 40% 이상이 취소될 것**으로 전망합니다. 주된 이유는 비용 통제 실패와 불명확한 비즈니스 가치입니다. 한편 규제 시계는 멈추지 않습니다. [EU AI Act 시행 일정](https://artificialintelligenceact.eu/implementation-timeline/)에 따르면 금지 AI 관행은 2025년 2월 2일, 범용 AI 모델(GPAI) 의무는 2025년 8월 2일부터 이미 적용되고 있으며, **고위험 AI 시스템 의무는 2026년 8월 2일**부터 적용됩니다. 감사 추적, 모델 인벤토리, 사용 기록 같은 거버넌스 산출물은 이제 "있으면 좋은 것"이 아니라 규제 대응의 전제 조건입니다.

여기에 보안 표준의 관점도 더해집니다. [OWASP Top 10 for LLM Applications (2025)](https://genai.owasp.org/llm-top-10/)는 프롬프트 인젝션(LLM01), 민감정보 노출(LLM02), 그리고 특히 **무제한 소비(LLM10, Unbounded Consumption)**를 핵심 위험으로 명시합니다. 이 위험들은 팀마다 개별적으로 방어하기 어렵고, *중앙 집행 지점*에서 한 번에 막는 편이 훨씬 효율적입니다. Rate Limit, 비용 상한, 모델 화이트리스트가 바로 그런 중앙 통제 장치입니다.

정리하면, **사용은 이미 일어났고(75%), 그 대부분이 통제 밖이며(78% BYOAI), 비용은 역설적으로 커지고(월 수천만 달러), 실패율(40%)과 규제(2026년 8월)가 동시에 다가오고 있습니다.** 게이트웨이는 이 네 가지 압력에 대한 단일한 답입니다.

### 직접 연결과 게이트웨이의 차이

같은 이야기를 Claude Code의 두 가지 배포 방식으로 좁혀 보면 차이가 선명해집니다.

| 항목 | Claude Code 직접 연결 | Claude Code + LLM Gateway |
|---|---|---|
| **인증/보안** | 개인이 Bedrock IAM 자격증명을 직접 보유. 키 유출 시 추적·회수 곤란 | 사내 인증(OIDC/SSO/Cognito) 연동, 로그인 시 **Virtual Key 자동 발급**(유효 1시간) |
| **비용 가시성** | 개인·팀 사용량 분리 불가, 사후 분해 불가 | 매 요청마다 토큰·비용·모델을 **사용 기록으로 저장**(Bedrock 정식 토큰 카운터) |
| **거버넌스** | 모델 선택·한도·팀별 허용 범위를 조직이 통제 불가 | 개인/팀 예산, 팀별 허용 모델, 임계 초과 시 자동 다운그레이드·차단 |
| **감사** | 누가 무엇을 했는지 중앙 로그 없음 | `usage_logs` + CloudTrail + Bedrock invocation log로 **교차 검증** |

핵심은 비용의 비대칭성입니다. 한 명의 개발자가 무심코 대형 컨텍스트를 반복 호출하면 월 수천 달러가 나올 수 있는데, 직접 연결 구조에서는 그것이 *누구의* 비용인지조차 사후에 알 수 없습니다. 게이트웨이는 이 비용을 요청 단위로 귀속시키고, 예산 임계치에서 사후 청구서가 아니라 *선제적으로* 개입합니다.

---

## 2. 오픈소스 게이트웨이와의 차별점

"이미 나와 있는 오픈소스 LLM 프록시를 쓰면 되지 않나"라는 질문은 정당하고, 실제로 기능의 상당 부분은 겹칩니다(범용 오픈소스 게이트웨이는 좋은 출발점입니다). 우리가 직접 구축하면서 의식적으로 강화한 지점은 *정상 경로*가 아니라 **실패 경로**에서의 행동이었습니다.

| 항목 | 일반적 접근 | 본 게이트웨이 |
|---|---|---|
| **토큰 사용량 정확도** | LLM provider가 응답으로 돌려준 숫자만 신뢰(billing과 오차 가능) | Bedrock **정식 토큰 카운터** + CountTokens API로 AWS 청구 단위와 정합 |
| **스트리밍 중단 시 비용** | 응답 도중 끊기면 비용 기록 누락 | 끊긴 시점까지 받은 텍스트로 토큰을 역산해 **부분 비용 보존** |
| **비용 기록 워커 장애** | 단일 DB writer(leader) 구조. leader가 죽으면 정체 | 여러 워커가 Redis Stream **소비자 그룹**에서 분산 처리 |
| **워커 재시작 연쇄사고** | 사용량 기록기 down 시 게이트웨이 전체 down | liveness가 외부 의존성을 검사하지 않아 **cascade-down 방지** |
| **인증 경로 판정** | 프레임워크가 재구성한 URL 경로에 의존하면 Host 헤더 조작에 취약 | 모든 미들웨어가 **ASGI 원시 `scope["path"]`**만 사용 + 미등록 경로 기본 거부 |

이 다섯 가지는 뒤에서 코드와 함께 하나씩 다시 등장합니다. 지금은 "우리가 *다르게* 신경 쓴 지점"의 목록으로만 기억해 두면 됩니다.

마지막 행은 단순한 모범 사례가 아니라 실제 사고를 배경으로 둡니다. 2026년 공개된 [CVE-2026-49468](https://cybersecuritynews.com/litellm-vulnerability-host-header-injection/)은 널리 쓰이는 한 오픈소스 LLM 프록시의 **Host 헤더 인젝션을 통한 인증 우회**입니다. 인증 레이어가 Starlette가 Host 헤더로 *재구성한* `request.url.path`로 경로를 판정하는 반면 실제 라우팅은 다른 경로로 처리되는 불일치를 악용해, 인증 없이 관리 엔드포인트에 접근하고 LLM API 키를 탈취할 수 있는 결함입니다.

우리 게이트웨이는 이 취약점 클래스에 네 겹으로 해당하지 않습니다. **첫째, 범용 오픈소스 프록시를 그대로 쓰지 않고 자체 프록시를 구현했습니다.** 코드에 외부 프록시 라이브러리의 import나 의존성은 없으며, 차용한 것은 Rate Limit의 TTL 보존 *알고리즘 패턴* 정도입니다. 따라서 영향받는 버전 범위 자체에 들지 않습니다. **둘째, 경로 판정이 Host 헤더와 독립입니다.** 모든 인증·예산·Rate Limit 미들웨어가 프레임워크가 재구성한 URL이 아니라 ASGI 서버(uvicorn)가 요청 라인에서 파싱한 원시 경로(`scope["path"]`)만 읽습니다. 인증이 보는 경로와 라우팅이 보는 경로가 *같은 값*이므로 불일치가 발생할 수 없고, 등록되지 않은 경로는 기본적으로 401로 거부합니다.

```python
# gateway-proxy/src/app/middleware/auth.py — 경로 판정은 ASGI 원시 scope만 사용
path: str = scope.get("path", "")          # Host 헤더로 재구성된 URL이 아님
strategy = resolve_auth_strategy(path)
if strategy is None:                        # 미등록 경로 = 기본 거부(deny-by-default)
    await self._send_401(scope, send, "Unknown route")
    return
```

**셋째, 관리 API는 경로 매칭으로 인증을 걸지 않습니다.** 민감한 엔드포인트가 모인 admin-api는 *경로를 보는 미들웨어 자체가 없고*, 각 핸들러가 `Depends(require_admin)` / `require_admin_or_team_leader`로 Cognito JWT와 역할을 직접 바인딩합니다. "인증 레이어와 라우팅 레이어가 경로를 다르게 본다"는 공격면이 구조적으로 존재하지 않으며, 인증을 우회해 경로에 도달하더라도 관리 작업이 거부됩니다. **넷째, ALB 호스트 라우팅 뒤에 배치됩니다.** CVE 문서가 "영향받지 않는 환경"으로 명시한 *호스트 기반 라우팅 클라우드 로드밸런서* 조건을 이미 충족하며, 사용자 신원 검증에 쓰이는 STS 호출에는 별도의 host allowlist(`sts.{region}.amazonaws.com`)와 `Action=GetCallerIdentity` 강제까지 둡니다.

### Claude만이 아니다: GPT-5.5와 Codex도 Bedrock 위에서

이 글을 쓰는 2026년 6월 시점에서, Bedrock의 의미는 한 번 더 넓어졌습니다. 이제 Bedrock 위에서 도는 것은 Anthropic Claude만이 아닙니다. 2026년 6월 1일 AWS는 [OpenAI의 프런티어 모델 GPT-5.5·GPT-5.4와 Codex를 Amazon Bedrock에서 바로 쓸 수 있다](https://aws.amazon.com/blogs/aws/get-started-with-openai-gpt-5-5-gpt-5-4-models-and-codex-on-amazon-bedrock/)고 발표했습니다. 모델 ID는 각각 `openai.gpt-5.5`, `openai.gpt-5.4`이며(앞서 공개된 오픈 웨이트 `openai.gpt-oss-120b`·`gpt-oss-20b`도 함께), [공식 안내 페이지](https://aws.amazon.com/bedrock/openai/)는 *"Handle large codebases using Codex via Bedrock for enterprise-scale development"* 라고 명시합니다. Codex는 Bedrock API 키 또는 AWS SDK 자격증명 체인으로 인증하고, GPT-5.x는 chat/completions가 아니라 **Responses API**로 호출됩니다.

요컨대 **Claude Code뿐 아니라 OpenAI의 Codex 같은 코딩 에이전트도, 그것도 GPT-5.5 같은 최신 프런티어 모델로, 같은 Bedrock 백엔드 위에서 동작**합니다. 조직 입장에서 "어떤 코딩 에이전트를 표준으로 삼을 것인가"는 더 이상 게이트웨이 인프라를 가르는 문제가 아닙니다. 인증·비용·거버넌스를 한 곳에서 통제하면 그 위에서 Claude를 쓰든 Codex를 쓰든 상관없기 때문입니다.

바로 이 지점이 우리가 게이트웨이를 처음부터 **멀티 프로바이더**로 설계한 이유입니다. 게이트웨이는 두 개의 프로바이더 타입과 두 개의 API 포맷을 1급 시민으로 모델링합니다.

```python
# gateway-proxy/src/app/schemas/domain.py
class ProviderType(str, Enum):
    BEDROCK = "BEDROCK"           # Anthropic Claude (Messages API)
    OPENMODEL = "OPENMODEL"       # OpenAI 호환 (GPT-5.x / Codex / 사내 vLLM 등)

class ApiFormat(str, Enum):
    BEDROCK_NATIVE = "BEDROCK_NATIVE"        # /v1/messages (Anthropic)
    OPENAI_COMPATIBLE = "OPENAI_COMPATIBLE"  # /v1/chat/completions, /v1/completions
```

덕분에 게이트웨이는 Claude용 `/v1/messages`와 함께 **OpenAI 호환 엔드포인트**(`/v1/chat/completions`, `/v1/completions`, `/v1/models`)를 나란히 제공합니다. 그리고 결정적으로, **인증·예산·Rate Limit·자동 다운그레이드·비용 기록의 거버넌스 파이프라인이 두 경로에 동일하게 적용**됩니다. 다운그레이드 미들웨어는 OpenAI 경로를 인지해(`_is_openai_path`) 해당 포맷의 모델을 해석하고, 비용 집계 역시 OpenAI 경로에서는 `tiktoken`(cl100k_base) 기반 근사로 출력 토큰을 역산해 같은 `cost:stream`에 흘려보냅니다. 어떤 에이전트가 어떤 모델을 부르든 거버넌스는 한 곳에서 일관되게 집행됩니다.

---

## 3. 솔루션 아키텍처

전체 시스템은 AWS EKS Fargate(ap-northeast-2) 위에 올라가며, 두 갈래의 흐름으로 나뉩니다. 인증·VK 발급(녹색)과 LLM 호출·비용 기록(적색)입니다.

![LLM Gateway 전체 아키텍처](images/fig-architecture.svg)

흐름을 말로 풀면 이렇습니다. 사용자가 `gateway-cli`로 로그인하면 OIDC(PKCE)를 거쳐 사내 IdP(이 PoC에서는 Cognito)가 토큰을 발급하고, `admin-api`가 그 토큰을 검증해 Virtual Key를 내줍니다(녹색). 이후 코딩 에이전트(Claude Code 또는 Bedrock 위에서 도는 Codex 같은 OpenAI 호환 클라이언트)가 그 VK를 Bearer 토큰으로 실어 `gateway-proxy`에 요청을 보내고, 게이트웨이는 인증·예산·Rate Limit·다운그레이드를 차례로 통과시킨 뒤 Bedrock으로 스트리밍 호출합니다(적색). 위 다이어그램은 Claude Code 경로를 대표로 그렸지만, OpenAI 호환 경로도 동일한 파이프라인을 지납니다.

데이터 평면의 두 축은 **Aurora PostgreSQL**(인증·사용량·예산·모델)과 **ElastiCache(Valkey)**(VK 캐시·Rate Limit 카운터·`cost:stream`)입니다. 그리고 백그라운드 워커들이 비용 적재·예산 알림·VK 정리를 비동기로 처리합니다. 각 서비스의 정확한 책임은 다음 절에서 하나씩 다룹니다.

### 컴퓨트 모드: Fargate에서 EKS Auto Mode로

현재 배포는 **EKS Fargate 전용**입니다. Terraform `eks-fargate` 모듈이 EC2 노드 그룹 없이 네임스페이스별 Fargate Profile만 정의하므로, 노드 패치·스케일링을 신경 쓸 필요 없이 Pod 단위로만 운영합니다. 다만 우리가 표준 `terraform-aws-modules/eks/aws` 클러스터를 쓰고 애플리케이션을 Helm `deploymentMode`(`eks-fargate` / `onprem`)로 분리해 둔 덕분에, 컴퓨트 평면은 워크로드 코드와 독립적으로 교체할 수 있습니다. 즉 GPU·대용량 배치처럼 Fargate가 맞지 않는 워크로드가 생기거나 노드 비용을 더 최적화하려는 시점에는, 같은 클러스터·같은 매니페스트를 유지한 채 **EKS Auto Mode**로 이전하는 경로가 열려 있습니다. Auto Mode는 Karpenter 기반 노드 프로비저닝·패치·빈패킹을 AWS가 관리해 주므로, "Fargate의 무(無)노드 운영"과 "EC2의 유연성·비용 효율" 사이에서 운영 부담을 늘리지 않고 균형점을 옮길 수 있습니다.

---

## 4. 서비스 구성: 각 API의 역할

게이트웨이는 단일 프로세스가 아니라 책임이 명확히 분리된 여러 서비스의 묶음입니다. 진입점(데이터 평면), 관리(컨트롤 평면), 그리고 비동기 백그라운드의 세 갈래로 나뉩니다.

| 서비스 | 평면 | 핵심 책임 | 대표 엔드포인트 / 트리거 |
|---|---|---|---|
| **gateway-proxy** | 데이터 | VK 인증 → 예산 → RateLimit → 다운그레이드 → Bedrock 프록시 → 비용 이벤트 발행 | `POST /v1/messages`, `/v1/chat/completions`, `/model/*/invoke*` |
| **admin-api** | 컨트롤 | VK 발급, 사용자·팀·모델·예산·RateLimit CRUD, 분석·모니터링 | `/v1/auth/exchange`, `/cli/*`, `/admin/*` |
| **admin-ui** | 컨트롤 | Next.js 14 운영 콘솔(대시보드·모니터링·관리 화면) | (브라우저) |
| **cost-recorder-worker** | 백그라운드 | `cost:stream` 소비 → `usage_logs` 적재 + 예산 집계 + 일별 통계 | Redis Stream 소비자 그룹 + 매일 00:10 KST cron |
| **notification-worker** | 백그라운드 | `notifications:budget` 구독 → 예산 임계 알림 발송 | Redis Pub/Sub 구독 |
| **scheduler** | 백그라운드 | 만료 VK 정리, ROI/사용량 집계 | 주기 실행 |
| **migration** | 1회성 | Alembic DB 스키마 마이그레이션 | 배포 시 Job |

### 4.1 gateway-proxy: 사용자 요청의 단일 진입점

데이터 평면의 전부입니다. FastAPI 기반이지만 인증·예산·RateLimit·다운그레이드는 **Pure ASGI 미들웨어**로 구현해, 스트리밍(SSE) 응답 동안 불필요한 객체 변환 없이 동작하도록 했습니다. 노출하는 엔드포인트는 세 그룹입니다.

- **Anthropic Messages API**: `POST /v1/messages`(LLM 호출 본경로), `POST /v1/messages/count_tokens`(토큰 사전 계수). Claude Code가 그대로 붙는 면입니다.
- **OpenAI 호환 API**: `GET /v1/models`, `GET /v1/models/{id}`, `POST /v1/chat/completions`, `POST /v1/completions`. Codex 등 OpenAI 형식 클라이언트가 붙는 면입니다.
- **Bedrock 네이티브 패스스루**: `POST /model/{id}/invoke`, `/invoke-with-response-stream`, `/converse`, `/converse-stream`. AWS SDK가 직접 호출하는 형식을 그대로 받습니다.

여기에 사용자 본인용 조회 엔드포인트(`GET /v1/usage/me`, `GET /v1/usage/team/{id}`)와 헬스체크(`GET /health`)가 붙습니다. 어떤 포맷으로 들어오든 §5의 거버넌스 파이프라인을 동일하게 통과한다는 점이 핵심입니다.

### 4.2 admin-api: 컨트롤 평면 REST API

운영에 필요한 모든 관리 작업과 VK 발급을 담당합니다. 라우터는 책임별로 명확히 분리되어 있습니다.

- **인증·CLI**: `POST /v1/auth/exchange`(OIDC id_token → Virtual Key 교환, §5.1), `POST /cli/auth/virtual-key`, `GET /cli/downloads`·`/cli/download/{os}/{arch}`(gateway-cli 바이너리 배포).
- **API Key 관리**: `GET /admin/keys`(VK 목록), `/admin/keys/count`, `DELETE /admin/keys/{id}`(키 해지).
- **예산**: `PUT /admin/budgets/team/{id}`·`/user/{id}`(한도 설정), `GET /admin/budgets/summary`, `GET|PUT|DELETE /admin/budgets/{scope}/{id}/downgrade`(다운그레이드 정책 CRUD, §4.3에서 다룸).
- **모델**: `GET /admin/models`, `POST /admin/models`(등록), `PUT /admin/models/{alias}`, `PUT /admin/models/{alias}/pricing`(단가 버전), `PATCH /admin/models/{alias}/status`(활성/비활성).
- **Rate Limit**: `GET /admin/rate-limits/tree`, `PUT /admin/rate-limits/user/{id}`·`/team/{id}`·`/global/{model}`.
- **사용자·팀·조직**: `POST /admin/teams`·`/departments`, `PUT /admin/teams/{id}/leader`, `PUT /admin/teams/{id}/allowed-models`(팀별 허용 모델, §5.3), `POST /admin/users/sync-cognito`(IdP 그룹 동기화), `GET /admin/users/tree`.
- **분석·모니터링**: `/admin/analytics/*`(ROI·생산성), `GET /admin/monitoring/{overview,models,events,users}`(실시간 운영 현황), `/admin/dashboard/*`, `/admin/my/*`(개인 사용량).

모든 `/admin/*` 핸들러는 `Depends(require_admin)` 또는 `require_admin_or_team_leader`로 Cognito JWT와 역할을 강제합니다. 경로 문자열을 보는 인증 미들웨어가 따로 없고 의존성 주입(DI)으로 핸들러에 직접 바인딩하는 구조라, §2에서 설명한 경로 불일치 공격면이 존재하지 않습니다.

### 4.3 백그라운드 워커들

세 개의 워커가 데이터 평면에서 비동기로 분리되어 돕니다.

- **cost-recorder-worker**: `cost:stream`(Redis Stream)을 소비자 그룹으로 읽어 `usage_logs`에 멱등 적재하고, 예산 사용량(`budget_usages`)과 일별 통계를 집계합니다. APScheduler가 매일 00:10(KST) 일별 집계 cron을 돌리고, 시작 시 누락분 backfill도 수행합니다. 자세한 내구성 설계는 §4.6에서 다룹니다.
- **notification-worker**: `notifications:budget` 채널을 구독해, 예산 임계(80/90/100%)를 돌파한 이벤트를 받아 이메일로 알립니다. 발송기(EmailSender)는 mock과 사내 메일 API를 팩토리로 교체할 수 있습니다.
- **scheduler**: 유효기간이 지난 Virtual Key를 주기적으로 정리하고 사용량·ROI 집계 작업을 돌립니다.

이 분리 덕분에 데이터 평면(gateway-proxy)은 "받고, 검사하고, 프록시하고, 이벤트 하나 던지는" 일에만 집중하고, 무거운 집계·발송·정리는 모두 뒤로 빠집니다.

---

## 5. 데이터 플레인: gateway-proxy 요청 파이프라인

사용자의 LLM 요청이 들어오면, `gateway-proxy`는 미들웨어 체인을 LIFO 순서로 통과시킵니다. 각 단계는 이전 단계가 `request.state`에 주입한 컨텍스트를 읽어 동작하는, 일종의 컨텍스트 파이프라인입니다.

![gateway-proxy 요청 파이프라인](images/fig-pipeline.svg)

### 5.1 인증의 출발점: 사내 IdP와 OIDC

데이터 평면의 첫 관문은 "Virtual Key 검증"입니다. 그 VK가 발급되는 곳이 컨트롤 평면(admin-api)의 인증 흐름입니다. 핵심 원칙은 하나로 요약됩니다. **사용자는 사내 신원으로 로그인할 뿐, Bedrock 자격증명을 직접 만지지 않는다.**

사용자 경험은 의도적으로 단순합니다. 개발자는 `gateway-cli login` 한 번이면 됩니다. 내부적으로는 다음이 일어납니다.

```
[사용자]      gateway-cli login → 브라우저로 사내 IdP 로그인 (OIDC Authorization Code + PKCE)
[IdP]         인증 성공 → id_token(JWT, RS256 서명, 유효 ~1h) + refresh_token 발급
[gateway-cli] 받은 id_token 을 admin-api 의 POST /v1/auth/exchange 로 제출
[Admin API]   id_token 검증(JWKS 서명 + iss/exp) → groups claim 으로 팀 매핑
              → Virtual Key 발급(유효 1h) → 해시만 저장(원문은 1회만 반환)
[gateway-cli] VK 를 로컬에 캐시 → api-key-helper 가 매 요청에 Bearer VK 주입
[Claude Code / Codex] LLM 호출 → gateway-proxy 가 VK 검증 후 Bedrock 으로 프록시
```

여기서 두 가지를 구분하는 것이 중요합니다. **id_token은 "당신이 누구인가"를 증명하는 사내 신원 토큰**이고, **Virtual Key는 "이 게이트웨이를 통해 LLM을 부를 수 있다"는 게이트웨이 전용 자격증명**입니다. id_token은 1시간짜리 단명 토큰으로 로그인 직후 VK로 *교환*되고 버려집니다. 이후 모든 LLM 요청은 VK만 사용합니다. 사내 IdP 토큰이 데이터 평면을 매번 오갈 필요가 없고, VK는 게이트웨이가 언제든 무효화·회수할 수 있는 자체 자격증명이기 때문입니다.

**IdP는 특정 제품에 묶이지 않습니다.** admin-api의 OIDC 검증기(`core/oidc_verifier.py`)는 표준 OIDC를 따르는 범용 클라이언트로 설계되어, Cognito·Keycloak·IAM Identity Center·Okta·Azure AD 등 OIDC를 지원하는 어떤 IdP와도 동작합니다.

```python
# admin-api/src/app/core/oidc_verifier.py — IdP 무관 OIDC 검증 (요지)
# 1) issuer의 .well-known/openid-configuration → jwks_uri 자동 발견 (TTL 캐시)
# 2) 토큰 헤더의 kid로 정확한 공개키 선택 → 키 로테이션 안전
# 3) RS256/384/512만 허용 (alg=none, HS256 차단으로 서명 우회 방지)
# 4) jose가 서명 + iss + exp + nbf + iat (+ aud) 를 한 번에 검증
payload = jwt.decode(token, public_key, algorithms=["RS256"],
                     issuer=ISSUER_URL, options={"verify_aud": AUDIENCE is not None})
```

`audience` 검증을 *조건부*로 둔 점이 IdP 유연성의 작은 핵심입니다. Cognito의 access_token에는 표준 `aud` claim이 없고 `client_id`만 있어 비워두면 검증을 건너뛰지만, Okta·Azure AD처럼 `aud`가 명시되는 IdP에서는 값을 채워 엄격하게 검증합니다. 본 PoC는 gateway-cli가 **id_token**을 보내는 방식을 쓰며(그래서 access_token과의 binding 검증인 `at_hash`는 비활성), 신원은 issuer·서명·`sub` claim으로 충분히 보증됩니다. 검증을 통과하면 토큰의 그룹 claim(Cognito의 경우 `cognito:groups`)을 읽어 팀을 매핑합니다. 그룹은 `Claude_[부서]_[팀]` 명명 규칙을 따르고, 어느 그룹에도 매칭되지 않는 사용자는 `rejectUnmatchedGroups`로 차단할 수 있습니다.

### 5.2 VK 인증: Redis 캐시를 먼저, Aurora를 나중에

데이터 평면으로 돌아오면, 요청 헤더의 `Authorization: Bearer <vk>`에서 Virtual Key를 꺼내 SHA256으로 해시한 뒤, 먼저 Redis 캐시(`key:cache:vk:{hash}`, TTL 300초)를 조회합니다. 매 요청마다 DB를 때리면 데이터 평면이 버티지 못하므로 캐시가 주 경로입니다. 다만 캐시에는 함정이 하나 있습니다. 캐시 TTL 5분 안에 계정이 비활성화되면 그 5분 동안은 죽은 키가 살아 있는 것처럼 보일 수 있습니다. 그래서 캐시 히트 시에도 Aurora에서 `is_active`를 한 번 더 확인합니다.

```python
# gateway-proxy/src/app/services/auth_service.py — VK → AuthContext (요지)
key_hash = hashlib.sha256(token.encode()).hexdigest()
ctx = await redis.get(f"key:cache:vk:{key_hash}")          # ① 캐시(주 경로)
if ctx and not await user_is_active(ctx.user_id):          #    계정 비활성 재확인
    await redis.delete(f"key:cache:vk:{key_hash}")
    raise PermissionError("user_deactivated")
if not ctx:                                                # ② 캐시 미스 → Aurora
    user_id = await redis.get(f"key:vk:{key_hash}")
    ctx = await load_auth_context(user_id)                 #    users + team_allowed_models
    await redis.setex(f"key:cache:vk:{key_hash}", 300, ctx)
```

VK는 `AuthContext`(`user_id`, `team_id`, `roles`, 그리고 `allowed_models`)로 해석됩니다. 이 마지막 필드가 모델 거버넌스의 출발점입니다. `None`이면 전체 모델 허용, 리스트면 화이트리스트이며, 그 값은 §5.7의 "팀별 허용 모델" 설정에서 흘러 들어옵니다.

### 5.3 예산: 세 가지 정책과 TEAM 우선 원칙

인증을 통과하면 예산 검사입니다. 이 부분이 거버넌스의 심장이므로 조금 자세히 보겠습니다. 예산 정책은 세 가지이며, 모두 Redis Lua 스크립트로 *원자적으로* 평가됩니다.

```lua
-- gateway-proxy/src/app/redis_scripts/budget_check.lua (핵심 분기)
local limit   = tonumber(config.limit_usd) or 0
local used    = tonumber(redis.call('GET', usage_key) or '0')
local usage_pct = (limit > 0) and math.floor((used / limit) * 100) or 0

-- ① HARD_BLOCK: 한도 도달 즉시 차단
if policy == 'hard_block' and used >= limit then
    return deny(scope_label .. '_budget_exceeded')
end

-- ② SOFT_WARNING: limit×110%까지는 통과(경고 플래그), 그 이상은 차단
if policy == 'soft_warning' then
    local effective_limit = limit * (soft_limit_pct / 100)   -- 기본 110%
    if used >= effective_limit then return deny(scope_label .. '_soft_limit_exceeded') end
    if used >= limit then soft_warning = true end             -- 통과하되 경고
end

-- ③ THROTTLE: 임계치 [80,90,100]%에서 RPM 점진 축소 신호
if policy == 'throttle' then
    for _, t in ipairs(thresholds) do
        if usage_pct >= t then throttle_active = true; break end
    end
end
```

세 정책은 성격이 다릅니다. **HARD_BLOCK**은 한도에 닿는 순간 단호하게 429를 반환합니다. **SOFT_WARNING**은 한도(limit)와 유효 한도(limit × `soft_limit_pct`, 기본 110%) 사이의 구간을 *유예 구간*으로 두어, 짧은 초과 스파이크를 차단하지 않고 경고 플래그만 붙여 통과시킵니다. 한밤중 배치 작업이 잠깐 한도를 넘었다고 전체를 막는 것은 과하기 때문입니다. **THROTTLE**은 차단 대신 *감속*입니다. 80%·90%·100% 임계치를 넘을 때마다 해당 사용자의 RPM을 `throttle_rpm_pct`(기본 50%)로 점진 축소해, 예산이 소진되는 속도 자체를 늦춥니다.

여기서 중요한 운영 규칙이 하나 있습니다. **TEAM 예산이 USER 예산에 우선**하며, 더 나아가 *TEAM 예산이 설정되지 않은 경우에는 요청을 거부*합니다.

```python
# gateway-proxy/src/app/services/budget_service.py
# USER config 미설정 → pass-through (Q 정책: 개인은 팀 예산에 귀속)
if user_result.get("config_present") and not user_result["allowed"]:
    raise PermissionError(user_result.get("reason", "user_budget_exceeded"))

# TEAM config 미설정 → deny (C-1 정책: "설정 누락 = 무제한"을 원천 차단)
if not team_result.get("config_present"):
    raise PermissionError("team_budget_unset")
```

이것은 사소해 보이지만 거버넌스의 핵심 철학입니다. *"설정을 깜빡한 팀은 무제한으로 쓸 수 있다"*는 기본값은 비용 사고의 단골 원인입니다. 우리는 그 기본값을 뒤집어, 팀 예산이 명시되지 않으면 아예 LLM을 호출할 수 없게 했습니다. 안전한 쪽이 기본값(secure by default)이 되도록 한 것입니다.

### 5.4 자동 다운그레이드: 차단 대신 격하

예산 임계치를 넘었을 때 무조건 차단하는 것만이 답은 아닙니다. 때로는 "비싼 Opus 대신 저렴한 Haiku로라도 일을 계속하게" 하는 편이 낫습니다. 그래서 다운그레이드 미들웨어는 예산 임계치에 도달하면 *요청 본문의 `model` 필드 자체를 재작성*합니다.

```python
# gateway-proxy/src/app/services/downgrade_loader.py — 체인 적용
def apply_chain(alias, rules, current_pct, max_depth=5):
    visited = {alias}
    for _ in range(max_depth):                 # 무한 체인 방어
        rule = next((r for r in rules
                     if r.from_alias == alias and current_pct >= r.threshold_pct), None)
        if rule is None or rule.to_alias in visited:   # 순환 감지
            break
        visited.add(rule.to_alias); alias = rule.to_alias
    return alias
```

규칙은 `threshold_pct` 오름차순으로 평가되어 체인을 이룹니다. 예를 들어 한 팀이 예산의 90%를 넘으면 `claude-opus-4-8`이 `claude-sonnet-4-6`으로, 100%를 넘으면 다시 `claude-haiku-4-5`로 격하됩니다. 정책은 **TEAM scope에만** 적용되며 Redis(`budget:downgrade:team:{id}`, TTL 60초)를 먼저 보고 없으면 Aurora `downgrade_policies`로 폴백해 로드됩니다. 다운그레이드 대상이 Haiku 4.5일 때는 Bedrock의 ValidationException을 피하려고 본문의 `thinking` 필드를 제거하는 등, 모델별 호환성까지 본문 수준에서 세심하게 처리합니다.

운영자가 이 모든 것을 보는 화면이 예산 관리 페이지입니다. 예산표에서 팀·사용자별 한도와 사용률·정책을 한눈에 보고, 그 아래에서 다운그레이드 체인을 시각적으로 구성합니다.

![예산 관리: 사용자/팀 예산표 + 자동 다운그레이드 체인](images/fig-budget-dark.svg)

위 화면에서 `aws-team`은 THROTTLE 정책에 64.5% 사용 중이고, 소속 사용자들은 개별 예산 없이 "팀 예산 적용"을 받고 있습니다(USER 미설정 시 팀에 귀속). `Default Team`은 HARD_BLOCK입니다. 아래쪽 체인은 90%에서 Sonnet, 100%에서 Haiku로 내려가는 격하 경로와 각 단계의 단가를 함께 보여줍니다. 운영자가 "격하가 실제로 얼마를 아끼는지"를 즉시 가늠할 수 있도록 한 것입니다.

### 5.5 Rate Limit: 4차원 × 3스코프 선예약

다음 관문은 Rate Limit입니다. 단순히 분당 요청 수만 세는 것이 아니라, 네 개의 차원(RPM 요청/분, TPM 토큰/분, CPM 비용/분, CPH 비용/시간)을 세 개의 스코프(USER, TEAM, GLOBAL)로 교차 검사합니다. 핵심 기법은 **선예약(pre-reserve)** 입니다. 요청을 보내기 *전에* 예상 토큰·비용을 미리 예약해 두고, 응답이 끝난 뒤 실제값으로 정산(settle)합니다. 이렇게 하면 응답이 오기 전까지 한도가 비어 보이는 경쟁 상태(race)를 막을 수 있습니다.

```python
# gateway-proxy/src/app/services/rate_limit_scope.py
# 요청 전: 보수적으로 예약 (입력 추정 + 캐시 생성 + 최대 출력)
reserved = estimated_input + estimated_cache_creation + max_output
# 응답 후 cost_recorder.finalize()에서 실제값으로 정산:
adjustment = actual_tpm - reserved_tpm     # 음수면 환불, 양수면 추가 차감
```

RPM·TPM은 슬라이딩 윈도우(현재/이전 분 버킷의 가중합)로 계산되어 분 경계의 버스트를 부드럽게 흡수합니다. 그리고 여기서도 실패 경로가 설계되어 있습니다. **Redis가 다운되면** 게이트웨이는 멈추지 않고 "degraded 모드"로 진입해, USER RPM만 워커별 인메모리 fixed-window로 근사 제어합니다. 정밀도는 떨어지지만 서비스는 살아 있습니다. 실제로 부하 테스트에서 4,000 동시 스트리밍 중 Redis 순간 지연이 발생했을 때, 이 fallback이 발동해 단 6건만 429로 거절하고 나머지는 정상 처리했습니다.

### 5.6 Bedrock 호출과 정확한 비용 기록

이제 실제 모델 호출입니다. Bedrock은 `invoke_model_with_response_stream`으로 호출하며, 요청 본문은 Anthropic Messages API 포맷을 *그대로 패스스루*합니다. 번역(translation) 모드가 아니라 패스스루라는 점이 중요합니다. 본문 JSON을 변형하지 않으므로 Claude Code가 실어 보낸 프롬프트 캐싱 지시(`cache_control`)가 그대로 Bedrock에 전달되어, 캐시 적중과 그에 따른 비용 절감이 손상 없이 보존됩니다. 모델 ID는 리전에 맞춰 재작성되고(`us.` → `apac.`), `global.anthropic.*` 프로파일은 리전 무관 패스스루입니다.

응답 스트림이 끝나면 `cost_recorder.finalize()`가 단일 임계 경로에서 비용 계산, Redis 예산 차감, TPM/CPM/CPH 정산, `cost:stream` 발행을 순서대로 수행합니다. 비용 계산은 입력·출력·캐시 쓰기(5분/1시간 TTL 구분)·캐시 읽기를 토큰 단위로 분리해 Bedrock의 정식 카운터에 정합시킵니다.

그리고 여기에 우리가 가장 공들인 실패 경로가 있습니다. **클라이언트가 응답 도중 연결을 끊으면** `message_delta.usage` 이벤트가 도착하지 않아 출력 토큰이 0으로 집계됩니다. 비용 기록이 통째로 누락되는 것입니다. 우리는 이를 다음과 같이 복구합니다.

```python
# gateway-proxy/src/app/services/streaming.py — 스트리밍 중단 복구 (KI-08)
# 스트림 도중 누적해 둔 텍스트로, 끊긴 뒤 토큰을 역산한다.
if usage.output_tokens == 0 and accumulated_text:
    estimated = await tokenizer_hook("".join(accumulated_text))   # Bedrock CountTokens API
    usage = TokenUsage(output_tokens=estimated, estimated=True)   # estimated 플래그로 표시
```

받은 텍스트를 모아 **Bedrock의 CountTokens API**(추론 비용이 발생하지 않는 토큰 계수 전용 API)로 출력 토큰을 역산하고, `estimated_usage=true`로 표시해 부분 비용을 보존합니다. 이 한 가지 처리 덕분에 부하 테스트의 모든 시나리오에서 *레코드 유실 0건*을 달성했고, Bedrock invocation log와 DB usage log의 토큰 수가 완전히 일치했습니다.

### 5.7 비용 기록의 비동기 오프로드: 연쇄 장애를 끊다

마지막으로, 게이트웨이는 비용을 Aurora에 *직접 쓰지 않습니다*. 대신 `cost:stream`이라는 Redis Stream에 이벤트를 발행만 하고, 별도의 `cost-recorder-worker`가 이를 소비해 DB에 적재합니다. 이 분리에는 두 가지 의도가 있습니다.

```python
# cost-recorder-worker — 분산 소비자 그룹
XGROUP CREATE cost:stream cost-recorder-group 0 MKSTREAM
# 여러 워커가 같은 그룹으로 XREADGROUP → 배치 누적(max 100건 / 5초) →
#   usage_logs INSERT (ON CONFLICT request_id DO NOTHING, 멱등) +
#   budget_usages UPSERT + 일별 카운터 + 임계 알림 PUBLISH → XACK
```

첫째, **단일 리더가 없습니다.** 워커는 소비자 그룹으로 수평 확장되고, 한 워커가 죽어도 ack되지 않은 메시지는 다른 워커가 재처리합니다. `request_id`를 멱등 키로 쓰는 `ON CONFLICT DO NOTHING` 덕분에 재처리가 중복 기록을 만들지 않습니다. 둘째, **워커의 liveness 프로브가 Redis나 DB를 검사하지 않습니다.** 외부 의존성이 잠깐 흔들렸다고 워커가 "unhealthy"로 판정되어 재시작되면, 그 재시작이 또 다른 부하를 만들어 연쇄 장애(cascade-down)로 번질 수 있기 때문입니다. *신뢰성은 분산 시스템의 연결 지점에서 만들어진다*는 원칙에 따라, 비용 기록기가 죽어도 게이트웨이는 계속 응답하고, 비용 이벤트는 Stream에 안전하게 쌓여 있다가 워커가 복구되면 그대로 처리됩니다.

이 발행된 비용 이벤트가 곧바로 다음 통제의 근거가 됩니다. 예산 임계를 넘긴 이벤트는 `notifications:budget`으로 흘러 notification-worker가 알림을 보내고, 일별 집계는 운영 대시보드의 모든 수치로 환원됩니다.

---

## 6. 컨트롤 평면: 모델 거버넌스와 운영 콘솔

운영자는 Next.js 14 기반 Admin UI에서 게이트웨이를 관리합니다. 대시보드는 이번 달 사용량·예산 소진율·사용자당 평균 비용·총 토큰을 한눈에 보여주고, 모델별 비용 점유율을 도넛으로, 비용 추이를 면적 그래프로 시각화합니다. 아래 모든 수치는 §5.7의 워커가 적재한 `usage_logs`와 일별 집계에서 나옵니다. 즉 화면의 모든 숫자가 실제 토큰 기록에 근거합니다.

![대시보드: KPI · 비용 추이 · 모델 점유율 · 팀/사용자 랭킹 (다크 모드)](images/01-dashboard-dark.png)

### 모델 관리: Alias와 Bedrock Model ID, 그리고 Pricing

모델 거버넌스의 출발점은 매핑 테이블입니다. Claude Code가 사용하는 모델 이름(alias)을 Bedrock 모델 ID와 단가(입력/출력/캐시 5분·1시간·읽기)로 연결합니다. admin-api의 `POST /admin/models`로 등록하고 `PUT /admin/models/{alias}/pricing`으로 단가를 버전 관리합니다.

![모델 관리: alias · Bedrock Model ID · 단가 매핑 (다크 모드)](images/02-models-mapping.png)

단가는 `effective_from`/`effective_until`로 버전 관리되므로, 비용 계산이 항상 *요청 시점*의 정확한 가격을 반영합니다. 가격이 바뀌어도 과거 기록은 과거 단가로 남습니다. 그리고 팀별로 접근 가능한 모델을 체크박스로 제한할 수 있는데(`PUT /admin/teams/{id}/allowed-models`), 이 화이트리스트가 바로 §5.2의 VK `allowed_models`로 전파되어 데이터 평면에서 강제됩니다. 화면의 설정과 런타임의 집행이 한 줄로 이어지는 구조입니다.

---

## 7. 검증: 부하 테스트

설계가 의도대로 동작하는지는 측정으로만 말할 수 있습니다. 비용 정확도는 *실제 Bedrock 모델을 호출하며* Bedrock Invocation Log와 DB usage_log의 토큰 수를 대조해 확인했습니다.

| 단계 | 동시 SSE 세션 | Bedrock Log (입력 / 출력) | DB usage Log | 차이 |
|---|---|---|---|---|
| v1 | 1,000 | 13,234,680 / 1,293,485 | 동일 | **0** |
| v2 | 4,000 | 42,904,724 / 4,191,674 | 동일 | **0** |
| v3 | 500 (5k 입력 + 5분 캐시) | 12,322,494 / 48,481,542 | 동일 | **0** |

세 시나리오 모두에서 토큰 수가 완전히 일치했습니다. §5.6의 중단 복구와 §5.7의 멱등 적재가 함께 만들어낸 결과입니다. 동시성 측면에서는 Mock Bedrock으로 4,000 SSE 동시 스트리밍을 처리해 **536,936건 중 200 OK 99.9983%**(5xx 0건)를 기록했고, HPA가 gateway-proxy를 3에서 30 replica로 스케일아웃했습니다. 로그인/키 발급 처리량 테스트에서는 5분 내 1만 명 동시 로그인 시도에서 Pod 80대 사전 warming과 jitter 120초 조건으로 8,065명까지 안정 발급을 확인했습니다.

---

## 8. 정리

사내 LLM Gateway는 단순한 프록시가 아니라 **거버넌스 플랫폼**이었습니다. 처음 던진 네 가지 질문으로 되돌아가 답을 정리하면 이렇습니다.

- **누가 쓰는가(인증)**: OIDC/Cognito 로그인 한 번으로 Virtual Key가 자동 발급되고, 키 유출·추적 문제가 사라집니다.
- **얼마를 쓰는가(비용)**: Bedrock 정식 토큰 카운터에 기반한 정확한 요청 단위 귀속, HARD_BLOCK/SOFT_WARNING/THROTTLE 정책, 임계 초과 시 자동 다운그레이드로 비용을 *선제적으로* 통제합니다.
- **무엇에 접근하는가(거버넌스)**: 팀별 모델 화이트리스트가 UI 설정에서 런타임 집행까지 한 줄로 이어집니다.
- **어떻게 추적하는가(관측성)**: Redis Stream 분산 적재, `usage_logs`와 Bedrock invocation log 교차 검증으로 모든 활동이 기록됩니다.

LLM을 조직에 도입할 때 진짜 어려운 부분은 모델 호출이 아니라 그 *주변*입니다. 인증, 비용, 거버넌스, 그리고 신뢰. 게이트웨이는 바로 그 주변을 설계하는 일이었습니다. 그리고 그 위에서는 Claude Code든 Codex든, Claude든 GPT-5.5든, 같은 통제 아래 안전하게 공존할 수 있습니다.

### 자가 점검 체크리스트

이 패턴을 자사에 적용해보려는 분들을 위한 질문입니다.

- [ ] LLM 사용 비용을 *요청 단위*로 사용자·팀에 귀속할 수 있는가?
- [ ] 예산 임계치에서 사후 청구서가 아니라 *선제적으로* 개입(차단·다운그레이드)하는가?
- [ ] 비용 기록 컴포넌트가 죽었을 때 게이트웨이 전체가 함께 죽지 않는가?
- [ ] 인증이 프레임워크 재구성 경로가 아니라 신뢰할 수 있는 원시 경로에 기반하는가?
- [ ] 사내 IdP를 특정 제품에 종속되지 않게 표준 OIDC로 추상화했는가?

---

## 참고 자료

**외부 통계·표준 (본문 인용)**
- Microsoft & LinkedIn, *Work Trend Index 2024*. 지식 근로자 75% AI 사용, 78% BYOAI: https://www.microsoft.com/en-us/worklab/work-trend-index/ai-at-work-is-here-now-comes-the-hard-part
- Deloitte, *Tech Trends 2026*. 토큰 단가 280배 하락, 월 수천만 달러 청구, Gartner의 40% 취소 전망 인용: https://www.deloitte.com/us/en/insights/topics/technology-management/tech-trends.html
- *EU AI Act Implementation Timeline*. 고위험 AI 의무 2026년 8월 2일 적용: https://artificialintelligenceact.eu/implementation-timeline/
- *OWASP Top 10 for LLM Applications (2025)*. 프롬프트 인젝션·무제한 소비 등: https://genai.owasp.org/llm-top-10/
- *CVE-2026-49468* (오픈소스 LLM 프록시의 Host Header Injection 인증 우회): https://cybersecuritynews.com/litellm-vulnerability-host-header-injection/

**Bedrock 위의 멀티 프로바이더 (본문 인용)**
- AWS News Blog (2026-06-01), *Get started with OpenAI GPT-5.5, GPT-5.4 models, and Codex on Amazon Bedrock*: https://aws.amazon.com/blogs/aws/get-started-with-openai-gpt-5-5-gpt-5-4-models-and-codex-on-amazon-bedrock/
- *OpenAI Models on Amazon Bedrock* (공식 안내): https://aws.amazon.com/bedrock/openai/

**AWS 기술 블로그**
- [Amazon Bedrock과 Claude Code로 사내 LLM 게이트웨이 구축하기](https://aws.amazon.com/ko/blogs/tech/bedrock-claude-code-llm-gw/)

**AWS 서비스 / SDK**
- [Amazon Bedrock](https://docs.aws.amazon.com/bedrock/) · [Amazon EKS](https://docs.aws.amazon.com/eks/) · [Amazon Aurora](https://docs.aws.amazon.com/aurora/) · [Amazon ElastiCache](https://docs.aws.amazon.com/elasticache/)

---

*본 게시물의 코드 인용은 사내 `llm-gateway` 딜리버러블에서 발췌했으며 구조 설명을 위해 단순화되었습니다. 일부 UI 도식은 다크 모드 기준으로 재구성한 것입니다.*
