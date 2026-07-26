# Observability 가이드 — AnyCompany 이커머스 에이전트

이 문서는 `runtime/ecommerce_runtime.py`(Claude Agent SDK + AgentCore Runtime)의
관측성 구성을 정리합니다. **AgentCore Observability(CloudWatch GenAI Observability)를
기본 축**으로 하고, Claude Agent SDK 구조에서 자동 계측이 잡지 못하는 LLM 데이터
(토큰·비용·프롬프트)를 gen_ai 스팬·커스텀 메트릭·구조화 로그로 보완합니다.

> **AgentCore Observability는 어디서 보나?** AgentCore Observability는 기능 이름이고,
> 공식 확인 화면은 CloudWatch 콘솔의 GenAI Observability → Agent Core 탭입니다
> (AgentCore 서비스 콘솔의 Observability 메뉴도 같은 화면으로 연결).
> Transaction Search가 저장하는 스팬(`aws/spans` 로그 그룹) 기반으로 에이전트 →
> 세션 → 트레이스 타임라인 → 스팬 상세로 내려가며 탐색합니다.
> `agentcore deploy` 출력의 "GenAI Observability Dashboard" 링크가 이 화면입니다.

---

## 1. 아키텍처

```
┌─ AgentCore Runtime 컨테이너 ────────────────────────────────┐
│  opentelemetry-instrument (ADOT 자동 계측)                   │
│  └─ ecommerce_runtime.py  invoke()                          │
│       ├─ Claude Agent SDK ──▶ claude CLI (Node 서브프로세스) │
│       │     └─ 모델 호출·도구 실행은 이 안에서 일어남          │
│       └─ ResultMessage (토큰·비용·레이턴시·세션ID) 수신       │
│            └─ observability.py  observe_invocation()        │
│                 ├─▶ gen_ai 스팬 (claude_agent_sdk.invoke_agent│
│                 │    — 프롬프트·응답·토큰, 기존 트레이스에 연결)│
│                 ├─▶ CloudWatch 커스텀 메트릭 genai.*         │
│                 └─▶ GENAI_INVOCATION 구조화 로그 (입출력 전문)│
└─────────────────────────────────────────────────────────────┘
     │ (스팬: ADOT 자동 + gen_ai 수동)        │ (보완 메트릭)
     ▼                                       ▼
 CloudWatch GenAI Observability          CloudWatch 이상탐지 알람 4종
 (세션·트레이스·스팬 탐색기)              (setup_anomaly_alarms.py)
```

역할 분담:

| 축 | 무엇을 보여주나 | 어떻게 활성화되나 |
|----|----------------|------------------|
| **AgentCore Observability** (기본) | 세션 목록, 트레이스/스팬 타임라인, 런타임 로그, Transaction Search | `agentcore deploy` 시 자동 구성 (ADOT + X-Ray 전송 + 로그 전달) |
| **gen_ai 스팬** (보완) | 콘솔 트레이스 상세에서 프롬프트·응답 전문·토큰·비용 | `observability.py`가 호출마다 발행 (content는 옵트인) |
| **genai.* 보완 메트릭** | 호출별 토큰·비용·레이턴시·도구 호출·에러 추이 | `observability.py`가 ResultMessage 기반으로 기록 |
| **GENAI_INVOCATION 로그** | 입출력 전문 대량 조회 (Logs Insights) | `observability.py`가 기록 (옵트인) |
| **이상탐지 알람** | genai.* 메트릭의 급증·급감 감지 | `setup_anomaly_alarms.py` 1회 실행 |

**보완이 필요한 이유**: Strands 등 Python 네이티브 프레임워크는 ADOT가 LLM 호출
span(`gen_ai.*`)을 직접 잡지만, Claude Agent SDK는 모델 호출이 `claude` CLI
서브프로세스(Node) 안에서 일어나 Python 자동 계측에 잡히지 않습니다. 그래서 SDK의
`ResultMessage`(토큰·비용·`duration_ms`·세션ID)를 호출 단위로 스팬·메트릭·로그에
기록합니다.

## 2. 구성 요소

| 파일 | 위치 | 역할 |
|------|------|------|
| `observability.py` | `runtime/` (컨테이너에 동봉) | gen_ai 스팬 발행 + `genai.*` 메트릭 + `GENAI_INVOCATION` 로그 |
| `setup_anomaly_alarms.py` | `observability/` (로컬 실행) | 이상탐지 모델 + 알람 4종 생성/삭제 |
| ADOT (`aws-opentelemetry-distro`) | Dockerfile | 트레이스·로그를 GenAI Observability로 자동 전송 |
| `guide.md` | `observability/` | 이 문서 |

## 2-1. 입력/출력 프롬프트 확인 (콘솔 + 로그)

호출마다 `claude_agent_sdk.invoke_agent` gen_ai 스팬이 기존 트레이스에 발행됩니다.
AgentCore Observability에서 **Agent Core → 에이전트 → 세션 선택 → 트레이스 → 스팬 클릭**하면
`gen_ai.input.messages` / `gen_ai.output.messages`(요청·응답 전문),
`gen_ai.usage.input_tokens/output_tokens`, `cost_usd`, `gen_ai.tool.names`가 보입니다.

같은 내용이 `GENAI_INVOCATION` 구조화 로그로도 남아 Logs Insights로 대량 조회할 수
있습니다: `filter log_type = "GENAI_INVOCATION" | fields session_id, request_payload,
response_payload, cost_usd`

**옵트인**: 프롬프트 원문에는 PII가 포함될 수 있어 기본 비활성입니다. `deploy()`가
`GENAI_LOG_PAYLOADS=1`(로그·스팬 content 게이트)과
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`(ADOT의 content 속성
제거 방지)를 명시적으로 주입합니다. 끄려면 두 환경변수를 제거하고 재배포하세요.

## 3. 기록되는 메트릭

네임스페이스: `bedrock-agentcore` — AgentCore 관측성 데이터와 같은 네임스페이스이며,
기본 실행 역할의 `cloudwatch:PutMetricData` 권한이 여기로 제한되어 있어 IAM 수정
없이 동작합니다. 차원: `Agent=anycompany_ecommerce`.

| 메트릭 | 단위 | 의미 |
|--------|------|------|
| `genai.invocation.count` | Count | 호출 수 |
| `genai.invocation.latency` | ms | 호출 레이턴시 (`ResultMessage.duration_ms`) |
| `genai.token.input` | Count | 입력 토큰 (캐시 생성/읽기 포함) |
| `genai.token.output` | Count | 출력 토큰 |
| `genai.cost.usd` | None | 호출 비용 (`ResultMessage.total_cost_usd`) |
| `genai.tool.calls` | Count | 도구 호출 횟수 (`ToolUseBlock` 수집) |
| `genai.error.count` | Count | 에러 발생 시에만 기록 |

네임스페이스/에이전트명은 `GENAI_METRICS_NAMESPACE`, `GENAI_AGENT_NAME` 환경변수로
변경할 수 있습니다 (별도 네임스페이스 사용 시 실행 역할에 권한 추가 필요).

## 4. 활성화 방법

**전부 배포만 하면 됩니다** — GenAI Observability(트레이스·세션·로그)는
`agentcore deploy`가 자동 구성하고, 보완 메트릭은 컨테이너에 포함되어 있습니다.

```bash
cd runtime
python ecommerce_runtime.py deploy
```

**이상탐지 알람** (선택, 1회):

```bash
python observability/setup_anomaly_alarms.py                          # 생성/갱신
python observability/setup_anomaly_alarms.py --sns-topic-arn arn:...  # SNS 알림 연동
python observability/setup_anomaly_alarms.py --delete                 # 정리
```

레이턴시·입력 토큰은 양방향(급증·급감), 에러·비용은 상방만 감시합니다
(5분 주기, 3회 평가 중 2회 밴드 이탈 시 발화, `TreatMissingData=notBreaching`).

## 5. 테스트 / 확인

```bash
# 1) 트래픽 생성
python runtime/ecommerce_runtime.py invoke "3월 베스트셀러 TOP5 알려줘"

# 2) 세션·트레이스: GenAI Observability 대시보드 (첫 데이터는 최대 10분 지연)
#    https://console.aws.amazon.com/cloudwatch/home?region=us-east-1#gen-ai-observability/agent-core
#    → Agent Core → 에이전트 선택 → Sessions / Traces 탭

# 3) 메트릭: CloudWatch → Metrics → bedrock-agentcore → Agent 차원
aws cloudwatch get-metric-data --region us-east-1 \
  --start-time "$(date -u -v-1H +%Y-%m-%dT%H:%M:%S)" --end-time "$(date -u +%Y-%m-%dT%H:%M:%S)" \
  --metric-data-queries '[{"Id":"c","MetricStat":{"Metric":{"Namespace":"bedrock-agentcore",
    "MetricName":"genai.cost.usd","Dimensions":[{"Name":"Agent","Value":"anycompany_ecommerce"}]},
    "Period":300,"Stat":"Sum"}}]'

# 4) 런타임 로그
aws logs tail /aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT --since 1h

# 5) 알람: CloudWatch → Alarms → anycompany_ecommerce-genai.*-anomaly
```

**이상탐지 밴드는 등록 직후 "Insufficient data"가 정상**입니다. 수 시간~수 일의
트래픽으로 학습된 뒤 그래프에 회색 예상 밴드가 나타납니다. 테스트로 이상을
유발하려면 평소보다 훨씬 긴 프롬프트(토큰 급증)나 연속 다량 호출(비용 급증)을
보내보세요.

## 6. 비용: 세션별로 비용이 추가되는가?

**예. 세션마다 추가 비용이 발생합니다.** AgentCore Runtime은 세션(=microVM) 단위로
소비 기반 과금됩니다 ([공식 요금](https://aws.amazon.com/bedrock/agentcore/pricing/) 기준):

| 리소스 | 단가 | 과금 시점 |
|--------|------|-----------|
| CPU | $0.0895 / vCPU-시간 | **활성 처리 중에만** — LLM 응답·도구 호출을 기다리는 I/O 대기 중에는 과금 없음 |
| 메모리 | $0.00945 / GB-시간 | **세션이 살아있는 전체 시간** — 유휴(idle) 구간 포함, 초 단위 |

세션 수명 주기 ([공식 문서](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html)):
- 유휴 타임아웃 기본 **15분** (LifecycleConfiguration으로 60초~8시간 조정 가능)
- 최대 수명 **8시간** (호출로 리셋되지 않음)
- `StopRuntimeSession` API로 즉시 종료 가능

**이 프로젝트에의 함의**: `invoke_deployed()`는 호출마다 `uuid.uuid4().hex * 2`로
**새 세션**을 만듭니다. 즉 호출이 끝나도 microVM이 기본 15분간 유휴 상태로 살아있고,
그동안 **메모리 요금이 계속 쌓입니다**. 이 컨테이너는 Node + Claude CLI를 포함해
메모리 풋프린트가 작지 않으므로(피크 ~2GB 가정 시 세션당 유휴 비용 ≈ 2GB × 0.25h ×
$0.00945 ≈ **$0.005/세션**), 호출량이 많아지면 무시할 수 없습니다.

절감 방법:
1. **세션 재사용** — 같은 사용자/대화는 동일 `runtimeSessionId`로 호출 (유휴 VM을 새로 만들지 않음)
2. **유휴 타임아웃 단축** — 런타임의 `lifecycleConfiguration.idleRuntimeSessionTimeout`을 60~300초로 설정
3. **명시적 종료** — 일회성 배치 호출이라면 완료 후 `StopRuntimeSession` 호출

참고로 관측성 자체의 비용은 미미합니다: 커스텀 메트릭 7종 × 1차원(~$0.30/메트릭/월
수준 고정), 이상탐지 모델·알람 4종(알람당 ~$0.30/월 수준). GenAI Observability는
Transaction Search 기반으로 수집 span 양에 따라 CloudWatch 요금이 발생하지만 이
프로젝트 트래픽 수준에서는 무시할 만합니다.

## 7. 트러블슈팅

- **메트릭이 안 보임**: 런타임 로그에서 `put_metric_data 실패` 경고 확인.
  커스텀 네임스페이스를 쓴 경우 실행 역할에 해당 네임스페이스의
  `cloudwatch:PutMetricData` 권한이 있는지 확인하세요 (기본 역할은 `bedrock-agentcore`만 허용).
- **`get-metric-statistics`가 빈 결과**: `get-metric-data`로 조회하세요 (기간 정렬에 덜 민감).
- **GenAI Observability에 트레이스가 안 보임**: 첫 배포 후 최대 10분 지연이 정상.
  CloudWatch 콘솔에서 Transaction Search가 활성화돼 있는지 확인 (`agentcore deploy`가
  자동 구성하지만, 계정 최초 1회는 반영에 시간이 걸릴 수 있음).
- **알람이 계속 Insufficient data**: 이상탐지 모델 학습에 트래픽 이력이 필요합니다.
  주기적으로 호출을 보내며 수 시간 이상 기다리세요.
