# Claude Code beta 헤더 — Bedrock 에 무엇을 넘기나

Claude Code 는 요청마다 `anthropic-beta` 헤더로 쓰고 싶은 beta 기능의 이름을 보냅니다. 게이트웨이는 그중 정해 둔 것만 Bedrock 요청 본문의 `anthropic_beta` 로 옮기고 나머지는 버립니다. Bedrock 은 모르는 이름이 하나라도 섞이면 요청 전체를 400 으로 거부하기 때문입니다.

이 폴더는 Claude Code 가 보내는 beta 를 Bedrock 에 직접 시험한 기록입니다. Claude Code 가 올라갈 때마다 새 이름이 생기므로, 조사는 날짜별 문서로 쌓고 아래 표를 갱신합니다.

## 지금 넘기는 beta

gateway-proxy 설정 `BEDROCK_FORWARD_BETAS` 의 기본값입니다.

```text
dangerous-tool-use-2026-09-03:safeguards,per-turn-control-2026-07-01,
inline-tools-2026-09-15,thinking-display-updates-2026-08-18
```

실제 값은 쉼표로 이은 한 줄입니다.

- `이름:필드` 는 그 beta 를 본문 필드(`safeguards`)가 있을 때만, 그 필드와 함께 넘긴다는 뜻입니다.
- 빈 값이면 모두 버리는 이전 동작으로 돌아갑니다(재빌드 불필요). values 의 `gatewayProxy.env` 에 `BEDROCK_FORWARD_BETAS: ""` 한 줄을 넣고 다시 배포합니다.
- Bedrock native(InvokeModel) 경로에만 적용됩니다. Mantle 경로(Cowork·Codex, Mantle 로 가는 모델 별칭)는 지금처럼 beta 를 넘기지 않으므로, 그 경로의 Claude Code 는 PC 쪽 분류기를 씁니다.

## 지금까지 본 beta

Claude Code 가 게이트웨이(`ANTHROPIC_BASE_URL`)로 보낸 beta 입니다. "Bedrock" 열은 그 이름 하나만 넣고 보낸 요청의 결과입니다(us-west-2).

| beta | Bedrock | 게이트웨이 처리 | 확인 |
|---|---|---|---|
| `dangerous-tool-use-2026-09-03` | 200 | 넘김 (`safeguards` 와 함께) | 2026-10-05 |
| `per-turn-control-2026-07-01` | 200 | 넘김 | 2026-10-05 |
| `prompt-caching-scope-2026-01-05` | 400 | 버림 | 2026-10-05 |
| `context-management-2025-06-27` | 200 | 버림 (`context_management` 필드도 지움) | 2026-10-05 |
| `claude-code-20250219` | 200 | 버림 (달라지는 동작 없음) | 2026-10-05 |
| `interleaved-thinking-2025-05-14` | 200 | 버림 (beta 없이도 같음) | 2026-10-05 |
| `thinking-token-count-2026-05-13` | 200 | 버림 (beta 없이도 같음) | 2026-10-05 |
| `mid-conversation-system-2026-04-07` | 200 | 버림 (beta 없이도 같음) | 2026-10-05 |
| `mid-conversation-tool-changes-2026-07-01` | 200 | 버림 | 2026-10-05 |
| `effort-2025-11-24` | 200 | 버림 (beta 없이도 같음) | 2026-10-05 |
| `afk-mode-2026-01-31` | 200 | 버림 (서버 판정에 불필요) | 2026-10-05 |
| `inline-tools-2026-09-15` | 200 | 넘김 (advisor 를 가리키는 블록은 지움) | 2026-10-09 |
| `thinking-display-updates-2026-08-18` | 200 | 넘김 | 2026-10-09 |
| `advisor-tool-2026-03-01` | 400 | 버림 (advisor 도구도 지움) | 2026-10-09 |
| `redact-thinking-2026-02-12` | 400 | 버림 | 2026-10-09 |
| `structured-outputs-2025-12-15` | 200 | 버림 (영향 미확인) | 2026-10-09 |
| `fallback-credit-2026-06-01` | 200 | 버림 (영향 미확인) | 2026-10-09 |

- 보내는 beta 는 Claude Code 버전, 모델, 계정 기능 플래그에 따라 다릅니다. 2.1.295 를 빈 설정으로 실행하면 위의 앞 11개만 보내고, 계정 플래그가 켜진 사용자는 나머지 6개를 더 보냈습니다.
- 모델마다 받는 기능이 다릅니다. 5.5 세대(Opus·Sonnet·Haiku 5.5)는 4개 모두 받습니다. 메시지별 effort(`per-turn-control`)는 5.0 세대(Opus 5·Sonnet 5)가 받지 않고, 대화 중간 도구 추가(`tool_addition`)는 Sonnet 5 가 받지 않습니다("not supported on this model", 2026-10-10). Claude Code 는 이 400 도 알아보고 그 기능을 빼고 다시 보냅니다.
- Anthropic API 에 직접 붙을 때만 보내고 게이트웨이로는 오지 않은 beta: `message-threads`, `cache-diagnosis`, `mid-conversation-system-clear-at`, `advanced-tool-use`, `extended-cache-ttl`, `thinking-binding-controls`, `oauth`(2.1.295 기준).

## Claude Code 를 올린 뒤 점검

1. 게이트웨이 로그에서 처음 보는 beta 이름과 Bedrock 의 거부를 찾습니다.

   ```bash
   kubectl -n llm-gateway logs deploy/llm-gateway-gateway-proxy --since=24h \
     | grep -E "beta_dropped|bedrock_(stream_)?client_error"
   ```

   - `upstream_compat.beta_dropped`: 버린 beta 이름입니다(이름마다 파드당 한 번).
   - `bedrock_stream_client_error`·`bedrock_client_error`: Bedrock 이 거부한 요청과 그 사유입니다.
2. 처음 보는 이름은 Bedrock 에 직접 시험합니다. 먼저 이름 하나만 넣어 200/400 을 보고, 다음으로 실제 Claude Code 요청 본문을 beta 있음/없음으로 보내 비교합니다. 방법은 2026-10-05 문서 4절에 있습니다.
3. 넘길지 정합니다.
   - 설정만 바꾸면 되는 경우: 본문 필드가 없거나, 게이트웨이가 따로 손대지 않는 필드와 짝인 beta(비교 시험을 통과한 뒤)
   - 코드 변경이 필요한 경우: 그 필드나 응답이 웹 검색 루프, 사용량 집계, 도구 제거(advisor 등)와 얽히는 beta

⚠️ 400 을 내버려 두면 안 되는 이유: 서버 판정 beta 가 실린 요청에 Claude Code 가 모르는 400 이 오면, Claude Code 는 그 beta 를 빼고 다시 보내고 그 대화는 끝까지 PC 쪽 분류기를 씁니다. 게이트웨이가 만든 새 400 하나가 Auto mode 서버 판정을 조용히 끕니다.

## 문서

- [2026-10-05-bedrock-test.md](2026-10-05-bedrock-test.md) — 첫 조사. 이름 수락, 기능 비교, 서버 분류기, 실제 Claude Code 종단 시험, LiteLLM 의 처리 방식. 결론은 넘길 beta 2개(`dangerous-tool-use`, `per-turn-control`)
