# admin-chat-agent — Golden Test Framework

`docs/admin-chat-agent-spec.md` §8.5 의 12 use-case 정확도 평가. Phase 19 의
harness 기법(deterministic-tool-first / reconciliation gate / tool 투명성)이
실제로 정확도를 올렸는지 **숫자로 정량화**하기 위한 테스트 자산.

## 구조

```
tests/
├── conftest.py                  # main.py 순수 함수를 AST 추출해 격리 실행하는 fixture
├── golden/
│   ├── tier_a/use_case_01..08.yaml   # SQL only (8개)
│   └── tier_b/use_case_09..12.yaml   # SQL + Code Specialist (4개)
├── eval/
│   ├── scoring.py               # 이벤트 스트림 추출 + 채점 (순수, 라이브 호출 없음)
│   ├── agent_client.py          # 배포 runtime 호출 (boto3, GOLDEN_LIVE 토글)
│   └── run_golden.py            # CLI 러너 (--static / --live)
└── unit/
    ├── test_reconcile_numbers.py  # #1 reconciliation 시간표현 필터 회귀 고정
    ├── test_scoring.py            # 채점 로직 단위 테스트
    └── test_golden_static.py      # 12 케이스 자산 무결성 (schema drift 가드 포함)
```

## 두 가지 실행 모드

### 1. Static (비용 0, CI 기본) — 라이브 호출 없음

케이스 자산의 무결성과 순수 로직을 검증. PR 마다 무료로 돌린다.

```bash
cd admin-chat-agent
python -m pytest                       # 39 tests, ~0.1s
python -m tests.eval.run_golden --static   # 12 케이스 정합성 리포트
```

검증 항목: 정규식 유효성, tier/필드 일관성, 실제 tool 이름 사용,
`required_tables` 가 ground-truth 스키마(`usage`/`budget`/`model`/`auth`)만
쓰는지 — `config/golden_examples.yaml` 의 옛 `public.*` 네임스페이스 drift 가
새지 않도록 가드. (memory/chat-agent-schema-drift)

### 2. Live E2E (Bedrock 비용 발생) — 배포된 dev runtime 호출

실제 정확도를 측정. agent 를 12번 invoke 해 SQL/code/verdict/chart/agent-path 채점.

```bash
GOLDEN_LIVE=1 \
AGENTCORE_RUNTIME_ARN=arn:aws:bedrock-agentcore:ap-northeast-2:123456789012:runtime/llm_gateway_dev_admin_chat_agent-a8AdBh8WM8 \
python -m tests.eval.run_golden --live --json /tmp/golden-baseline.json

# 특정 케이스만
GOLDEN_LIVE=1 AGENTCORE_RUNTIME_ARN=... python -m tests.eval.run_golden --live --case 09
```

비용 가드: 12 case × (Opus orchestrator + Sonnet specialists) ≈ case 당 ~$0.2.
`--min-pass-rate 0.9` (live 기본) 미달 시 exit 1.

## 채점 방식 (이벤트 계약)

agents-as-tools 구조라 SQL/code 는 orchestrator stream 에 직접 안 보이고
sub-agent 의 구조화 envelope 에 들어 있다. 추출 경로
(memory/chat-agent-event-contract):

| 측정 | 추출 경로 |
|---|---|
| 생성 SQL | `tool_result`(tool==`ask_sql_specialist`)`.result.sql` |
| 실행 Python | `tool_result`(tool==`ask_code_specialist`)`.result.code` |
| validator verdict | `tool_result`(tool==`ask_validator`)`.result.verdict` |
| chart kind | `chart` 이벤트 `.spec.kind` |
| agent path | `tool_call` 이벤트 tool 이름 순서 |

채점은 관대(fail-soft 친화): PASS 기대에 WARN 관측은 통과(WARN 도 유효 답변),
차트 미발행이어도 `table`/`kpi` 가 허용이면 통과.

## Baseline 운영 (spec §8.5.4)

1. **Bootstrap (현재)**: 12 case 작성 완료. `config/golden_examples.yaml` 의
   정답 SQL 은 few-shot 용이며 **옛 스키마라 신뢰 금지** — 골든 케이스의
   `required_tables` 는 `db/init/02_create_tables.sql` ground truth 기준.
2. **Baseline 측정**: 배포 후 `--live --json` 으로 baseline pass-rate 기록.
3. **회귀 방지**: prompts/ 또는 agent/ 변경 시 live 재측정, baseline 대비
   하락 시 검토.

## 다음 작업

- live baseline 최초 측정 (배포 dev runtime 대상) → harness 효과 정량화.
- Tier B 의 Code Specialist 실제 sandbox 실행 확인 (DEVLOG §28.6 item 5).
- CI 통합: PR static 게이트는 즉시 가능. live 게이트는 비용/자격증명 정책 결정 후.
