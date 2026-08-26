# Admin Chat Agent — 사양서

> 운영자 (admin-ui 사용자) 가 자연어로 LLM Gateway 데이터를 질의하고
> 답변·시각화를 받는 AI assistant. 자연어 → 검증된 SQL → 결과 →
> Claude 의 자연어 요약 + 차트 까지의 agent loop.

---

## 0. 한 눈에 보기

| 항목 | 값 |
|---|---|
| 코드 이름 | `admin-chat-agent` |
| 새 컴포넌트 위치 | AgentCore Runtime (별도 컨테이너 image) + `admin-api` 의 thin proxy + `admin-ui` 의 Chat 페이지 |
| Agent SDK | **AWS Strands Agents** (AgentCore first-class) |
| Multi-agent 패턴 | **Pattern C+ — Orchestrator + 4 Specialists** (agents-as-tools, 5-agent total). §2.3 참조 |
| LLM (역할별 모델 분배) | Orchestrator: **Opus 4.7** / SQL Specialist: **Sonnet 4.6** / **Code Specialist**: **Sonnet 4.6** / SQL Validator: **Opus 4.7** / Viz Specialist: **Haiku 4.5** (모두 ap-northeast-2 → `global.anthropic.*` cross-region inference profile) |
| Code 실행 환경 | **AWS Bedrock AgentCore Code Interpreter** (Python sandbox, pandas/numpy/scipy/sklearn 사용) |
| 인증 | **Cognito JWT inbound** (admin-ui 의 ID token → AgentCore `CustomJWTAuthorizerConfiguration`) |
| 배포 | AWS Bedrock AgentCore Runtime (VPC 모드, 사설 서브넷 ENI) |
| Query 검증 | Layer A — Schema whitelist + sqlglot parse + EXPLAIN dry-run + 자동 재시도 loop |
| Streaming | SSE (`/invocations` HTTP) — agent thinking → tool call → result 를 incremental 로 전달 |
| Tool 호출 경로 | AgentCore Gateway (Lambda + OpenAPI) |
| Observability | AgentCore Observability (트레이스/메트릭/세션 자동) + 우리의 OTel collector 로 export |

---

## 1. Use Case (MVP — 운영자가 자주 묻는 12가지)

### 1.1 Tier A — SQL 만으로 (8개)

| # | 자연어 예시 | 차트 | 핵심 데이터 |
|---|---|---|---|
| 1 | "이번 달 비용 top 10 사용자" | bar | `usage_logs` × `auth.users` |
| 2 | "어제 평소보다 비싸진 사용자 누구" | table | `usage_logs` 7일 평균 vs 어제 |
| 3 | "팀별 모델 사용 분포" | stacked bar | `usage_logs` × `team` × `model` |
| 4 | "지난 30일 일별 총 비용 추이" | line | `usage_logs` 일자별 SUM |
| 5 | "이번 달 80% 도달한 팀" | KPI cards | `budgets` + `usage_logs` |
| 6 | "지금 활성 VK 가장 많은 사용자" | table | `auth.virtual_keys` |
| 7 | "지난 24h 429 가장 많이 받은 사용자" | bar | `usage_logs` (status_code=429) |
| 8 | "Cognito 등록은 됐는데 한 번도 호출 안 한 사용자" | table | `auth.users` left join `usage_logs` |

### 1.2 Tier B — SQL + Code Interpreter 분석 (4개)

| # | 자연어 예시 | 출력 | 분석 기법 |
|---|---|---|---|
| 9 | "지난 30일 사용 패턴 outlier 사용자 찾아줘" | table + scatter | IsolationForest (sklearn) |
| 10 | "이번 달 비용 추세에서 trend / seasonal / residual 분해" | line panels | STL decomposition (statsmodels) |
| 11 | "다음 달 총 비용 예측해줘" | line + 신뢰구간 | SARIMAX (statsmodels) |
| 12 | "팀별 사용 시간대 패턴 시각화" | heatmap (PNG) | matplotlib seaborn |

추가 (자유 query, v2): 자연어 필터링, drill-down, alert 룰 자동 제안,
회귀 분석 ("input_tokens 가 cost 에 미치는 영향"), 클러스터링 ("비슷한
사용 패턴의 사용자 그룹").

---

## 2. 아키텍처

### 2.1 데이터 흐름 (Pattern C+ — 5-agent + Code Interpreter)

```
┌────────────────────────────────────────────────────────────────────┐
│ admin-ui (Next.js)  /chat                                          │
│   - Chat input + message list (SSE 수신, streaming markdown)       │
│   - Chart renderer (recharts: bar/line/pie/table/kpi)              │
└──────────┬─────────────────────────────────────────────────────────┘
           │ Authorization: Bearer <Cognito ID token>
           │ POST /admin/chat/sessions/{id}/messages  (SSE)
           ▼
┌────────────────────────────────────────────────────────────────────┐
│ admin-api (FastAPI, EKS Pod) — Cognito JWT 검증 + admin 권한 +     │
│  AgentCore InvokeAgentRuntime thin proxy (SSE pass-through)        │
└──────────┬─────────────────────────────────────────────────────────┘
           │ InvokeAgentRuntime (HTTPS + JWT)
           ▼
╔════════════════════════════════════════════════════════════════════╗
║ AWS Bedrock AgentCore Runtime (VPC 모드)                           ║
║                                                                    ║
║  ┌──────────────────────────────────────────────────────────────┐ ║
║  │ ① ORCHESTRATOR  (Claude Opus 4.7)                            │ ║
║  │    - 사용자 의도 이해, 추가 질의 (필요 시)                    │ ║
║  │    - 3개 specialist 위임 결정                                 │ ║
║  │    - 최종 자연어 요약 생성                                    │ ║
║  │                                                              │ ║
║  │    Tools: ask_sql_specialist, ask_validator, ask_viz_spec,   │ ║
║  │           render_chart                                        │ ║
║  └──┬───────────────────────────────────────────────────────────┘ ║
║     │                                                              ║
║     ▼ ask_sql_specialist(question, hints)                          ║
║  ┌──────────────────────────────────────────────────────────────┐ ║
║  │ ② SQL SPECIALIST  (Claude Sonnet 4)                          │ ║
║  │    - Text-to-SQL 전담                                         │ ║
║  │    - schema 화이트리스트 임베드 + few-shot 8개                │ ║
║  │    - self-correction loop (최대 3회, FAIL 시 escalate Opus)   │ ║
║  │                                                              │ ║
║  │    Tools: get_schema, query_db                                │ ║
║  │    Returns: { sql, rows[:5], row_count, columns, note }      │ ║
║  └──┬───────────────────────────────────────────────────────────┘ ║
║     │                                                              ║
║     ▼ ask_validator(question, sql, sample_rows, schema_used)       ║
║  ┌──────────────────────────────────────────────────────────────┐ ║
║  │ ③ SQL VALIDATOR  (Claude Opus 4.7)  ★ 정확도 핵심             │ ║
║  │    - 의미 검증 ("SQL 이 사용자 의도와 일치?")                 │ ║
║  │      • Timezone (KST 가정)                                    │ ║
║  │      • Group by 누락 (top N 류)                               │ ║
║  │      • Filter 해석 ("활성"/"에러"/"비싸진")                   │ ║
║  │      • Aggregation 적정 (SUM vs AVG vs COUNT)                 │ ║
║  │      • 결과 sanity (row_count, 비정상 큰 값)                  │ ║
║  │    Returns: {verdict: PASS|WARN|FAIL, reason, fix?, confidence} │
║  └──┬───────────────────────────────────────────────────────────┘ ║
║     │                                                              ║
║     ├─ FAIL → Orchestrator 가 SQL Specialist 재호출 (with fix)     ║
║     ▼ ask_code_specialist(intent, data_ref)  ★ 분석/예측 케이스만   ║
║  ┌──────────────────────────────────────────────────────────────┐ ║
║  │ ④ CODE SPECIALIST  (Claude Sonnet 4.6)                       │ ║
║  │    - AgentCore Code Interpreter (microVM Python sandbox)      │ ║
║  │    - pre-installed: pandas/scipy/sklearn/statsmodels/         │ ║
║  │      matplotlib (prophet 은 statsmodels SARIMAX 로 대체)      │ ║
║  │    - data 는 SQL Specialist 결과를 S3 staging 으로 받음        │ ║
║  │    - 이상치 탐지 / 시계열 분해 / 클러스터링 / 예측 / heatmap   │ ║
║  │    Tools: execute_python (Code Interp), s3_read, s3_write    │ ║
║  │    Returns: { result_summary, chart_s3_url?, csv_s3_url? }   │ ║
║  └──┬───────────────────────────────────────────────────────────┘ ║
║     │                                                              ║
║     ▼ ask_viz_specialist(data_shape, intent)                       ║
║  ┌──────────────────────────────────────────────────────────────┐ ║
║  │ ⑤ VIZ SPECIALIST  (Claude Haiku 4.5)                         │ ║
║  │    - SQL 결과는 recharts → render_chart kind/encoding         │ ║
║  │    - Code 결과는 PNG (S3 presigned URL) 로 embed 또는         │ ║
║  │      raw data 로 재가공해 recharts                            │ ║
║  │    Returns: { kind, x, y, color?, title } 또는 { image_url } │ ║
║  └──┬───────────────────────────────────────────────────────────┘ ║
║     │                                                              ║
║     ▼ Orchestrator 가 render_chart() 호출 (deterministic tool)     ║
╚═════╤══════════════════════════════════════════════════════════════╝
      │
      │ Deterministic tools (LLM 아님, AgentCore Gateway 경유):
      ▼
┌─────────────┐   ┌─────────────────┐   ┌─────────────────────────┐
│ get_schema  │   │ query_db        │   │ render_chart            │
│ (Lambda)    │   │ (Lambda, VPC)   │   │ (admin-api OpenAPI tgt) │
│             │   │                 │   │                         │
│ yaml 화이트 │   │ sqlglot AST +   │   │ recharts spec 반환      │
│ 리스트 →    │   │ EXPLAIN dry-run │   │ admin-ui 가 렌더        │
│ schema +    │   │ + LIMIT 1000 +  │   │                         │
│ column meta │   │ 10s timeout +   │   │                         │
│             │   │ read-only role  │   │                         │
└─────────────┘   └─────────────────┘   └─────────────────────────┘
```

### 2.2 4-agent 구성 결정 근거

| Agent | 모델 (Bedrock 호출 ID) | 왜 이 모델? |
|---|---|---|
| **Orchestrator** | `global.anthropic.claude-opus-4-7` | 의도 이해 + 멀티턴 대화 + sub-agent 위임 결정 = critical path 의 두뇌. Opus 의 판단력 필요. 1M context 로 긴 대화 보존 |
| **SQL Specialist** | `global.anthropic.claude-sonnet-4-6` | text-to-SQL 은 잘 정의된 task — Sonnet 4.6 (Bedrock 의 latest Sonnet) 으로 충분. extended thinking 활용 가능 |
| **Code Specialist** ★ | `global.anthropic.claude-sonnet-4-6` | Python (pandas/scipy/sklearn) 분석 코드 작성. Code Interpreter sandbox 에서 실행. SQL 만으로 안 되는 분석/예측/이상치 처리 |
| **SQL Validator** | `global.anthropic.claude-opus-4-7` | "SQL 이 사용자 의도와 의미적으로 일치?" 는 sqlglot/EXPLAIN 으론 못 잡는 영역. timezone/groupby/filter 의도 검증은 Opus 의 추론 필수. **`thinking.type: "adaptive"` 만 사용** (4.7 제약) |
| **Viz Specialist** | `global.anthropic.claude-haiku-4-5-20251001-v1:0` | data shape → chart kind 매핑. Code 결과 (PNG embed) vs SQL 결과 (recharts) 분기 |

**ap-northeast-2 호출 제약 (조사 결과)**:
- Anthropic 4.x 모델은 ap-northeast-2 In-Region/Geo 미지원, **`global.*` cross-region inference profile 만 호출 가능**
- IAM `bedrock_allowed_model_arns` 에 inference profile + 호출되는 foundation model 양쪽 모두 허용 필요 — 우리 prod 의 `bedrock_allowed_model_arns` 가 이미 그 패턴이라 추가 작업 적음 (4.7 만 추가 검토)
- Opus 4.7 는 `temperature` / `top_p` / `top_k` 미지원, `thinking.type` 은 `"adaptive"` 만 허용 (Strands 호출 시 주의)

### 2.3 컴포넌트 분리 이유

- **Validator 분리 (LLM)** — sqlglot + EXPLAIN 은 syntactic 만 검증. 의미적 함정
  (KST/UTC, 활성 정의, 비싸진 의 해석) 은 LLM critic 만 잡음. Validator 가
  PASS/WARN/FAIL 명시하면 Orchestrator 가 retry/proceed 판단.
- **SQL Specialist 분리** — schema 화이트리스트 + few-shot 8개를 specialist
  prompt 에 임베드. Orchestrator prompt 가 비대해지지 않음.
- **Viz Specialist 분리** — chart 결정 전 결정적 rule 로 거를 수 있는 케이스
  (단일 숫자 → KPI card) 외엔 LLM 의 "stacked vs grouped" 미묘 판단.
- **admin-api thin proxy**: CORS / 토큰 노출 방지, AgentCore 호출 중앙화.
- **AgentCore VPC 모드**: query_db Lambda 가 Aurora 사설 서브넷 직접 접근.
- **Tool 분리**: query / schema / chart 가 다른 책임. Lambda 별 IAM 최소화.

### 2.4 Code Interpreter 데이터 흐름 (제약 우회)

**제약**: AgentCore Code Interpreter 는 `SANDBOX` (인터넷 차단) / `PUBLIC`
(공용) 만 지원. **사설 VPC subnet attach 미지원** → Aurora 직접 연결 불가.

**우회 흐름**:

```
사용자: "지난 30일 사용 패턴 outlier 사용자"
  │
  ▼
[1] Orchestrator → ask_sql_specialist("30일 사용자별 일일 cost 시계열")
[2] SQL Specialist → query_db(...) → Aurora
[3] query_db 가 결과를 S3 staging 에 Parquet 으로 업로드 (SSE-KMS 암호화)
    s3://llm-gateway-chat-staging/{session_id}/{step_id}.parquet
[4] SQL Specialist 반환: { sql, rows[:10], row_count, s3_uri, schema }
[5] Validator 검증 → PASS
[6] Orchestrator → ask_code_specialist(intent="outlier", data_ref=s3_uri)
[7] Code Specialist (Sonnet 4.6) Python 작성:
    """
    import pandas as pd
    from sklearn.ensemble import IsolationForest
    df = pd.read_parquet('s3://...')  # execution role 권한
    iso = IsolationForest(contamination=0.05).fit(df[['cost_usd']])
    df['outlier'] = iso.predict(df[['cost_usd']]) == -1
    outliers = df[df['outlier']].groupby('user_id').agg(...)
    outliers.to_parquet('s3://.../outliers.parquet')
    plt.savefig('/tmp/chart.png'); upload to S3
    """
[8] execute_python tool 호출 → microVM 에서 실행 → 결과 + S3 URL 반환
[9] Orchestrator 자연어 요약 + chart presigned URL 또는 outlier 데이터 recharts
```

**S3 staging 정책**:
- bucket: `llm-gateway-chat-staging-{account_id}` (별도, KMS 암호화)
- 객체 lifecycle: 1일 후 자동 삭제 (분석은 stateless 하게 끝남)
- prefix: `{session_id}/` — 세션별 격리, IAM 으로 cross-session 접근 차단
- Code Interpreter execution role 은 자기 prefix 만 read/write

### 2.5 SQL 정확도 향상 기법 (2024-2026 SOTA 통합)

| 기법 | 적용 위치 | 효과 |
|---|---|---|
| **Schema Linking (RAG)** | SQL Specialist 의 prompt 빌더 | 100컬럼 schema 중 관련 8-10개만 LLM 에 노출 → context noise ↓ |
| **Few-shot Example Retrieval** (DAIL-SQL) | SQL Specialist 의 prompt 빌더 | 비슷한 질문의 작동하는 SQL 패턴 dynamic 삽입 → hallucination ↓ |
| **Schema Augmentation** | `schema_whitelist.yaml` | 컬럼별 description + sample_values → 의미 매핑 정확 |
| **Self-Consistency** (Validator FAIL 시) | Orchestrator retry loop | N개 후보 SQL 생성 → majority vote → 까다로운 case ↑ |
| **Query Decomposition** | Orchestrator system prompt | 복합 질문을 sub-question 으로 분해 → 단일 SQL 복잡도 ↓ |

#### 2.5.1 Schema Linking with Embedding Retrieval

```
사용자 질문 → Bedrock Titan Text Embedding v2
   ↓ (1024-dim vector)
Aurora pgvector extension 으로 cosine similarity 검색
   ↓
schema_whitelist.yaml 의 컬럼 description + sample_values 도 사전
embedding 처리되어 있음 (build-time 1회)
   ↓
top-K (K=10) 관련 컬럼 추출 → SQL Specialist prompt 의 schema 섹션에만 포함
```

**구현**:
- Aurora 에 `pgvector` extension 추가 (이미 PostgreSQL 16 이라 가능)
- `chat_agent.schema_embeddings` 테이블 — schema yaml 파싱해서 build 시 embedding 저장
- runtime: 사용자 질문 embedding → vector search → narrow schema

#### 2.5.2 Few-shot Example Retrieval (DAIL-SQL 패턴)

```sql
CREATE TABLE chat_agent.golden_examples (
  id           uuid PRIMARY KEY,
  question     text NOT NULL,
  sql          text NOT NULL,
  embedding    vector(1024),
  used_count   integer DEFAULT 0,
  success_rate numeric(3,2),  -- Validator PASS 비율
  created_at   timestamptz DEFAULT now(),
  created_by   text  -- admin email 또는 'auto'
);
```

- **Bootstrap**: 8개 MVP use case 의 question + SQL 수작업 50개 (운영자 + 우리)
- **Runtime**: 새 질문 → embedding → top-3 example similarity 추출 → SQL Specialist prompt 에 dynamic 삽입
- **Auto 축적**: Validator PASS + 사용자 thumbs-up 한 query 자동 추가 (admin 승인 후)

#### 2.5.3 Schema Augmentation (yaml 확장)

```yaml
allowed_tables:
  - schema: public
    table: usage_logs
    columns:
      - name: cost_usd
        type: numeric(10,6)
        description: "한 호출당 비용 (USD). 입력+출력+캐시 합산"
        sample_values: [0.012, 0.045, 0.0001]
      - name: model
        type: varchar(64)
        description: "모델 이름. 'claude-sonnet-4-6' / 'claude-haiku-4-5' / 'claude-opus-4-7' 등"
        sample_values: ["claude-sonnet-4-6", "claude-haiku-4-5", "claude-opus-4-7"]
      - name: status_code
        type: integer
        description: "HTTP 응답 코드. 200=정상, 4xx=클라이언트 에러, 5xx=서버, 429=throttle/budget exceeded"
        sample_values: [200, 429, 500]
      - name: created_at
        type: timestamptz
        description: "호출 시각 (UTC). 사용자 질문이 한국 시간 가정이면 'AT TIME ZONE Asia/Seoul'"
        sample_values: ["2026-05-28T03:14:22Z"]
```

#### 2.5.4 Self-Consistency (Validator FAIL 트리거)

- Validator FAIL → Orchestrator 가 SQL Specialist 에 "3개 다른 후보 SQL 생성" 요청
- 각 후보 query_db 로 실행 → 결과 row_count + sample 비교
- majority winner (2/3 이상 일치) 선택. 일치 없으면 Validator 가 가장 그럴듯한 것 선택
- 비용: SQL Specialist 호출 +2회 (~$0.01 추가) + query_db Aurora +2회

#### 2.5.5 Query Decomposition

Orchestrator system prompt 에 다음 룰 추가:

```
질문이 다음 패턴 중 하나면 sub-question 으로 분해:
- "그리고" / "그 후" / "추가로" 같은 conjunction
- 두 가지 다른 측정값 비교 ("비용 vs 호출수")
- 연쇄 인과 ("X 한 사용자가 Y 도 했나")

분해 예시:
원본: "지난 7일 비싸진 사용자, 그 사람들이 주로 쓴 모델"
Sub-1: "7일 vs 그 전 7일 cost 증가율 top 5"
Sub-2: "위 사용자들의 모델별 호출 분포"
```



```
Specialist → Validator: PASS    → 진행
Specialist → Validator: WARN    → reason 을 chart annotation 으로 노출, 진행
Specialist → Validator: FAIL    → Orchestrator 가 suggested_fix 를 새 hint
                                   로 SQL Specialist 재호출 (최대 2회 retry)
재시도 후에도 FAIL → 사용자에게 "더 구체적으로 질문해주세요" + 시도한 SQL 표시
```

---

## 3. Agent Loop (검증 가능한 query 루프)

### 3.1 단계 (4-agent 흐름)

#### Phase 1 — 의도 파악
1. **사용자 입력** → admin-ui 메시지 전송
2. **Orchestrator** (Opus 4.7) 시작 — system prompt + 사용자 메시지
3. Orchestrator: 의도 명확하면 Phase 2 로, 모호하면 사용자에게 명확화 질문 후 종료

#### Phase 2 — SQL 생성 (specialist 위임)
4. Orchestrator → `ask_sql_specialist(question, hints={tz: "Asia/Seoul"})`
5. **SQL Specialist** (Sonnet 4.6):
   - 필요시 `get_schema(...)` 호출
   - SQL 작성 후 `query_db(sql)` 호출
6. **`query_db` Lambda 의 결정적 검증** (Layer A — sqlglot+EXPLAIN):
   1. `sqlglot.parse(sql, dialect="postgres")` — 파싱 실패 시 LLM 에 에러 피드백
   2. `ast.find(Insert/Update/Delete/Drop/...)` — 발견 시 reject
   3. `ast.find(Table)` 모든 테이블이 화이트리스트에 있는지 검증
   4. `EXPLAIN (FORMAT JSON) <sql>` — 비용 추정 > 50000 시 reject
   5. 실행 (read-only role, `statement_timeout=10s`, `LIMIT 1000` 강제 wrap)
7. SQL Specialist self-correction loop (최대 3회):
   - 결과 0행 또는 에러 → schema 재조회 + SQL 수정
   - 3회 후에도 실패 → Sonnet 4.6 → Opus 4.7 escalation
8. SQL Specialist 반환: `{ sql, rows[:5], row_count, columns, note }`

#### Phase 3 — 의미 검증 (Validator)
9. Orchestrator → `ask_validator(user_question, generated_sql, sample_rows, schema_used, row_count)`
10. **SQL Validator** (Opus 4.7) — 5개 항목 체크:
    - Timezone (KST 가정 확인)
    - Group by 누락 (top N 류)
    - Filter 해석 (모호한 단어: "활성", "에러", "비싸진")
    - Aggregation 적정 (SUM vs AVG vs COUNT)
    - 결과 sanity (row_count, 비정상 값)
11. Verdict 분기:
    - **PASS** → Phase 4 진행
    - **WARN** → reason 을 chart annotation 으로 기록, Phase 4 진행
    - **FAIL** → suggested_fix 를 hint 로 SQL Specialist 재호출 (최대 2회). 그 후에도 FAIL → 사용자에게 "더 구체적 질문" 안내 + 시도 SQL 표시

#### Phase 4 — 시각화 결정
12. Orchestrator → `ask_viz_specialist(data_shape, user_intent)`
13. **Viz Specialist** (Haiku 4.5): data shape (행수/컬럼 type/카테고리 수) + 사용자 intent → `{kind, x, y, color?, title}` 반환
14. Orchestrator → `render_chart(kind, data, x, y, title)` (deterministic tool, admin-api endpoint)

#### Phase 5 — 응답
15. Orchestrator 자연어 요약 stream — "이번 달 비용 top 10 은..."
16. SSE 로 admin-ui 에 markdown + chart spec 전달
17. admin-ui 가 recharts 로 chart 렌더 + markdown body 표시

### 3.2 Schema 화이트리스트 (read-only)

```yaml
allowed_tables:
  - schema: auth
    table: users
    columns: [id, email, role, is_active, team_id, created_at, last_login_at]
    pii_redact: false  # admin 권한자라 email 허용
  - schema: auth
    table: teams
    columns: [id, name, department, created_at]
  - schema: auth
    table: virtual_keys
    columns: [id, user_id, created_at, last_used_at, is_active, ttl_seconds]
    # value (실제 키) 컬럼은 절대 노출 안 함
  - schema: public
    table: usage_logs
    columns: [id, user_id, team_id, model, input_tokens, output_tokens,
              cache_read_tokens, cache_write_tokens, cost_usd,
              status_code, latency_ms, created_at]
  - schema: public
    table: budgets
    columns: [id, scope, scope_id, period, limit_usd, used_usd,
              policy, threshold_pct, period_start, period_end]
  - schema: public
    table: rate_limits
    columns: [id, scope, scope_id, rpm, tpm, cpm, cph]
  - schema: public
    table: model_configs
    columns: [id, alias, provider, is_active, price_input, price_output]
allowed_views:
  - usage_summary_daily   # 사전 집계 view (성능)
  - team_budget_status    # budgets + usage_logs join view
forbidden:
  - auth.users.password_hash      # 항상 제외
  - auth.virtual_keys.value       # 항상 제외
  - audit.audit_logs              # 별도 권한 필요
```

### 3.3 SQL 강제 변형 규칙

- 모든 SQL 은 `SELECT ... LIMIT 1000` 으로 자동 wrap (이미 LIMIT 있으면 작은 값 채택)
- `EXPLAIN (FORMAT JSON, ANALYZE FALSE)` 먼저 실행 — total_cost > 50000 면 reject
- `statement_timeout = 10000ms` (DB session level)
- read-only role 사용 (transaction 없이 SELECT 만)
- `set_config('app.user_id', <admin-id>)` — RLS 가 있다면 적용
- 결과 cell 값이 `bytea` / `jsonb` 등 binary 면 자동 truncate (1KB)

---

## 4. API 명세

### 4.1 admin-api 의 새 엔드포인트

#### `POST /admin/chat/sessions`
세션 생성. AgentCore 의 session_id 발급 (8h TTL).

**Auth**: Cognito JWT (admin role 필수)
**Request**: `{}`
**Response**: `{ "session_id": "uuid", "expires_at": "2026-05-29T03:00:00Z" }`

#### `POST /admin/chat/sessions/{session_id}/messages`
메시지 전송. SSE stream 으로 응답.

**Auth**: Cognito JWT (admin role)
**Request**:
```json
{ "content": "이번 달 비용 top 10 사용자" }
```

**Response (SSE events)**:
```
event: thinking
data: {"text": "사용자별 비용 합계를 조회하겠습니다..."}

event: tool_call
data: {"tool": "query_db", "args": {"sql": "SELECT ..."}}

event: tool_result
data: {"tool": "query_db", "rows": 10, "columns": [...]}

event: tool_call
data: {"tool": "render_chart", "args": {"kind": "bar", ...}}

event: chart
data: {"kind": "bar", "spec": {...}}

event: text
data: {"chunk": "이번 달 비용 top 10 은 다음과 같습니다..."}

event: done
data: {"total_tokens": 2453, "cost_usd": 0.012, "duration_ms": 4200}
```

#### `GET /admin/chat/sessions/{session_id}/history`
지난 대화 조회 (UI 새로고침 후 복원용).

**Response**: `{ "messages": [{role, content, tool_calls, charts, ...}] }`

### 4.2 AgentCore agent 구조 (Strands, Pattern C)

#### 4.2.1 Deterministic tools (Lambda / admin-api)

```python
# tools/deterministic.py
from strands import tool

@tool
def get_schema(table_name: str | None = None) -> dict:
    """Allowed schema 화이트리스트 메타정보. None 이면 전체."""
    # AgentCore Gateway → Lambda → yaml lookup
    ...

@tool
def query_db(sql: str) -> dict:
    """Read-only SELECT. sqlglot AST + EXPLAIN dry-run + LIMIT + 10s timeout."""
    # AgentCore Gateway → Lambda VPC → Aurora reader endpoint
    ...

@tool
def render_chart(kind: str, data: list[dict], x: str, y: str | list[str],
                 title: str | None = None, color: str | None = None) -> dict:
    """admin-ui 렌더용 chart spec. kind ∈ {bar, line, pie, table, kpi, area}."""
    # AgentCore Gateway → admin-api OpenAPI target
    ...
```

#### 4.2.2 Specialist sub-agents (LLM, agents-as-tools)

```python
# agents/sql_specialist.py
from strands import Agent

sql_specialist = Agent(
    # Sonnet 4.6 — Bedrock latest Sonnet (Sonnet 4 는 Bedrock 미존재)
    model="global.anthropic.claude-sonnet-4-6",
    tools=[get_schema, query_db],
    system_prompt=open("prompts/sql_specialist.md").read(),  # schema whitelist + 8 few-shot
    max_iterations=3,  # self-correction loop
)

# agents/code_specialist.py
from bedrock_agentcore.tools.code_interpreter_client import code_session

@tool
def execute_python(code: str, session_id: str | None = None) -> dict:
    """microVM Python sandbox 에서 실행. SANDBOX 모드 (인터넷 차단, S3 만 허용).
    pandas / scipy / sklearn / statsmodels / matplotlib 미리 설치됨."""
    with code_session("ap-northeast-2") as cc:
        result = cc.invoke("executeCode", {
            "code": code, "language": "python", "clearContext": False,
        })
        return {"stdout": ..., "stderr": ..., "files": [...]}

code_specialist = Agent(
    model="global.anthropic.claude-sonnet-4-6",
    tools=[execute_python],  # S3 read/write 는 sandbox 내부 코드에서 boto3 로
    system_prompt=open("prompts/code_specialist.md").read(),
    max_iterations=4,  # 분석 코드 self-correct (예: import 누락)
)

# agents/sql_validator.py
sql_validator = Agent(
    # ap-northeast-2 In-Region/Geo 미지원 → global cross-region inference profile 사용
    model="global.anthropic.claude-opus-4-7",
    tools=[],  # 검증 LLM, tool 호출 없음 — 입력만으로 판단
    system_prompt=open("prompts/sql_validator.md").read(),
    response_format={
        "verdict": "PASS|WARN|FAIL",
        "reason": "str",
        "suggested_fix": "str | null",
        "confidence": "float"
    },
)

# agents/viz_specialist.py
viz_specialist = Agent(
    model="global.anthropic.claude-haiku-4-5-20251001-v1:0",
    tools=[],
    system_prompt=open("prompts/viz_specialist.md").read(),
    response_format={
        "kind": "bar|line|pie|table|kpi|area",
        "x": "str",
        "y": "str | list[str]",
        "color": "str | null",
        "title": "str"
    },
)
```

#### 4.2.3 Orchestrator (top-level agent + agents-as-tools)

```python
# main.py
from strands import Agent, tool
from bedrock_agentcore_starter_toolkit import BedrockAgentCoreApp
from agents.sql_specialist import sql_specialist
from agents.sql_validator import sql_validator
from agents.viz_specialist import viz_specialist
from tools.deterministic import render_chart

app = BedrockAgentCoreApp()

@tool
def ask_sql_specialist(question: str, hints: dict | None = None) -> dict:
    """자연어 질문 → 검증된 SQL + 결과. self-correction 내장."""
    return sql_specialist({"question": question, "hints": hints or {}})

@tool
def ask_validator(user_question: str, generated_sql: str,
                  sample_rows: list[dict], schema_used: list[str],
                  row_count: int) -> dict:
    """SQL 의 의미적 정확성 검증. PASS/WARN/FAIL 반환."""
    return sql_validator({
        "user_question": user_question,
        "generated_sql": generated_sql,
        "sample_rows": sample_rows,
        "schema_used": schema_used,
        "row_count": row_count,
    })

@tool
def ask_code_specialist(intent: str, data_ref: str, hints: dict | None = None) -> dict:
    """SQL 만으로 안 되는 분석 (이상치/시계열/예측/클러스터링) 위임.
    data_ref 는 SQL Specialist 가 staging 한 S3 URI."""
    return code_specialist({"intent": intent, "data_ref": data_ref, "hints": hints or {}})

@tool
def ask_viz_specialist(data_shape: dict, user_intent: str) -> dict:
    """data shape + intent → chart kind/encoding 추천."""
    return viz_specialist({"data_shape": data_shape, "intent": user_intent})

orchestrator = Agent(
    # ap-northeast-2 → global inference profile 필수
    model="global.anthropic.claude-opus-4-7",
    tools=[
        ask_sql_specialist,     # agent-as-tool
        ask_code_specialist,    # agent-as-tool (Code Interpreter)
        ask_validator,          # agent-as-tool
        ask_viz_specialist,     # agent-as-tool
        render_chart,           # deterministic
    ],
    system_prompt=open("prompts/orchestrator.md").read(),
)

@app.entrypoint
def invoke(payload: dict) -> dict:
    return orchestrator(payload["content"])

if __name__ == "__main__":
    app.run()
```

#### 4.2.4 Orchestrator system prompt 발췌

```
당신은 LLM Gateway 운영자의 BI assistant 입니다. 운영자의 자연어 질문에
정확한 데이터 기반 답변과 시각화를 제공합니다.

작업 흐름:
1. 사용자 질문의 의도 파악. 모호하면 명확화 질문.

2. 질문 분류:
   A. 데이터 조회/집계 (top N, 평균, 추세, 분포) → SQL only
   B. 분석/예측 (이상치, 시계열 분해, 클러스터링, 회귀) → SQL + Code
   C. 복합 ("X 한 후 Y 도") → Query Decomposition 으로 분해 후 순차

3. SQL 단계: ask_sql_specialist(question, hints={tz: "Asia/Seoul"})
   - 결과는 sample_rows + s3_uri 반환

4. Validation: ask_validator(question, sql, sample_rows, schema_used, row_count)
   - PASS: 다음 단계
   - WARN: 다음 단계 + reason 을 chart annotation 으로
   - FAIL:
     a) suggested_fix 를 hint 로 SQL Specialist 재호출 (최대 2회)
     b) 그래도 FAIL 이면 self-consistency 활성화 — 3개 후보 SQL → vote
     c) 모두 실패 → 사용자에게 "더 구체적 질문" 요청

5. (분류 B/C 면) Code 단계: ask_code_specialist(intent, data_ref=s3_uri)
   - 분석 코드 + 결과 (S3 또는 inline) 반환

6. Viz 단계: ask_viz_specialist(data_shape, user_intent)
   - SQL 결과 → recharts spec
   - Code 결과 (PNG) → image embed URL

7. render_chart() 로 최종 spec 생성, 자연어 요약 + chart stream

원칙:
- 한국어로 답변, 숫자는 K/M 단위로 축약 (예: $1.2K)
- timezone 은 KST 명시 ("이번 달" = Asia/Seoul 기준 2026-05)
- 결과가 비었으면 그렇게 명확히 알리고 query 조건 완화 제안
- 4.7 모델 제약: temperature/top_p/top_k 사용 금지, thinking.adaptive 만
```

### 4.3 세션 / 메시지 history 관리

#### 4.3.1 DB schema

```sql
CREATE SCHEMA chat_agent;

CREATE TABLE chat_agent.sessions (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  agentcore_id    text,                    -- AgentCore 의 session_id (8h TTL)
  title           text,                    -- 첫 user message 자동 요약 (Haiku 4.5 사용)
  status          text DEFAULT 'active',   -- active | expired | archived
  created_at      timestamptz DEFAULT now(),
  updated_at      timestamptz DEFAULT now(),
  expires_at      timestamptz,             -- AgentCore 세션 만료 시각
  message_count   integer DEFAULT 0,
  total_cost_usd  numeric(10,6) DEFAULT 0
);
CREATE INDEX ON chat_agent.sessions (user_id, updated_at DESC);

CREATE TABLE chat_agent.messages (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id      uuid NOT NULL REFERENCES chat_agent.sessions(id) ON DELETE CASCADE,
  role            text NOT NULL,           -- user | assistant | tool
  content         text,                    -- markdown 본문
  tool_calls      jsonb,                   -- [{tool: "ask_sql_specialist", args, result}]
  charts          jsonb,                   -- [{kind: "bar", spec}]
  validator       jsonb,                   -- {verdict, reason, suggested_fix?}
  cost_usd        numeric(10,6),           -- 이 메시지 처리 비용
  duration_ms     integer,
  created_at      timestamptz DEFAULT now()
);
CREATE INDEX ON chat_agent.messages (session_id, created_at);
```

#### 4.3.2 세션 lifecycle

```
[첫 진입]
  POST /admin/chat/sessions
    → admin-api 가 chat_agent.sessions row 생성
    → AgentCore InvokeAgentRuntime 첫 호출로 agentcore_id 발급 (lazy)
    → expires_at = now + 8h

[메시지 전송]
  POST /sessions/{id}/messages → SSE stream
    → admin-api 가 같은 agentcore_id 로 InvokeAgentRuntime 호출 (멀티턴)
    → AgentCore 가 자체 history 보존 (우리는 prompt 재주입 불필요)
    → SSE event 마다 admin-api 가 messages 테이블에 row 누적
    → 마지막 done 이벤트에 session 정보 + expires_at 갱신

[세션 만료 임박 (5분 전)]
  admin-api 가 SSE 에 event: session_warning 삽입
  admin-ui 는 toast — "세션 곧 만료, 새 대화 시작?"

[8h 만료]
  AgentCore 가 자동 종료
  사용자의 다음 메시지에 admin-api 가 자동 새 sessions row + 새 agentcore_id 발급
  follow-up 이면 직전 messages[-6:] 를 새 세션 system prompt 에 prepend

[archive]
  사용자가 sidebar conversation list 에서 archive 클릭
    → status='archived', list 에서 hide. GET /history 로는 조회 가능
```

#### 4.3.3 Multi-turn / multi-tab

- AgentCore session_id 가 같으면 history 자동 보존 (8h 안)
- 멀티탭: 각 탭 = 독립 sessions row + agentcore_id. 사이드바에서 모든 탭의 conversation 보임
- 사용자 격리: `auth.users.id` FK + admin-api 의 권한 필터로 cross-user 0
- AgentCore Memory 서비스 **사용 안 함** (DB 가 SoT, 비용 절감)

### 4.4 admin-ui 컴포넌트 구조

```
admin-ui/app/(authenticated)/chat/
├── page.tsx                    Chat 메인 페이지 (사이드바 메뉴 항목)
├── ChatLayout.tsx              좌측 세션 목록 / 우측 메시지 영역
├── MessageList.tsx             SSE 수신 → 메시지 누적
├── MessageBubble.tsx           role(user/assistant) + content (markdown)
├── ToolCallBlock.tsx           tool_call 이벤트 → "SQL 실행 중..." UI
├── ChartRenderer.tsx           kind 별 분기:
│                                  - bar/line/pie → recharts
│                                  - table → @tanstack/react-table
│                                  - kpi → custom card
├── useChatStream.ts            SSE hook (EventSource 또는 fetch+ReadableStream)
└── api.ts                      admin-api endpoint 래퍼
```

---

## 5. 권한 모델

### 5.1 누가 admin-chat 을 쓸 수 있나
- 기존 admin role 사용자만 (`ADMIN_EMAILS` 또는 `ClaudeAdmin` 그룹)
- TEAM_LEADER 도 본인 팀 데이터에 한해 접근 (v2 — schema whitelist 에 RLS 추가)

### 5.2 IAM / IRSA

```
admin-chat-agent (AgentCore Runtime)
  └─ AgentCore Service Role (AWS managed)
       └─ Bedrock InvokeModel (Claude)
       └─ Lambda Invoke (query_db, render_chart, get_schema)
       └─ ECR Pull (image)
       └─ CloudWatch Logs

query_db Lambda (VPC mode)
  └─ Lambda execution role
       └─ Aurora connect (read-only role: gateway_chat_reader)
       └─ Secrets Manager: gateway_chat_reader 의 DB 비밀번호
       └─ VPC ENI 생성 권한

admin-api (기존)
  └─ AgentCore InvokeAgentRuntime  ← 추가
```

### 5.3 DB role

```sql
CREATE ROLE gateway_chat_reader LOGIN PASSWORD :pw NOINHERIT;
GRANT CONNECT ON DATABASE gateway TO gateway_chat_reader;
GRANT USAGE ON SCHEMA auth, public TO gateway_chat_reader;
GRANT SELECT ON
    auth.users, auth.teams, auth.virtual_keys,
    public.usage_logs, public.budgets, public.rate_limits,
    public.model_configs
TO gateway_chat_reader;
-- 명시적으로 password_hash, vk value 등은 미부여
ALTER ROLE gateway_chat_reader SET statement_timeout = '10s';
```

---

## 6. Observability

### 6.1 AgentCore native
- 세션별 trace (입력/tool-call/출력)
- 모델별 token / latency 메트릭
- AgentCore Console 의 Observability 탭에서 즉시 확인

### 6.2 우리 OTel 통합
- AgentCore → CloudWatch Logs / X-Ray → OTel Collector → 우리 Aurora `audit.audit_logs`
  (선택, 향후)
- `usage_logs` 테이블에 `source = 'admin-chat-agent'` 컬럼 추가 — 일반 사용자
  호출과 구분, 별도 budget 으로 관리 (admin 도구 사용량 추적)

### 6.3 비용 회계
- admin-chat-agent 가 Claude 호출 → 그 비용은 별도 admin pool 에 적재
- 일반 사용자 호출과 구분되어 monthly 운영 비용 산출

### 6.4 Audit / Compliance

운영자가 어떤 데이터를 봤는지 사후 추적용. 모든 query_db / Code Specialist
실행 = 1 audit row.

```sql
CREATE TABLE audit.chat_agent_queries (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id        uuid NOT NULL,
  message_id        uuid,
  user_id           uuid NOT NULL,
  user_email        text NOT NULL,           -- 누가
  user_question     text NOT NULL,           -- 무엇을
  agent_path        text[],                  -- ['orchestrator','sql_specialist','validator','viz']
  generated_sql     text,
  validator_verdict text,                    -- PASS|WARN|FAIL
  validator_reason  text,
  row_count         integer,                 -- 결과 row 수
  columns_seen      text[],                  -- 노출된 컬럼
  schemas_seen      text[],                  -- 'public', 'auth' 등
  pii_columns_seen  text[],                  -- email 같은 PII 컬럼
  s3_staging_uri    text,                    -- 추출 데이터 위치
  code_executed     text,                    -- Code Specialist 가 실행한 Python (optional)
  total_cost_usd    numeric(10,6),
  duration_ms       integer,
  created_at        timestamptz DEFAULT now()
);
CREATE INDEX ON audit.chat_agent_queries (user_email, created_at);
CREATE INDEX ON audit.chat_agent_queries (created_at);
```

**정책**:
- 모든 query_db 호출 = 1 row (성공/실패 무관)
- 결과 데이터는 row count + columns 메타만 저장 (전체 결과는 안 둠 — privacy)
- 보존 기간 1년 (regulatory). lifecycle 으로 자동 archive 후 삭제
- 조회 권한: super-admin (별도 IAM 그룹 `ClaudeSuperAdmin`) 만
- Aurora `audit.audit_logs` 와 별개 테이블 — chat-agent 만의 트래픽 분리

---

### 6.5 디자인 시스템 (admin-ui 전반에 적용)

### 6.5.1 현재 상태 (감사)
- **스택**: Next.js 14 + Radix UI primitives + Tailwind CSS (이미 설치됨)
- **darkMode**: `tailwind.config.darkMode = ['class']` 설정은 있으나 토글
  컴포넌트 / `ThemeProvider` / 색상 토큰 시스템 미구현
- **세련도**: 기본 shadcn/ui 톤. 인포메이션 dense 한 BI 도구로 진화하면서
  spacing / typography / motion 정합성이 부족

### 6.5.2 도입할 디자인 시스템

**theme provider**:
- `next-themes` 추가 — System / Light / Dark 3-mode (System 이 기본,
  prefers-color-scheme 따라감)
- 토글 버튼: Sidebar 하단 + 사용자 메뉴 안에. icon `Sun`/`Moon`/`Laptop`
  (`lucide-react` 기존 사용 중)

**색상 토큰 (Tailwind CSS variables, shadcn/ui 표준)**:

```css
/* admin-ui/app/globals.css */
@layer base {
  :root {
    /* light */
    --background: 0 0% 100%;
    --foreground: 240 10% 3.9%;
    --card: 0 0% 100%;
    --card-foreground: 240 10% 3.9%;
    --primary: 240 5.9% 10%;
    --primary-foreground: 0 0% 98%;
    --secondary: 240 4.8% 95.9%;
    --muted: 240 4.8% 95.9%;
    --accent: 240 4.8% 95.9%;
    --destructive: 0 84.2% 60.2%;
    --border: 240 5.9% 90%;
    --input: 240 5.9% 90%;
    --ring: 240 5.9% 10%;
    --chart-1: 12 76% 61%;
    --chart-2: 173 58% 39%;
    --chart-3: 197 37% 24%;
    --chart-4: 43 74% 66%;
    --chart-5: 27 87% 67%;
    --radius: 0.625rem;
  }

  .dark {
    /* dark — Vercel/Linear/Raycast 스타일 deep neutral */
    --background: 240 10% 3.9%;
    --foreground: 0 0% 98%;
    --card: 240 10% 3.9%;
    --card-foreground: 0 0% 98%;
    --primary: 0 0% 98%;
    --primary-foreground: 240 5.9% 10%;
    --secondary: 240 3.7% 15.9%;
    --muted: 240 3.7% 15.9%;
    --accent: 240 3.7% 15.9%;
    --destructive: 0 62.8% 30.6%;
    --border: 240 3.7% 15.9%;
    --input: 240 3.7% 15.9%;
    --ring: 240 4.9% 83.9%;
    --chart-1: 220 70% 50%;
    --chart-2: 160 60% 45%;
    --chart-3: 30 80% 55%;
    --chart-4: 280 65% 60%;
    --chart-5: 340 75% 55%;
  }
}
```

**Typography**:
- 본문: Inter (이미 next/font 로 가능) — 14px / 1.5 line-height
- 모노 (SQL / 코드): JetBrains Mono — `font-mono`
- 헤딩: Inter weight 600, tracking -0.02em (모던 느낌)
- 한국어 fallback: `Pretendard` (오픈소스, weight 9단계)

**Layout**:
- Sidebar 폭 256px (collapse 시 64px), 우측 영역 가변
- Content max-width 1280px (대시보드는 full-width 허용)
- Section 간 spacing 24px, card 안 padding 16px
- Border radius 10px 통일 (`--radius: 0.625rem`)

**Motion**:
- 모든 transition 150ms ease-out (Tailwind `transition-all duration-150`)
- Dialog / Sheet / Popover 의 enter/exit 은 Radix primitives 의 기본 (이미 적용)
- Chart 등장 시 `motion-safe:animate-in fade-in-0 slide-in-from-bottom-1`
  (200ms) — Linear / Vercel Dashboard 패턴

**Component 가이드**:
- `Card` — bg-card, border, rounded-[--radius], shadow-sm
- `Button` — primary / secondary / ghost / destructive 4종, size sm/md/lg
- `DataTable` — `@tanstack/react-table` + 정렬/필터/페이지네이션 표준 헤더
- `Sidebar` — collapsible (md ↑ 자동 expand, sm 모바일은 Sheet)
- `Skeleton` — chart 로딩 시 표시
- `Toast` — sonner library (admin 작업 알림)

**Icon**:
- `lucide-react` (이미 사용) — 일관된 stroke-width 1.5
- 의미 있는 icon 만 (장식적 icon 자제)

### 6.5.3 Chat 페이지의 디자인 적용

```
┌────────────────────────────────────────────────────────────────┐
│ Sidebar                  │ Chat                                │
│ - Dashboard              │ ┌────────────────────────────────┐  │
│ - Users                  │ │ ➤ kyutae  10:32                │  │
│ - Models                 │ │   이번 달 비용 top 10 사용자     │  │
│ - Budgets                │ ├────────────────────────────────┤  │
│ - Rate Limits            │ │ ✨ Assistant  10:32             │  │
│ - API Keys               │ │   사용자별 비용 합계를 조회…    │  │
│ ─────────                │ │                                 │  │
│ - Chat        ← 신규 메뉴 │ │   [📊 SQL 실행 중...]          │  │
│ - Monitoring             │ │   ┌─ Bar Chart ─────────────┐   │  │
│ - Settings               │ │   │  user_a  ████████ $124   │  │
│                          │ │   │  user_b  ██████   $89    │  │
│ ─────────                │ │   │  ...                      │  │
│ ☀ Light/🌙 Dark/💻 Sys   │ │   └──────────────────────────┘   │  │
│                          │ │                                 │  │
│ [user@email]             │ │   가장 비싼 사용자는...          │  │
│                          │ ├────────────────────────────────┤  │
│                          │ │ [메시지 입력...]   [↑ Send]    │  │
│                          │ └────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────┘
```

- 메시지 버블: 양쪽 정렬 안 함 (ChatGPT 스타일), full-width 사용
  - user 는 옅은 background (bg-muted), assistant 는 transparent
  - aside icon (Avatar 컴포넌트, 36px) + name + time
- Tool call 진행: 인라인 collapsed pill (`[📊 SQL 실행 중...]`),
  클릭하면 expand 되어 SQL 본문 노출
- Chart: `<Card className="my-3 p-4">` 안에 recharts.
  bg-card / border / rounded-[--radius]
- Markdown 본문: `react-markdown` + `remark-gfm` (코드블럭 highlighting:
  `prism-react-renderer`)
- Empty state: 첫 진입 시 8개 use case suggestion chips
  ("이번 달 비용 top 10", "예산 80% 도달한 팀") 클릭으로 자동 입력

### 6.5.4 차트 색상 (Light / Dark 모두 보기 좋게)

- recharts 사용 시 색상은 `var(--chart-1)` ~ `--chart-5` 토큰 참조
- 6개 이상 카테고리는 generative HSL 사이클 (h += 60°)
- Tooltip / Legend 도 토큰 기반 (border / background / muted-foreground)

### 6.5.5 접근성

- 모든 인터랙티브 요소 keyboard navigation (Radix 기본)
- 다크모드 대비 비율 WCAG AA 이상 (contrast checker 로 검증)
- Chart 는 colorblind-safe palette (Tailwind chart token 이 그렇게 선택됨)
- SR-only label — chart 데이터를 표 alt 로 제공

---

## 7. 보안 / 함정

| 항목 | 대응 |
|---|---|
| **SQL injection** | sqlglot AST 검증 + DDL/DML 거부 + read-only role |
| **권한 escalation** | Cognito JWT 의 admin role claim 강제 (admin-api 에서 검증 후 AgentCore 호출) |
| **PII 노출** | password_hash / vk value 컬럼 화이트리스트 제외 |
| **DoS via heavy query** | EXPLAIN cost 임계 + statement_timeout + LIMIT |
| **Prompt injection** | system prompt 가 user 메시지를 절대 신뢰 안 함, schema whitelist 가 LLM 출력 검증 |
| **Cross-tenant leak** | RLS (v2) — TEAM_LEADER 본인 팀만 |
| **Hallucinated SQL** | sqlglot fail → LLM 에 에러 피드백 → 자동 재시도 (최대 3회) |
| **AgentCore 자체의 권한 남용** | IAM least privilege — Aurora 는 read-only role 만, prod write 절대 불가 |
| **Code Interpreter prompt injection** | sandbox 가 인터넷 차단 (`SANDBOX` 모드) + S3 prefix 제한 + execution role 최소 권한. 사용자에게 노출 = 결과 chart/csv 만, 코드 자체는 admin trace 에만 |
| **Code Interpreter S3 staging 데이터 유출** | bucket 별 KMS 암호화 + 1일 lifecycle + 세션 prefix IAM 격리. cross-session 접근 0 |
| **Code Interpreter 실행 비용 폭주** | per-second 과금이라 무한 loop 가 비쌈. 세션 timeout 5min 강제, max 8h 절대 |
| **Embedding RAG cache poisoning** | golden_examples auto-add 는 admin 승인 후만. embedding 모델은 Bedrock Titan v2 (regional) — 외부 의존 없음 |

### 7.5 Failure Recovery 시나리오

| Failure | 어디서 | 대응 |
|---|---|---|
| SQL Specialist 가 query_db 3회 모두 에러 | self-correction loop | Specialist 종료 후 Sonnet → Opus escalation 1회. 그래도 실패 시 사용자에게 "schema 확인 권장" + Specialist 가 시도한 SQL 표시 |
| Validator FAIL 5회 (suggested_fix 없음) | retry loop | Self-Consistency 활성 (3개 후보 SQL → vote). 그래도 FAIL 이면 사용자에게 "더 구체적 질문" 안내 |
| Code Specialist sandbox timeout (5분) | Code Interpreter | execute_python 에러 → Specialist 가 코드 단순화 후 재시도 (max 2회). 그래도 실패면 "SQL 결과만으로 답하겠습니다" 로 graceful degrade |
| Code Specialist 의 ImportError | sandbox 안 | (a) 사전 설치 패키지 (statsmodels 등) 로 대체 (b) `pip install <pkg>` 호출 (cold 비용 있음). prophet 같이 자주 누락은 Specialist system prompt 에 "statsmodels SARIMAX 우선" 가이드 |
| Aurora reader endpoint 다운 | query_db Lambda | writer endpoint 로 fallback (read-only 트랜잭션 + statement_timeout 강제). 그래도 안 되면 502 + admin-ui "데이터 조회 일시 불가" toast |
| AgentCore InvokeAgentRuntime 5xx | admin-api proxy | exponential backoff 3회 (1s/2s/4s). 모두 실패면 사용자 "잠시 후 재시도" + admin email 알림 (운영자 본인이라도) |
| Bedrock throttle (Claude API rate limit) | LLM 호출 | AgentCore 자체 retry. admin-api timeout 60s. 사용자에게 "응답 지연" 안내 + spinner 유지 |
| S3 staging 업로드 실패 | query_db Lambda | S3 retry 3회. 실패 시 inline 으로 sample 100행만 Code Specialist 에 전달 (전체 분석 불가능 시 사용자 안내) |
| Cognito JWT 만료 (대화 중) | admin-api | 401 → admin-ui silent refresh (기존 OIDC refresh token). 실패 시 재로그인 모달 + 현재 진행 메시지는 messages 테이블에 저장되어 복귀 시 복원 |
| 사용자가 query 중 페이지 닫기 | SSE | SSE disconnect 감지 → admin-api 가 AgentCore InvokeAgentRuntime abort signal 전송. partial 응답을 messages 테이블에 `status='aborted'` 로 저장 |

---

## 8. 구현 로드맵

### Phase 0 — 디자인 시스템 정비 (3-4일, Chat 작업과 병행 가능)
- [ ] `next-themes` 추가 + `ThemeProvider` Layout 에 wrap
- [ ] `app/globals.css` 의 색상 토큰 (light/dark) 정리 + chart token 추가
- [ ] Sidebar 에 Light/Dark/System 토글 추가 (lucide Sun/Moon/Laptop)
- [ ] Pretendard font 추가 (`next/font/local` 또는 CDN)
- [ ] 기존 페이지 (Dashboard / Users / Models / Budgets) 다크모드 검수 +
      contrast 보정
- [ ] DataTable / Card / Button / Skeleton / Toast 표준 컴포넌트 정리
      (이미 shadcn 기반이라 미설치된 것만 추가)
- [ ] 디자인 가이드 문서 (`admin-ui/docs/design.md`) — 색상/typography/spacing
      한 페이지

### Phase 1 — Bootstrap (1주)
- [ ] AgentCore Runtime 환경 만들기 (terraform 모듈 추가, IAM, ECR)
- [ ] Strands hello-world image push 후 InvokeAgentRuntime 검증
- [ ] Cognito JWT inbound 설정 (`CustomJWTAuthorizerConfiguration`)
- [ ] admin-api 에 thin proxy endpoint 추가 (`POST /admin/chat/...`)

### Phase 2 — Tool 구현 + RAG 인프라 (2주)
- [ ] `gateway_chat_reader` DB role 생성 + ESO secret
- [ ] `query_db` Lambda — sqlglot + EXPLAIN + read-only Aurora + S3 staging 업로드
- [ ] `get_schema` Lambda — yaml 화이트리스트에서 응답 생성
- [ ] `render_chart` — admin-api 안의 thin endpoint
- [ ] AgentCore Gateway 에 3개 tool 등록 + Code Interpreter 활성화
- [ ] **Aurora pgvector extension 추가** (`CREATE EXTENSION vector`)
- [ ] **`chat_agent.schema_embeddings` 테이블** + build script (yaml → embedding)
- [ ] **`chat_agent.golden_examples` 테이블** + bootstrap 50개 example 작성
- [ ] **S3 staging bucket** 생성 (KMS, 1일 lifecycle, 세션별 prefix IAM)

### Phase 3 — Agent + UI (3주)
- [ ] **5개 agent system prompt** 작성 — Orchestrator / SQL Specialist /
      Code Specialist / SQL Validator / Viz Specialist
- [ ] **Schema Linking RAG** — 사용자 질문 embedding → narrow schema 추출
- [ ] **Few-shot retrieval** — golden_examples top-3 dynamic 삽입
- [ ] **Self-consistency** retry path (Validator FAIL 시 N=3 후보 생성)
- [ ] **Query decomposition** — Orchestrator system prompt 의 분해 룰
- [ ] Code Specialist 의 `execute_python` tool — `bedrock_agentcore.tools.code_interpreter_client`
- [ ] 8개 MVP use case + 4개 분석 use case (이상치/시계열/예측/heatmap)
      golden test 자동 검증
- [ ] admin-ui `/chat` 페이지 — Phase 0 디자인 토큰 사용
  - MessageList / MessageBubble / ToolCallBlock / ChartRenderer / ImageEmbed
  - Empty state — 12개 use case suggestion chips
  - Light / Dark 모두에서 시각 검수
  - PNG embed 의 다크모드 대응 (Code Specialist 가 matplotlib style 자동 분기)
- [ ] E2E smoke: 12개 use case 정확도 측정 (PASS/WARN/FAIL 비율)

### Phase 4 — 운영 안정화 (1주)
- [ ] AgentCore Observability 와 OTel collector 연동
- [ ] `usage_logs.source = 'admin-chat-agent'` 추적
- [ ] 비용 alarm (admin-chat 비용 임계)
- [ ] admin-guide.md 에 사용법 문서

### Phase 5 — v2 (TODO, 별도)
- [ ] RLS — TEAM_LEADER 가 본인 팀 데이터만
- [ ] Floating widget (현재 페이지 컨텍스트 자동 주입)
- [ ] 자연어 alert 룰 제안 ("매일 9시에 어제 비용 top 5 슬랙 푸시")
- [ ] 자동 budget downgrade rule 제안
- [ ] Code Specialist 활용 추가 use case — 회귀 ("input_tokens 가 cost 영향"),
      클러스터링 ("비슷한 사용 패턴 사용자 그룹"), causal inference

### 8.5 Golden Test Framework + CI Evaluation

**목표**: 12 use case 정확도를 자동 측정 + PR 회귀 방지.

#### 8.5.1 테스트 데이터셋 형식

```yaml
# tests/golden/tier_a/use_case_01.yaml
use_case_id: 01
tier: A   # SQL only
name: "이번 달 비용 top 10 사용자"
question: "이번 달 비용 top 10 사용자"

expected:
  sql:
    required_tables: ["public.usage_logs", "auth.users"]
    required_clauses:
      - "GROUP BY"
      - "ORDER BY .* DESC"
      - "LIMIT 10"
    forbidden_clauses:
      - "INSERT|UPDATE|DELETE|DROP"
    timezone_hint: "Asia/Seoul"

  validator:
    verdict: "PASS"
    min_confidence: 0.8

  result:
    expected_row_count: { min: 0, max: 10 }
    required_columns: ["email", "cost_usd"]

  chart:
    kind: ["bar", "table"]

  agent_path:
    - "orchestrator"
    - "sql_specialist"
    - "validator"
    - "viz_specialist"

  follow_ups:
    - question: "그 중 첫 번째 사용자가 주로 쓴 모델은?"
      expected_chart: ["pie", "bar"]
```

```yaml
# tests/golden/tier_b/use_case_09.yaml — Code Interpreter 케이스
use_case_id: 09
tier: B   # SQL + Code
name: "지난 30일 사용 패턴 outlier 사용자"
question: "지난 30일 사용 패턴 outlier 사용자 찾아줘"

expected:
  sql_specialist_called: true
  code_specialist_called: true

  code:
    required_imports: ["pandas", "sklearn"]
    required_classes: ["IsolationForest"]   # 또는 "DBSCAN" 등 outlier 기법

  agent_path:
    - "orchestrator"
    - "sql_specialist"
    - "validator"
    - "code_specialist"
    - "viz_specialist"

  result:
    has_outliers: true   # 결과에 outlier flag 컬럼
```

#### 8.5.2 Evaluation Harness

```python
# tests/eval/run_golden.py
import asyncio, yaml, re, glob
from pathlib import Path

async def evaluate_case(case_path: str) -> dict:
    case = yaml.safe_load(open(case_path))
    response = await invoke_agent(case["question"])

    score = {"case_id": case["use_case_id"], "checks": {}}

    # 1. SQL 검증
    if "sql" in case["expected"]:
        sql = response.tool_calls.get("query_db", {}).get("sql", "")
        score["checks"]["required_tables"] = all(
            t in sql for t in case["expected"]["sql"]["required_tables"]
        )
        score["checks"]["required_clauses"] = all(
            re.search(p, sql) for p in case["expected"]["sql"]["required_clauses"]
        )
        score["checks"]["forbidden_clauses"] = not any(
            re.search(p, sql, re.I) for p in case["expected"]["sql"]["forbidden_clauses"]
        )

    # 2. Validator
    score["checks"]["validator_verdict"] = (
        response.validator["verdict"] == case["expected"]["validator"]["verdict"]
    )

    # 3. Agent path
    actual_path = [t["agent"] for t in response.tool_calls if "agent" in t]
    score["checks"]["agent_path"] = actual_path == case["expected"]["agent_path"]

    # 4. Chart kind
    if "chart" in case["expected"]:
        score["checks"]["chart_kind"] = (
            response.chart["kind"] in case["expected"]["chart"]["kind"]
        )

    # 5. Code (Tier B 만)
    if case["tier"] == "B":
        code = response.tool_calls.get("execute_python", {}).get("code", "")
        score["checks"]["required_imports"] = all(
            f"import {pkg}" in code or f"from {pkg}" in code
            for pkg in case["expected"]["code"]["required_imports"]
        )

    score["passed"] = all(score["checks"].values())
    score["pass_rate"] = sum(score["checks"].values()) / len(score["checks"])
    return score

async def run_all():
    cases = glob.glob("tests/golden/**/*.yaml", recursive=True)
    results = await asyncio.gather(*[evaluate_case(c) for c in cases])
    pass_rate = sum(r["passed"] for r in results) / len(results)
    print(f"Total: {len(results)}, Pass: {sum(r['passed'] for r in results)}, "
          f"Pass rate: {pass_rate:.1%}")
    return results, pass_rate
```

#### 8.5.3 CI 통합

- **PR trigger**: `tests/golden/**` 또는 `prompts/**` 또는 `agent/**` 변경 시
- **GitHub Actions** (또는 EKS Job) 가 evaluation harness 실행
- **Pass rate 임계**: < 90% 면 PR block
- **Cost guard**: 1 PR run = ~$0.20 (12 case × Sonnet+Validator), 일 50 PR 가정 = $10/day. CI 비용 추적
- **Result artifact**: 각 case 의 pass/fail + actual SQL/code/chart 를 PR comment 로 포스트
- **Regression alert**: main 의 baseline pass rate vs PR — 5%p 이상 떨어지면 reviewer assign

#### 8.5.4 Golden 데이터 운영

- **Bootstrap (Phase 2)**: 12 case 작성 (kyutae + 우리, 2-3일 분량)
- **Auto-add (Phase 3)**: 운영 중 Validator PASS + 사용자 thumbs-up (admin-ui 의 message 별 👍 버튼) → admin 승인 워크플로 거쳐 golden_examples 와 tests/golden 양쪽에 추가
- **Schema drift 감지**: Aurora schema 가 바뀌면 (alembic migration) golden_examples 의 SQL 이 깨질 수 있음 → CI 의 별도 step "regression: schema_breaking" 으로 detect

---

## 9. 미해결 질문

1. AgentCore VPC 모드의 networkMode enum 값 (`PUBLIC` / `VPC`) — 정확한 string
   값 필요. `aws bedrock-agentcore-control create-agent-runtime` 호출 전 확인.
2. AgentCore Memory 서비스를 쓸지 — 현재 spec 은 stateful session 안 씀
   (대화 history 는 admin-api 가 DB 에 저장). Memory 서비스 도입 시 비용·복잡도 증가.
3. admin-chat-agent 가 자기 자신의 LLM 호출을 Gateway 로 보낼지, Bedrock direct 일지.
   - Gateway 경유: 비용·rate 일관 관리 vs latency 약간 증가
   - Bedrock direct: 단순 vs 별도 budget 추적 필요
4. Strands 의 streaming 출력 형식이 우리 SSE 스펙 (event: thinking/tool_call/text)
   과 1:1 매핑 가능한지 — 문서 확인 필요. Strands 가 Pattern C 의 sub-agent
   streaming (각 specialist 의 thinking/tool 호출) 까지 노출하는지가 핵심.
5. AgentCore Gateway 의 Lambda tool 정의 시 Lambda 가 VPC 안에 있어야 하는데,
   AgentCore Gateway 자체가 그 VPC 에서 호출 가능한지. (PrivateLink 또는 cross-VPC?)
6. **Bedrock allowed model ARN 갱신 필요** — 현재 `bedrock_allowed_model_arns`
   에 `claude-opus-4-*` 와 `global.anthropic.claude-*` 패턴이 있어 Opus 4.7 (
   `global.anthropic.claude-opus-4-7`) 이 자동 매치되는지 검증. 안 되면
   tfvars 갱신 후 terraform apply 필요.
7. Opus 4.7 의 제약 (`temperature` 미지원, `thinking.type: "adaptive"` 만)
   이 Strands `Agent(model=...)` 추상화에서 어떻게 표현되는지 확인. Strands 가
   기본으로 temperature 보내면 400 에러.
8. **Code Interpreter sandbox 의 S3 access pattern** — execution role 의
   bucket 권한이 자기 prefix 만으로 가는 게 자동인지, 아니면 우리가 IAM
   policy 작성해야 하는지. AWS sample 검증.
9. **statsmodels SARIMAX 가 prophet 대체로 충분한지** — 사용자 use case 11
   (다음 달 비용 예측) 의 정확도 비교. 첫 spike 에서 둘 다 prototype 후 결정.
10. **golden_examples 의 첫 50개 작성 책임자** — 운영자 (kyutae) 가 직접
    SQL 까지 작성? 우리가 8개 MVP use case 기준으로 prototype 한 뒤 운영자
    검토? Phase 2 시작 전 분담 정리.
11. **Aurora pgvector 성능** — Aurora PostgreSQL 16 의 pgvector extension
    이 우리 schema embedding (200~500개 컬럼) regression 영향 있는지. HNSW
    index 사용 여부.
12. **Self-Consistency 의 비용 제어** — 3개 후보 SQL = Sonnet 호출 ×3 +
    query_db ×3. 무한 retry 방지 위해 Validator FAIL 후에만 1회 활성화 +
    초기 제한 (admin 사용자당 일 5회).

---

## 10. 참고 자료

- AgentCore Runtime 동작: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-how-it-works.html
- AgentCore JWT inbound: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/inbound-jwt-authorizer.html
- AgentCore Gateway: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway.html
- Strands Agents: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/using-any-agent-framework.html
- AgentCore CreateAgentRuntime API: https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_CreateAgentRuntime.html
- sqlglot: https://github.com/tobymao/sqlglot
- Vercel AI SDK SSE 패턴: https://sdk.vercel.ai/docs (참고용 — 우리는 직접 구현)
