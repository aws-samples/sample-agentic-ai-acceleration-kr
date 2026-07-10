# admin-chat-agent

LLM Gateway 운영자용 BI assistant. 자연어로 사용자/팀/예산/사용량 데이터를
질의하고 답변·시각화를 받는 5-agent (Orchestrator + SQL Specialist + Code
Specialist + SQL Validator + Viz Specialist) 시스템.

전체 사양: [`docs/admin-chat-agent-spec.md`](../docs/admin-chat-agent-spec.md)
작업 로그: [`_archive/DEVLOG-migration-phase1-6.md`](../_archive/DEVLOG-migration-phase1-6.md) §16

## 상태 (2026-05-30)

- **Phase 0~3 완료** — 디자인 시스템 / AgentCore IaC / Tool 구현 / 5-agent + UI.
- **dev 환경 배포·검증 완료** — image build → ECR push → AgentCore Runtime
  생성 → invoke + admin-api proxy E2E (세션→메시지→SSE→DB) 모두 통과.
- 운영(prod) 미배포. dev 런타임은 `enable_chat_agent=true` 로 생성된 자원.

## 배포 핵심 제약 (실배포로 확인)

| 항목 | 값 | 비고 |
|---|---|---|
| **아키텍처** | **arm64 전용** | AgentCore = Graviton microVM. amd64 push 시 `ValidationException` |
| **networkMode** | `PUBLIC` | 현 API 는 VPC subnet/SG 미지원. DB 는 Lambda 가 VPC 에서 접근 |
| **인증** | **SigV4 (IAM)** | admin-api 가 admin 권한 검증 후 IAM 으로 InvokeAgentRuntime. (JWT authorizer 도 가능하나 admin-ui 내부 토큰과 불일치 → SigV4 채택) |
| **ECR 태그** | immutable | 같은 태그 재push 불가 → 버전 올려 push |
| **entrypoint** | `bedrock_agentcore.runtime.BedrockAgentCoreApp` | starter_toolkit 아님 (SDK 버전별 경로 주의) |

## 빌드 + Push

terraform 으로 ECR repo 가 만들어진 뒤 (`enable_chat_agent=true` 적용):

```bash
ENV=dev    # 또는 prod
ECR_URL=$(terraform -chdir=../deployment/terraform/environments/llm-gateway-$ENV \
    output -raw chat_agent_ecr_url)
REGISTRY=$(echo "$ECR_URL" | cut -d/ -f1)

aws ecr get-login-password --region ap-northeast-2 | \
    finch login --username AWS --password-stdin "$REGISTRY"

# arm64 필수 (Apple Silicon 은 네이티브, 그 외는 에뮬레이션)
finch build --platform linux/arm64 -t admin-chat-agent:0.1.1-arm64 .
finch tag  admin-chat-agent:0.1.1-arm64 "$ECR_URL:0.1.1-arm64"
finch push "$ECR_URL:0.1.1-arm64"
```

## AgentCore Runtime 생성

런타임 이름은 영숫자+언더스코어만 허용(하이픈 불가) → `tr '-' '_'`.
입력은 JSON 파일로 (`--cli-input-json`):

```bash
ROLE_ARN=$(terraform -chdir=../deployment/terraform/environments/llm-gateway-$ENV \
    output -raw chat_agent_execution_role_arn)
NAME=$(terraform -chdir=... output -raw chat_agent_name | tr '-' '_')
STAGING=$(terraform -chdir=... output -raw chat_agent_staging_bucket)

cat > /tmp/create.json <<JSON
{
  "agentRuntimeName": "$NAME",
  "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": "$ECR_URL:0.1.1-arm64"}},
  "roleArn": "$ROLE_ARN",
  "networkConfiguration": {"networkMode": "PUBLIC"},
  "protocolConfiguration": {"serverProtocol": "HTTP"},
  "environmentVariables": {
    "MODEL_OPUS": "global.anthropic.claude-opus-4-7",
    "MODEL_SONNET": "global.anthropic.claude-sonnet-4-6",
    "MODEL_HAIKU": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    "CHAT_STAGING_BUCKET": "$STAGING"
  }
}
JSON

aws bedrock-agentcore-control create-agent-runtime \
    --cli-input-json file:///tmp/create.json --region ap-northeast-2
# → agentRuntimeArn / status: CREATING → READY
```

> 인증을 JWT(Cognito) 로 하려면 위 JSON 에 추가:
> `"authorizerConfiguration": {"customJWTAuthorizer": {"discoveryUrl": "<issuer>/.well-known/openid-configuration", "allowedClients": ["<client_id>"]}}`
> — 단 Cognito **access token** 만 통과 (ID token 은 `client_id` claim 부재).
> admin-api proxy 는 현재 SigV4 를 쓰므로 authorizer 없이 생성.

## 호출 검증 (SigV4)

```bash
RT_ARN=<위 출력의 agentRuntimeArn>
python3 - <<PY
import boto3, json
c = boto3.client("bedrock-agentcore", region_name="ap-northeast-2")
r = c.invoke_agent_runtime(
    agentRuntimeArn="$RT_ARN",
    runtimeSessionId="verify-" + "0"*40,   # >=33 chars
    payload=json.dumps({"content": "hello"}).encode(),
)
print(r["response"].read().decode())
PY
# → {"reply": "안녕하세요! ...", "agent": "admin-chat-agent", "phase": 3}
```

## admin-api 연동

런타임 생성 후 admin-api pod 의 env 에 `AGENTCORE_RUNTIME_ARN` 설정 →
`/admin/chat/*` endpoint 활성. 미설정 시 503 (운영 영향 0). proxy 는
SigV4 로 호출하므로 admin-api 의 IAM role 에 `bedrock-agentcore:InvokeAgentRuntime`
권한 필요.

## 정확도 하네스 (deterministic-tool-first)

LLM 환각/재계산 방지를 위해 답변·차트의 모든 숫자가 실행 결과에서만 나오도록
강제한다. 구현은 `src/agent/main.py`:

| 기법 | 설명 | 구현 |
|---|---|---|
| deterministic-tool-first | 숫자는 SQL/Code envelope 셀에서만, 산문 재계산 금지 | `prompts/orchestrator.md` |
| 구조화 envelope | sub-agent 가 JSON envelope 만 반환(마크다운/산문 금지) | 각 specialist prompt |
| forced structured output | Strands `structured_output_model`(Pydantic)로 envelope 필드 누락을 구조적 차단, 실패 시 텍스트 파싱 fallback | `envelopes.py` · `_structured_call` |
| envelope 파싱 견고화 | 산문·다중 JSON 섞여도 tool별 기대 키(sql/code/verdict)로 우선 추출 | `_parse_agent_json` |
| DAIL-SQL few-shot | 유사 질문→검증 SQL 예시를 in-context 주입(키워드 유사도, LOO 오염 가드) | `fewshot.py` · `fewshot_bank.json` |
| reconciliation gate | 최종 텍스트 숫자가 실행 결과서 유래했는지 검사 → WARN(fail-soft) | `_reconcile_numbers` |
| tool 투명성 | tool_call/tool_result 이벤트 발행 → admin-ui 가 SQL·코드 렌더 | `invoke()` |
| render_chart 발행 | render_chart tool spec 을 chart 이벤트로 stash→발행 | `_chart_specs` |

## 골든 테스트 & 정확도 측정 (`tests/`)

12 use-case (Tier A 8 SQL-only + Tier B 4 SQL+Code) 골든 테스트.

```bash
# static — 케이스 무결성 + 스키마 drift 가드 (비용 0)
python -m pytest                          # 60+ 단위 테스트
python -m tests.eval.run_golden --static  # 12 케이스 정합성

# live E2E — 배포 agent invoke 채점 (Bedrock 비용 발생)
GOLDEN_LIVE=1 AGENTCORE_RUNTIME_ARN=<arn> \
  python -m tests.eval.run_golden --live --json /tmp/golden.json
# Tier B(Code Interpreter)는 timeout 자동 600s, Tier A 180s.
# --runs N : 케이스당 N회 실행 → 다수결 집계로 LLM 변동성(flaky) 분리 (scoring.reduce_runs)
```

하네스 반복으로 측정된 live pass-rate: **17% → 33% → 50% → 67%**
(schema/datetime 정정 → Code Interpreter 권한+envelope+합성데이터 → render_chart fix).
이벤트 계약·합성 시드(`lambdas/seed_dev_data/`) 상세는 [`tests/README.md`](tests/README.md).
