# 평가(Evaluation): 배포 후 품질 게이트

배포만으로 프로덕션 준비가 끝나는 것은 아닙니다. 에이전트의 응답이 충분히 정확하고 도움이 되는지 지속적으로 채점하는 품질 게이트가 필요합니다. 이 디렉토리는 두 단계의 평가를 제공합니다.

> 이 에셋은 [agentops-kit](../agentops-kit)에서 검증된 AgentCore Evaluations API 호출 패턴을 이커머스 에이전트에 맞춰 적용한 것입니다.

## 구성

| 파일 | 역할 |
|---|---|
| `test_cases.json` | 이커머스 도메인 평가 케이스 5종 (사실 조회, 분석, 리포트, 웹 검색) |
| `run_eval.py` | 케이스를 배포된 Runtime에 실행해 로컬 휴리스틱으로 1차 채점 (회귀 테스트용) |
| `agentcore_evaluation.py` | AgentCore Evaluations API 래퍼 (online/batch/on-demand) |
| `setup_eval_role.sh` | 평가 실행 IAM 역할 생성 |

## 2단계 평가 전략

### 1단계: 로컬 휴리스틱 (빠른 회귀 테스트)

배포 직후 기대 키워드 적중률, 수치 포함 여부, 지연시간을 빠르게 확인합니다.

```bash
python run_eval.py --report results.json
# > tc01_total_sales ... 키워드 2/2  숫자 O  12.1s
# 평균 키워드 적중률 100%, 수치 포함률 100%
```

### 2단계: AgentCore Evaluations (LLM 평가자 기반 정성 채점)

운영 트래픽 트레이스를 빌트인 또는 커스텀 평가자로 채점합니다. 사용 가능한 빌트인 평가자는 Correctness, Helpfulness, Faithfulness, ResponseRelevance, Conciseness, InstructionFollowing, GoalSuccessRate, ToolSelectionAccuracy, ToolParameterAccuracy, Coherence, Refusal, Harmfulness 등 16종입니다.

```bash
# (1) 평가 실행 역할 생성
bash setup_eval_role.sh
export EVAL_ROLE_ARN=<출력된 ARN>
export LOG_GROUP_NAME=/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT
export SERVICE_NAME=anycompany_ecommerce

# (2) 평가자 목록 확인
python agentcore_evaluation.py list

# (3) 운영 트래픽 상시 채점(online)
python agentcore_evaluation.py online \
  Builtin.Correctness,Builtin.ToolSelectionAccuracy,Builtin.Helpfulness

# (4) 지난 로그 일괄 채점(batch)
python agentcore_evaluation.py batch Builtin.GoalSuccessRate
```

## 이커머스 에이전트에 특히 유용한 평가자

- ToolSelectionAccuracy / ToolParameterAccuracy: `query_sales`와 `top_products`를 올바른 인자로 호출했는지 확인합니다. 잘못된 기간이나 카테고리로 조회하면 매출 수치가 틀어집니다.
- Correctness / Faithfulness: 리포트 수치가 실제 DB 값과 일치하는지 확인해 환각을 방지합니다.
- InstructionFollowing: sales-report 스킬의 4단 형식을 지켰는지 확인합니다.
- GoalSuccessRate: 매출 리포트 작성이라는 목표를 끝까지 달성했는지 확인합니다.
