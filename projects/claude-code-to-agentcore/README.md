# Claude Code to AgentCore

Claude Code에서 검증한 데이터 분석 에이전트를 코드 로직 변경을 최소화하면서 Amazon Bedrock AgentCore의 관리형 서비스로 이전하는 예제입니다.

가상의 온라인 쇼핑몰 AnyCompany의 매출 리포트 에이전트를 소재로, 로컬 Claude Code 구성(Skill, stdio MCP, 웹 검색)을 프로덕션 구성(AgentCore Runtime, Gateway, Web Search)으로 단계별로 전환합니다.

## 전환 매핑

| Claude Code (로컬) | AgentCore (프로덕션) | 디렉토리 |
|---|---|---|
| stdio MCP 서버 (`ecommerce_mcp.py`) | Gateway + Lambda 타깃 (원격 MCP, JWT 인증) | `gateway/` |
| `@mcp.tool()` 데코레이터와 docstring | Gateway 타깃의 inline `toolSchema` | `gateway/deploy_gateway.py` |
| 웹 검색 MCP 프록시 | AgentCore Web Search Gateway (SigV4) | `runtime/` |
| `agent.py`와 `.claude/` 설정 | Runtime (ARM64 컨테이너, 자동 확장) | `runtime/` |
| `.claude/skills/sales-report` | 컨테이너에 포함된 동일 스킬 | `runtime/ecommerce_runtime.py` |

## 디렉토리 구성

```
claude-code-to-agentcore/
├── local_agent/                 # 출발점: 로컬 Claude Code 에이전트
│   ├── agent.py                 # Claude Agent SDK 진입점
│   ├── .claude/                 # settings.json + skills/sales-report
│   ├── .mcp.json                # MCP 서버 등록 (Claude Code와 동일 포맷)
│   ├── mcp_server/              # 사내 DB 조회 stdio MCP 서버
│   └── data/generate_data.py    # mock 데이터 생성기 (seed 고정)
├── gateway/
│   ├── lambda_src/handler.py    # 로컬 MCP 도구 3개를 그대로 옮긴 Lambda
│   ├── deploy_gateway.py        # Gateway + Lambda 타깃 생성 (재실행 안전)
│   ├── verify_gateway.py        # JWT로 Gateway 직접 호출 검증
│   └── gateway.json.example     # 배포 결과 캐시 형식 (실제 파일은 커밋 금지)
├── runtime/
│   └── ecommerce_runtime.py     # Runtime 진입점 + configure/deploy/invoke
└── evaluation/                  # 배포 후 품질 게이트
    ├── test_cases.json          # 이커머스 평가 케이스 5종
    ├── run_eval.py              # 로컬 휴리스틱 1차 채점 (회귀 테스트)
    ├── agentcore_evaluation.py  # AgentCore Evaluations API (online/batch)
    └── setup_eval_role.sh       # 평가 실행 IAM 역할 생성
```

## 사전 준비

- AWS 계정과 관리자 수준 권한 (Bedrock, AgentCore, Lambda, IAM, Cognito, ECR, CodeBuild)
- AWS CLI v2, Python 3.11 이상, [uv](https://docs.astral.sh/uv/), Node.js 20 이상
- us-east-1 리전에서 Anthropic Claude Sonnet 4.6 모델 액세스 활성화
- AgentCore Web Search Gateway 엔드포인트 (`.mcp.json`과 `runtime/ecommerce_runtime.py`의 플레이스홀더를 본인 값으로 교체)

```bash
uv venv
uv pip install "claude-agent-sdk>=0.1.19" "mcp>=1.2.0" \
  "bedrock-agentcore>=1.1.4" "bedrock-agentcore-starter-toolkit" "boto3>=1.35"

export CLAUDE_CODE_USE_BEDROCK=1
export AWS_REGION=us-east-1
```

## 실행 순서

```bash
# 0) mock 데이터 생성 (최초 1회)
uv run python local_agent/data/generate_data.py

# 1) 로컬 에이전트 실행 (출발점 검증)
uv run python local_agent/agent.py "2026 상반기 매출 리포트를 작성해줘"

# 2) 이커머스 도구를 Gateway로 (Lambda + MCP Gateway 생성)
uv run python gateway/deploy_gateway.py
uv run python gateway/verify_gateway.py     # 도구 3개와 실제 값 확인

# 3) 에이전트를 Runtime으로 (CodeBuild ARM64 컨테이너)
uv run python runtime/ecommerce_runtime.py deploy

# 4) 프로덕션 호출 (Runtime이 Gateway/Lambda + Web Search 사용)
uv run python runtime/ecommerce_runtime.py invoke "2026년 상반기 매출 리포트를 작성해줘. \
web-search로 2026 이커머스 트렌드를 인사이트에 반영해줘."
```

배포가 끝나면 `evaluation/`의 품질 게이트로 회귀 테스트와 AgentCore Evaluations 채점을 수행할 수 있습니다. 자세한 내용은 [evaluation/README.md](evaluation/README.md)를 참고하세요.

## 보안 참고 사항

- `gateway/deploy_gateway.py`가 생성하는 `gateway.json`에는 JWT 발급용 Cognito 클라이언트 시크릿이 포함됩니다. 이 파일은 `.gitignore`로 제외되어 있으며 버전 관리에 커밋하지 않아야 합니다.
- 실습에서는 Gateway 접속 정보를 Runtime 환경 변수로 주입합니다. 프로덕션에서는 클라이언트 시크릿을 AWS Secrets Manager에 저장하고 런타임에서 조회하는 방식을 권장합니다.
- 데이터 조회 도구는 SQLite를 읽기 전용으로 열고 SELECT 쿼리만 허용합니다.
- 실습 편의를 위해 관리자 권한을 가정합니다. 프로덕션에서는 리소스 ARN을 한정한 최소 권한 정책을 구성하시기 바랍니다.

## 리소스 정리

```bash
# Runtime
cd runtime/.build/anycompany_ecommerce && agentcore destroy

# Gateway 타깃 + Gateway (gateway.json의 gateway_id 사용)
aws bedrock-agentcore-control delete-gateway-target \
  --gateway-identifier <GATEWAY_ID> --target-id <TARGET_ID> --region us-east-1
aws bedrock-agentcore-control delete-gateway \
  --gateway-identifier <GATEWAY_ID> --region us-east-1

# Lambda
aws lambda delete-function --function-name anycompany-ecommerce-tools --region us-east-1
```
