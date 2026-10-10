# Bedrock beta 헤더 조사 — Claude Code 의 beta 가 Bedrock 에서 실제로 동작하는가

> 작성 2026-10-05 · 시험용 AWS 계정에서 Bedrock 직접 호출 + 로컬 미니 게이트웨이로 실제 Claude Code 종단 시험 · us-west-2 · 서울 · Claude Code 2.1.286 / 2.1.289

## 요약

Claude Code 가 게이트웨이로 보내는 beta 11개를 Bedrock 에 직접 시험했습니다. 넘겨야 하는 것은 두 개뿐이고, 통째로 넘기면 요청 전체가 400 이 됩니다.

- 넘길 beta: `dangerous-tool-use-2026-09-03`(서버 분류기, 판단 재료 `safeguards` 와 함께), `per-turn-control-2026-07-01`(대화 중간 메시지의 턴별 effort)
- 두 개를 넘기자 실제 Claude Code 2.1.289 에서 400, 별도 분류기 요청, 과금 안내가 모두 사라졌습니다.
- 넘기면 안 되는 것: `prompt-caching-scope-2026-01-05` — Bedrock 이 몰라서 요청 전체가 400 입니다.
- 나머지는 beta 가 없어도 동작이 같거나 이번 기능과 상관없습니다.
- Bedrock 에는 지원 beta 를 알려 주는 API 가 없고 AWS 문서의 표는 오래됐습니다. 그래서 직접 시험한 결과로 목록을 관리합니다.

## 이 문서를 읽는 이유

- 게이트웨이가 왜 beta 2개만 넘기는지, 다른 beta 를 넣어도 되는지 판단할 때
- Claude Code 를 올린 뒤 새 beta 를 시험하는 방법을 찾을 때(4절)
- 작성 시점: 2026-10-05 기록입니다. 본문의 "지금 방식"·"지금 우리"는 수정 전 게이트웨이를 말합니다. 결론은 게이트웨이 설정 `BEDROCK_FORWARD_BETAS` 의 기본값으로 반영됐습니다(구현 `gateway-proxy/src/app/services/upstream_compat.py`). 그 뒤에 더한 beta 2개(`inline-tools`, `thinking-display-updates`)는 README 의 표에 있습니다.

## 1. 배경 — beta 헤더와 전달 방식

- **정의**: 요청마다 붙이는 실험 기능 선택 참여(opt-in) 스위치입니다. 이름은 `기능이름-출시날짜` 형식입니다.
- **왜 있는가 — API 버전 관리(API versioning)**: `anthropic-version`(1P 는 `2023-06-01`, Bedrock 은 `bedrock-2023-05-31`)이 안정 계약(stable contract)을 고정합니다. 새 기능을 계약에 바로 넣으면 기존 클라이언트가 깨질 수 있어(하위 호환성, backward compatibility), 원하는 클라이언트만 beta 로 켭니다.
- **수명 주기(lifecycle)**: beta 로 나온 뒤 정식 출시(GA, General Availability)되거나 폐기(deprecation)됩니다. 날짜만 바뀐 새 버전이 나오기도 합니다(`computer-use-2024-10-22` → `computer-use-2025-01-24`).

**서버는 세 단계로 처리합니다.**

1. **이름 검증(validation)**: 서버가 아는 beta 목록(허용 목록, allowlist)과 대조합니다. 모르는 이름이면 400 `Unexpected value(s)` 입니다.
2. **스키마 확장(schema extension)**: beta 가 켜지면 요청 스키마에 필드가 생깁니다. 꺼진 채 그 필드를 보내면 엄격한 스키마 검증(strict schema validation)에 걸려 400 `Extra inputs are not permitted` 입니다.
3. **동작 변경(behavior change)**: 필드 없이 동작만 바꾸는 beta 도 있습니다. GA 가 되면 beta 없이도 기본 동작이 되고, beta 는 효과 없는 값(no-op)이 됩니다.

**전달 방식(transport)은 플랫폼마다 다릅니다.**

```
[1P]      Claude Code --header anthropic-beta: a,b,c--> api.anthropic.com
[Bedrock] Claude Code --header--> Gateway --body anthropic_beta: [b]--> Bedrock
```

- Bedrock InvokeModel 은 SigV4 로 서명하는 AWS API 라서 Anthropic 헤더가 전달되지 않습니다. 그래서 beta 를 JSON 본문 `anthropic_beta` 배열에 넣습니다. Bedrock Mantle(Anthropic 호환 엔드포인트)은 헤더로 보냅니다.
- 플랫폼마다 지원 목록이 따로 있어, 1P 에서 되는 beta 가 Bedrock 에서는 400 이 날 수 있습니다.
- Claude Code 는 제공자 유형(provider) · 모델 · 설정에 따라 보낼 beta 를 고릅니다. Bedrock 직결(`CLAUDE_CODE_USE_BEDROCK=1`)이면 Bedrock 이 받는 것만 본문에 넣습니다. 게이트웨이(`ANTHROPIC_BASE_URL`)로 붙으면 1P 처럼 헤더로 보내므로, 번역과 거르기는 게이트웨이 몫입니다.
- Claude Code 는 beta 때문에 400 을 받으면 그 기능을 끄고 다시 보냅니다(우아한 기능 저하, graceful degradation). 서버 판정이 오지 않으면 로컬 분류기로 바꿉니다. 그래서 기능이 빠져도 장애로 보이지 않습니다. 단, 서버 판정 beta 가 실린 요청에 Claude Code 가 모르는 400 이 오면 그 대화는 끝까지 로컬 분류기를 씁니다(2026-10-09 확인).
- 게이트웨이가 할 일은 네 가지입니다.
  - 프로토콜 변환(protocol translation): 헤더를 본문으로 옮깁니다.
  - 기본 거부(deny-by-default) 필터링: 허용 목록에 있는 것만 넘깁니다.
  - 짝 맞추기: beta 와 그 beta 가 여는 필드는 함께 넘기거나 함께 버립니다.
  - 응답의 처음 보는 필드 통과: 전방 호환성(forward compatibility)을 지킵니다.

### 예시 코드

① **[1P] 헤더로 보냄**

```bash
curl https://api.anthropic.com/v1/messages \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: dangerous-tool-use-2026-09-03,per-turn-control-2026-07-01" \
  -H "content-type: application/json" \
  -d @request.json
```

② **Claude Code → 게이트웨이** (실제 캡처를 줄인 것)

```http
POST /v1/messages?beta=true
Authorization: Bearer vk-…
anthropic-version: 2023-06-01
anthropic-beta: claude-code-20250219,interleaved-thinking-2025-05-14,…(11개)
content-type: application/json

{"model": "claude-sonnet-5-5", "stream": true, "max_tokens": 128000,
 "safeguards": [...], "context_management": {...}, "messages": [...], ...}
```

③ **게이트웨이: 헤더를 본문으로 옮기고 아는 것만 남김**

```python
FORWARD = {                                  # 넘길 beta → 짝인 본문 필드
    "dangerous-tool-use-2026-09-03": "safeguards",
    "per-turn-control-2026-07-01": None,     # messages[].output_config 를 엶
}
BEDROCK_FIELDS = {"messages", "max_tokens", "system", "tools", "tool_choice",
                  "thinking", "output_config", "metadata", "temperature",
                  "top_p", "top_k", "stop_sequences"}

def to_bedrock_body(headers: dict, body: dict) -> dict:
    sent = [b.strip() for b in headers.get("anthropic-beta", "").split(",")
            if b.strip()]
    keep = [b for b in sent if b in FORWARD]          # 허용 목록만
    out = {k: v for k, v in body.items() if k in BEDROCK_FIELDS}
    out["anthropic_version"] = "bedrock-2023-05-31"   # 버전도 본문으로
    for b in keep:                                    # 짝 필드도 함께
        field = FORWARD[b]
        if field and field in body:
            out[field] = body[field]
    if keep:
        out["anthropic_beta"] = keep                  # 헤더 → 본문
    return out
```

④ **게이트웨이 → Bedrock**: `model` 은 `modelId` 로 가고, `stream` 은 호출할 API 를 고르는 데 씁니다.

```python
brt = boto3.client("bedrock-runtime", region_name="us-west-2")
resp = brt.invoke_model_with_response_stream(
    modelId="us.anthropic.claude-sonnet-5-5",
    body=json.dumps(to_bedrock_body(headers, body)))
```

실제로 나간 본문은 이렇습니다.

```json
{"anthropic_version": "bedrock-2023-05-31",
 "anthropic_beta": ["per-turn-control-2026-07-01",
                    "dangerous-tool-use-2026-09-03"],
 "max_tokens": 128000, "output_config": {"effort": "medium"},
 "safeguards": [{"type": "dangerous_tool_use", "classifier_context": {...}}],
 "messages": [{"role": "user", "content": [...]},
              {"role": "system", "content": [...],
               "output_config": {"effort": "medium"}}],
 "system": [...], "tools": [...], "thinking": {...}, "metadata": {...}}
```

⑤ **응답**: 스트림에서는 판정이 `message_delta.delta` 안에 있습니다.

```
event: message_delta
data: {"type": "message_delta", "delta": {"stop_reason": "tool_use", ...,
       "safeguard_results": [{"type": "dangerous_tool_use", "status": {
         "type": "available", "tool_uses": {"toolu_bdrk_01DZ…": {
           "type": "evaluated", "outcome": "not_flagged"}}}}]}, "usage": {...}}
```

## 2. 결과

### 2-1. Claude Code 가 보내는 beta

게이트웨이(`ANTHROPIC_BASE_URL`)로 붙은 Claude Code 의 본 요청 헤더입니다. 2.1.286 은 9개, 2.1.289 + Sonnet 5.5 는 11개를 보냈습니다.

| beta | 용도 | 비고 |
|---|---|---|
| `claude-code-20250219` | Claude Code 클라이언트 표시 (추정) | |
| `interleaved-thinking-2025-05-14` | 도구 호출 사이 thinking | |
| `thinking-token-count-2026-05-13` | thinking 토큰 수 | |
| `context-management-2025-06-27` | 컨텍스트 편집 | |
| `prompt-caching-scope-2026-01-05` | 캐시 범위 지정 (추정) | |
| `mid-conversation-system-2026-04-07` | 대화 중간 system 메시지 | |
| `per-turn-control-2026-07-01` | 턴별 effort (대화 중간 메시지의 `output_config`) | 2.1.289 에서 추가 |
| `mid-conversation-tool-changes-2026-07-01` | 대화 중간 도구 추가·제거(`tool_reference`) — 2026-10-09 확인 | 2.1.289 에서 추가 |
| `effort-2025-11-24` | 생각 깊이 | |
| `dangerous-tool-use-2026-09-03` | 서버 분류기 | |
| `afk-mode-2026-01-31` | Auto mode | |

2.1.289 바이너리에는 beta 이름이 약 50개 들어 있어, 앞으로 더 늘어날 수 있습니다.

### 2-2. 이름 수락 시험

beta 하나씩 본문 `anthropic_beta` 에 넣고 "hi" 를 보냈습니다. Sonnet 5.5 · Opus 5.5 · Haiku 4.5 · Sonnet 5, us-west-2(`us.*`) · 서울(`global.*`) 모두 결과가 같았습니다.

- **200**: 위 표에서 `prompt-caching-scope` 를 뺀 10개 전부, 그리고 `tool-search-tool-2025-10-19`, `thinking-binding-controls-2026-08-01`
- **400**: `prompt-caching-scope-2026-01-05`, `advisor-tool-2026-03-01`
- 2.1.286 의 9개를 한꺼번에 보내면 400 이고, `prompt-caching-scope` 를 빼면 200 입니다.
- 400 메시지: ``Unexpected value(s) `prompt-caching-scope-2026-01-05` for the `anthropic-beta` header``
- 이 시험은 "hi" 만 보냈기 때문에 beta 가 여는 필드는 다루지 않습니다. 필드는 2-3 에서 따로 확인했습니다.

### 2-3. 기능 시험 (beta 있음 / 없음 비교)

200 은 이름을 받아들였다는 뜻일 뿐이라, 기능이 실제로 달라지는지 따로 비교했습니다. 서버 분류기를 뺀 나머지는 Sonnet 5.5 / us-west-2 에서만 시험했습니다.

| beta | 비교 결과 | 판정 |
|---|---|---|
| `dangerous-tool-use` | 없으면 `safeguards` 400, 있으면 판정 반환 | **beta 필요** |
| `per-turn-control` | 없으면 대화 중간 메시지의 `output_config` 400, 있으면 200 | **beta 필요** |
| `context-management` | 없으면 `context_management` 400, 있으면 도구 결과 2건 실제 삭제 | **beta 필요** |
| `afk-mode` | 혼자서는 `safeguards` 400, 판정은 `dangerous-tool-use` 만으로 나옴 | 서버 분류기엔 불필요 |
| `interleaved-thinking` | 도구 결과 뒤 thinking 이 양쪽 다 나옴 | 기본 동작 |
| `thinking-token-count` | `thinking_tokens` 집계가 양쪽 다 나옴 | 기본 동작 |
| `mid-conversation-system` | 대화 중간 system 지시를 양쪽 다 따름 | 기본 동작 |
| `effort` | low 는 thinking 0, high 는 약 500, 양쪽 같음 | 기본 동작 |
| `tool-search-tool` | 도구 검색 후 호출까지 양쪽 다 됨 | 기본 동작 |
| `claude-code`, `mid-conversation-tool-changes` | 달라지는 동작을 찾지 못함 | 근거 없음 |

### 2-4. 서버 분류기 상세

- **모델 · 리전**: 4개 모델 × 2개 리전 모두 판정을 돌려줬습니다.
- **응답 위치**: 비스트림은 응답 최상위 `safeguard_results`, 스트림은 `message_delta.delta.safeguard_results` 입니다(1절 ⑤).
- **beta 조합**

| 보낸 것 | 결과 |
|---|---|
| `dangerous-tool-use` + `afk-mode` + `safeguards` | 판정 반환 |
| `dangerous-tool-use` + `safeguards` | 판정 반환 |
| `afk-mode` + `safeguards` | 400 |
| `safeguards` 만 | 400 |
| `dangerous-tool-use` 만 (`safeguards` 없음) | 200, 판정 빈 목록 |

- **차단 판정 실측**: 판단 재료 `classifier_context.auto_mode.hard_deny` 에 "`/etc` 권한 변경 금지" 규칙을 넣었습니다.
  - `chmod -R 777 /etc` 는 `flagged` 였습니다.
  - 같은 규칙에서 `ls -la` 는 `not_flagged` 였습니다.
  - 규칙을 `soft_deny` 에 넣었거나 사용자가 그 명령을 직접 시킨 경우는 `not_flagged` 였습니다(`curl | sudo bash` 포함). 위험도가 아니라 사용자 의도를 벗어났는지를 판단합니다.
- **보안**: `classifier_context` 에 홈 · 작업 경로, git 상태, 사용자 신원이 담겨 Bedrock 으로 갑니다.

### 2-5. 실제 Claude Code 종단 시험

로컬에 작은 게이트웨이를 띄웠습니다. 이 게이트웨이는 받은 요청을 바꿔 Bedrock us-west-2 로 보내고, 응답을 SSE 로 돌려줍니다. 바꾸는 방식은 두 가지로 시험했습니다.
- 지금 방식: beta 를 전부 버립니다.
- 고친 방식: 1절 ③ 의 코드를 씁니다.

Claude Code 2.1.289 를 `ANTHROPIC_BASE_URL` 로 이 게이트웨이에 붙이고, `claude -p "list files here, then say done" --permission-mode auto` 를 실행했습니다.

```text
[지금] 게이트웨이가 beta·safeguards 를 버림 (시험 결과 기준)

 사용자 프롬프트
   |
   v
 Claude Code -- ① 본 요청 (beta + safeguards) --> 게이트웨이 --(둘 다 버림)--> Bedrock
 Claude Code <-- ② 400 (턴별 effort 거부) ------- 게이트웨이 <-------------- Bedrock
   |
   |  턴별 effort 를 끄고 다시 보냄 (세션당 한 번)
   v
 Claude Code -- ③ 본 요청 다시 (턴별 effort 뺌) --> 게이트웨이 --(둘 다 버림)--> Bedrock
 Claude Code <-- ④ 응답: tool_use (판정 없음) ----- 게이트웨이 <-------------- Bedrock
   |
   |  판정이 없으므로 로컬 분류기로 전환 + 과금 안내 표시
   v
 Claude Code -- ⑤ 분류기 요청 (약 4.7만 토큰, 별도 과금) --> 게이트웨이 --> Bedrock
 Claude Code <-- ⑥ 판정: 허용 / 차단 ---------------------- 게이트웨이 <-- Bedrock
   |
   v
 ⑦ 도구 실행 (예: curl) --> ⑧ 결과를 담아 다음 본 요청
```

```text
[수정 후] 게이트웨이가 beta 2개·safeguards 를 넘기고 판정을 그대로 돌려줌 (시험 결과 기준)

 사용자 프롬프트
   |
   v
 Claude Code -- ① 본 요청 (beta + safeguards) --> 게이트웨이 --(2개 + safeguards)--> Bedrock
 Claude Code <-- ② tool_use + safeguard_results -- 게이트웨이 <-- Bedrock (응답 + 서버 판정)
   |
   |  safeguard_results 의 판정(not_flagged / flagged)을 그대로 사용
   v
 ③ 도구 실행 (예: curl) --> ④ 결과를 담아 다음 본 요청
```

- 2개 = `dangerous-tool-use-2026-09-03`, `per-turn-control-2026-07-01`
- 출처: ②③ 은 10/5 종단 시험(Claude Code 2.1.289), ⑤⑥ 은 10/1 개발 게이트웨이 시험(curl)에서 측정했습니다.
- 지금 방식의 ②③ 은 2.1.289 에서 생깁니다. 대화 중간 메시지에 붙는 턴별 effort 를 Bedrock 이 beta 없이 거부하기 때문입니다.
- 지금 방식의 ⑤⑥ 은 curl 처럼 판정이 필요한 명령일 때만 생깁니다. 종단 시험의 `ls` 는 읽기 명령이라 생기지 않았습니다.
- 수정 후에는 400 재시도(②③)와 분류기 요청(⑤⑥)이 없어지고, 과금 안내도 뜨지 않습니다.

한 번 실행했을 때의 요청 수와 결과는 아래와 같습니다.

| | 지금 방식 (beta 전부 버림) | 고친 방식 (1절 ③) |
|---|---|---|
| 요청 수 | 3건 (400 1건 포함) | 2건 |
| 첫 요청 | 400 `messages.1.output_config` → Claude Code 가 턴별 effort 를 끄고 재시도 | 200 |
| 서버 판정 | 없음 → `server_no_result` → 로컬 분류기 | Bash 호출 `not_flagged` |
| 과금 안내 | 뜸 | 안 뜸 |

- **오류 형식**: 400 복구는 이 게이트웨이의 실제 오류 형식(`{"error": {"type": "provider_error", "message": "<Bedrock 원문>"}}`)으로도 똑같이 됐습니다.
  - Claude Code 는 오류 문구(`messages.N.output_config: Extra inputs are not permitted`)를 보고 복구 여부를 판단합니다.
  - 그래서 게이트웨이는 Bedrock 오류 문구를 바꾸면 안 됩니다.
- **비용**: 이 400 은 모델 호출 전 검증 단계에서 나므로 토큰 과금은 없습니다.

### 2-6. 모델별 추가 시험 (2026-10-06)

게이트웨이가 실제로 보내는 모델 ID(us-west-2 `us.*` 20회, 서울 `global.*` 5회)로 반복했고, 같은 셀은 매번 결과가 같았습니다.

| 모델 | `per-turn-control` 만 | 메시지별 effort + beta | 대화 중간 `role:system` |
|---|---|---|---|
| Opus 5.5 · Sonnet 5.5 | 200 | 200 | 200 |
| Sonnet 5 | 200 | 400 | 200 |
| Opus 5 · Opus 4.8 | 미시험 | 400 | 200 |
| Haiku 4.5 | 200 | 400 | 400 |
| Opus 4.7 | 미시험 | 400 | 400 |
| Sonnet 4.6 | 미시험 | 400 (system 거부) | 400 |

- Claude Code 는 모델에 맞춰 요청을 바꿉니다. 기본 모델이 Haiku 4.5 이면 beta 5개만 보내고, `per-turn-control` · 메시지별 effort · 대화 중간 system 을 보내지 않습니다. 그래서 지원 모델(Opus 5.5 · Sonnet 5.5 · Haiku 4.5)에서는 게이트웨이가 메시지를 고칠 필요가 없습니다.
- 실제 본문(Sonnet 5.5 · Opus 5.5): 판정 정상, `message_delta.usage` 와 invocationMetrics 일치, 분류기 몫 토큰 없음, 두 번째 요청 캐시 읽기 25,660.
- 웹 검색 경로 오류 모양(HTTP 200 + `event: error`): Claude Code 가 비스트리밍으로 다시 보내고, 400 을 받은 뒤 `per-turn-control` 을 빼고 성공합니다. 장애는 아니지만 첫 턴이 비스트리밍이 되고, 그 프로세스에서는 이 beta 를 다시 보내지 않습니다.

## 3. 게이트웨이에 주는 의미

- **넘김**
  - `dangerous-tool-use` 와 `safeguards` 를 한 짝으로 넘깁니다.
  - `per-turn-control` 은 짝인 최상위 필드가 없습니다. 이 beta 가 여는 `output_config` 는 `messages` 안에 있어서, 게이트웨이가 이미 그대로 넘기고 있습니다.
  - `afk-mode` 는 Claude Code 가 Bedrock 에 직접 붙을 때도 보내는 값이라 같이 넘겨도 됩니다.
- **`context-management` 는 지금은 불필요합니다.**
  - Claude Code 가 지금 보내는 값은 `clear_thinking` + `keep: all` 뿐입니다. 넣었을 때와 뺐을 때 입력 토큰이 477 로 같았습니다.
  - 오래 쉰 뒤 도구 결과를 지우는 `clear_tool_uses` 가 실제 요청에서 보이면 그때 다시 검토합니다.
- **버림**: 나머지는 지금처럼 버립니다. 넘겨도 동작이 같거나 400 이 납니다.
- **새 beta 대비**
  1. 넘길 목록을 설정값으로 둡니다. beta 와 그 beta 가 켜는 본문 필드를 짝으로 적습니다. 새 beta 는 코드 수정 없이 설정 변경 + 재배포로 추가합니다.
  2. 버린 beta · 본문 필드 이름을 로그로 남겨, 새 이름이 처음 나타난 날을 알 수 있게 합니다.
  3. Claude Code 를 올릴 때마다 요청을 캡처해 이전과 비교하고, 새 beta 만 Bedrock 에서 A/B 시험합니다. 이때 "hi" 만 보내지 말고, 캡처한 실제 요청을 그대로 보내야 합니다(`per-turn-control` 처럼 필드가 메시지 안에 숨어 있을 수 있음). LiteLLM 파일이 바뀌었는지도 함께 확인합니다.
  4. "넘기고 400 이면 빼고 재시도" 방식은 쓰지 않습니다. 검토하지 않은 기능이 켜지고, 지연이 늘고, 오류 문구가 바뀌면 깨집니다.

### LiteLLM 방식을 이 게이트웨이에 적용

LiteLLM 의 처리 순서(5절)와 비교한 결과입니다.

| LiteLLM 방식 | 지금 우리 | 권장 |
|---|---|---|
| 제공처별 표 (넘김 / 이름 변경 / 버림, 표에 없으면 버림) | beta 전부 버림 | 채택: 설정값으로 |
| 본문에서 beta 유도 | 없음 | 채택: beta 가 최종 목록에 있을 때만 `safeguards` 를 넘김 |
| 본문 필드 허용 목록 한 곳 + 테스트 고정 | `_BEDROCK_ALLOWED_FIELDS` 있음 | 채택: 고정 테스트 (실제 구현은 `safeguards` 를 허용 목록에 넣지 않고 beta 와 짝으로만 넣음) |
| 버린 beta 경고 로그 | 없음 | 채택: 이름별로 한 번만 |
| 응답은 제자리에서만 고침 | 웹 검색 루프는 `message_delta.delta` 를 새로 만듦 | 채택: 받은 delta 를 복사해 바꿀 값만 덮어씀 |
| 시작할 때 GitHub 에서 표 받기 | — | 안 함: 폐쇄망이고, 검토 없이 운영 동작이 바뀜 |

- **가장 큰 교훈**: 웹 검색 루프에 `safeguard_results` 만 따로 넣어 주면, 다음에 새 응답 필드가 생길 때 또 빠집니다. 받은 것을 복사해서 고쳐야 앞으로 생길 필드까지 통과합니다.
- **LiteLLM 표의 한계**: 보수적이고 일부는 우리 시험과 다릅니다.
  - `clear_thinking` 을 보내면 400 이 난다고 적었지만, beta 와 함께 보내면 정상 동작했습니다.
  - `interleaved-thinking` 은 버리지만 Bedrock 은 받습니다.
  - 그래서 기준은 우리가 직접 시험한 결과로 두고, LiteLLM 표는 조기 경보로만 씁니다.

## 4. 시험 방법

1. **요청 캡처**: 받은 요청을 파일에 쓰고 400 을 돌려주는 로컬 서버를 띄운 뒤, Claude Code 를 그쪽으로 붙입니다. 첫 요청에 헤더 · 본문 · `safeguards` 가 그대로 남습니다.

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:18555 ANTHROPIC_AUTH_TOKEN=dummy \
ANTHROPIC_MODEL=claude-sonnet-5-5 \
claude -p "list files here" --permission-mode auto
```

2. **Bedrock 호출**: beta 를 본문에 넣고 InvokeModel 을 부릅니다(1절 ④).
3. **기능 비교**: 캡처한 실제 본문을 beta 있음 / 없음으로 보내 응답(content 블록, usage, `context_management`, `safeguard_results`)을 비교합니다.
4. **종단 시험**: 로컬 미니 게이트웨이에 실제 Claude Code 를 붙여 요청 수 · 400 · 판정 · 과금 안내를 봅니다(2-5).

- **주의**: Sonnet 5.5 는 `tool_choice` 의 `tool` · `any` 를 400 으로 거부합니다. 도구 호출 시험은 `auto` 로 합니다.
- **규모 · 비용**: 호출 약 190건, 1달러 미만입니다.

## 5. 참고

### 공개된 목록

- **AWS Bedrock 문서**: "Request and Response" 페이지의 `anthropic_beta` 표에 10개가 실려 있습니다. 비고가 Claude 3.7 Sonnet · Opus 4.5 수준에 머물러 있고, `dangerous-tool-use` · `per-turn-control` · `afk-mode` · `mid-conversation-system` · `thinking-token-count` 는 없습니다. 모델 정보 API(`get-foundation-model`)에도 beta 정보는 없습니다.
- **Anthropic 문서**: 플랫폼별 지원 목록은 없습니다.
- **Claude Platform on AWS**: Bedrock 과 다른 상품입니다. Anthropic API 를 그대로 쓰므로 beta 가 1P 와 같습니다.
- **LiteLLM** `anthropic_beta_headers_config.json`: 제공처별(bedrock · bedrock_mantle · vertex_ai 등) 넘김 / 버림 / 이름 바꿔 넘김 목록입니다. 커뮤니티가 관리하며 갱신이 빠릅니다(9/30 `dangerous-tool-use` 전달 추가, 10/2 갱신).

| beta | LiteLLM (bedrock) | 우리 시험 |
|---|---|---|
| `dangerous-tool-use`, `context-management`, `effort`, `tool-search-tool` | 넘김 | 200 |
| `prompt-caching-scope`, `advisor-tool` | 버림 | 400 |
| `per-turn-control` | 버림 (본문에 메시지별 `output_config` 가 있으면 `mid-conversation-output-config-2026-07-01` 을 대신 붙임) | 200, 두 이름 모두 동작 |
| `interleaved-thinking` | 버림 | 200 |
| `thinking-token-count`, `mid-conversation-system`, `afk-mode`, `claude-code` | 목록에 없음 | 200 |

LiteLLM 은 Bedrock 이 받는 beta 도 일부 버리는 보수적인 목록입니다. 넘김과 버림이 갈리는 지점은 우리 결론과 같습니다.

### LiteLLM 처리 순서 (Bedrock InvokeModel)

> LiteLLM 소스 `7a7d27c`(2026-10-05) 를 직접 읽고 정리했습니다.

1. **본문 정리**: `context_management` 에서 Bedrock 이 받는 편집 종류(`compact_20260112`, `clear_tool_uses_20250919`)만 남깁니다.
2. **beta 모으기**: 헤더의 beta 에 본문에서 유도한 beta 를 더합니다.
   - `safeguards` 가 있으면 `dangerous-tool-use` 를 붙입니다.
   - 메시지별 `output_config` 가 있으면 `mid-conversation-output-config` 를 붙입니다.
   - 도구 검색 도구가 있으면 `tool-search-tool` 을 붙입니다.
3. **제공처 표로 거르기**: 표에 없거나 값이 null 이면 버리고, 값이 있으면 그 이름으로 넘깁니다. 이름을 바꿔 넘길 수도 있습니다(`advanced-tool-use` 를 `tool-search-tool` 로). 버린 클라이언트 beta 는 경고 로그로 남깁니다.
4. **넣기**: 걸러진 beta 를 `body.anthropic_beta` 에 넣습니다.
5. **마지막 안전망**: 허용 목록 밖의 최상위 필드를 모두 지웁니다. 허용 목록은 타입 정의 한 곳(`BedrockInvokeAnthropicMessagesRequest`, `safeguards` 포함)이 기준이고, 테스트가 목록을 정확히 고정합니다.
6. **응답**: 스트림의 `message_delta` 는 usage 만 제자리에서 고칩니다. 그래서 처음 보는 필드도 그대로 통과합니다.

- **표 갱신**: 시작할 때 GitHub main 에서 표를 받아 오고, 실패하면 패키지에 들어 있는 사본을 씁니다. 환경 변수로 원격 조회를 끌 수 있습니다.
- **대화 중간 system 메시지**: 모델 정보 표에 지원 표시가 있는 모델(4.8 이상, 5 계열)은 그대로 둡니다. 옛 모델에서는 user 메시지로 바꿉니다.
- **관련 파일**
  - `litellm/anthropic_beta_headers_manager.py`
  - `litellm/anthropic_beta_headers_config.json`
  - `litellm/llms/bedrock/messages/invoke_transformations/anthropic_claude3_transformation.py`
  - `litellm/types/llms/bedrock.py`

### 링크

- AWS Bedrock Request and Response — https://docs.aws.amazon.com/bedrock/latest/userguide/model-parameters-anthropic-claude-messages-request-response.html
- Anthropic Beta headers — https://platform.claude.com/docs/en/api/beta-headers
- Claude Platform on AWS Feature support — https://docs.aws.amazon.com/claude-platform/latest/userguide/feature-support.html
- LiteLLM beta 목록 — https://github.com/BerriAI/litellm/blob/main/litellm/anthropic_beta_headers_config.json
