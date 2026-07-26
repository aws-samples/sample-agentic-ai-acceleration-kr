# evaluation — Execution Accuracy(EX) code-based evaluator

agentic Text-to-SQL 솔루션의 **Track A 평가** 패키지입니다. Amazon Bedrock AgentCore
Evaluations 의 custom **code-based evaluator** Lambda(`agentic-t2sql-ex-evaluator`)와
gold 질의 세트(`goldset-v1.jsonl`)를 제공합니다.

> CLAUDE.md 의 "Tool layer 에 Lambda 금지" 제약의 **명시적 예외**입니다 —
> code-based evaluator 는 Evaluations 서비스 규격상 Lambda 여야 합니다
> (`docs/m2-m3-interface-contract.md` §9.1).

> AWS 배포(Lambda·Evaluator·OnlineEvaluationConfig 생성)는 CDK(`infra/lib/evaluation-stack.ts`)
> 담당입니다. 이 패키지는 코드와 로컬 단위 테스트까지 제공합니다.

## 구성

```
evaluation/
├── src/evaluation/
│   ├── handler.py                    # Lambda handler (code-based evaluator 계약)
│   ├── spans.py                      # 스팬 → (질문, SQL, status) 방어적 추출
│   ├── goldset.py                    # goldset 로딩 + 질문 정규화·매칭
│   ├── comparison.py                 # 결과셋 정규화 비교 + 경량 SQL 방어
│   ├── dataapi.py                    # RDS Data API 러너(read-only agent_ro)
│   └── goldset/goldset-v1.jsonl      # gold 질의 세트 (Lambda asset 동봉)
└── tests/                            # fake 주입 단위 테스트(AWS 호출 없음)
```

Lambda handler 진입점: **`evaluation.handler.handler`** (런타임 Python 3.13,
의존성은 표준 lib + boto3 만 — 의존성 zip 불필요).

## Execution Accuracy(EX) 판정 로직

1. `evaluationInput.sessionSpans` 에서 orchestrator 가 남긴 `t2sql_query_record`
   JSON(§9.5)을 찾아 `(question, sql, status, version)` 을 복원합니다.
   마커가 없으면 `gen_ai.*` / `db.statement` 계열 속성으로 폴백합니다
   (스팬 스키마가 다양하므로 body·attributes·logs 를 모두 방어적으로 훑습니다).
2. `goldset-v1.jsonl` 에서 질문을 매칭합니다 — 정규화(공백·대소문자·구두점 제거) 후
   완전일치, 실패 시 부분 매칭. 매칭이 없으면 **SKIP**.
3. gold SQL 과 생성 SQL 을 **RDS Data API(agent_ro 시크릿)** 로 각각 실행하고
   결과셋을 정규화 비교합니다 — 컬럼명 무시, 행은 값 튜플 multiset(순서 무시),
   숫자는 float 소수 6자리 반올림. 일치 → **PASS/1.0**, 불일치 → **FAIL/0.0**
   (explanation 에 차이 요약).
4. 생성 SQL 은 실행 전 경량 방어(단일 statement + `SELECT`/`WITH` 시작 + 쓰기 키워드 차단).
   sqlglot 의존 없이 표준 lib 만 사용하며, **최후 방어선은 read-only DB grant** 입니다.
5. `explanation` 에 `EVALUATOR_VERSION`(기본 `1.0.0`)·goldset id·orchestrator
   version vector(bundle/agent)를 병기해 버전 귀인이 가능합니다.

판정 요약

| 상황 | label | value |
|---|---|---|
| 결과셋 일치 | `PASS` | 1.0 |
| 결과셋 불일치 / 생성 SQL 실행 실패 / 안전 검사 거부 / SQL 없음 | `FAIL` | 0.0 |
| goldset 미매칭 / 질문 추출 실패 / clarification 종료 / 비-aurora goldset | `SKIP` | (생략) |
| `evaluationInput` 없음 / gold SQL 실행 실패 / 내부 오류 | `errorCode` 응답 | — |

## 환경변수

| env | 설명 |
|---|---|
| `AURORA_CLUSTER_ARN` | Aurora 클러스터 ARN |
| `AURORA_SECRET_ARN` | **agent_ro**(read-only) 시크릿 ARN |
| `DB_NAME` | 데이터베이스명 (샘플: `ecommerce`) |
| `EVALUATOR_VERSION` | evaluator 버전 문자열 (기본 `1.0.0`) |
| `GOLDSET_PATH` | (선택) goldset JSONL 경로 오버라이드 |
| `AWS_REGION` | 기본 `us-west-2` |

## goldset

`src/evaluation/goldset/goldset-v1.jsonl` — 각 라인은
`{"id","question","sql","datasource"}` 입니다. 파일명이 곧 버전(`goldset-v1`)이며,
질의는 `sample-data` 의 e-커머스 스키마(Aurora, DB `ecommerce`)에서 실제 실행 가능합니다.
현재 8문항(모두 `aurora`)이며 E2E 기본 질문 "지역별 매출 상위 5개 지역을 알려줘"를 포함합니다.

## 로컬 테스트

```bash
cd evaluation
uv sync
uv run pytest
```

AWS 호출은 fake 주입으로 대체하므로 자격증명 없이 실행됩니다.
