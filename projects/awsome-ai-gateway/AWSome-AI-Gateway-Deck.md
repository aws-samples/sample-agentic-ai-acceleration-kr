---
marp: true
theme: default
paginate: true
footer: "© 2026, Amazon Web Services, Inc. or its affiliates. All rights reserved."
---

<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

:root {
  --aws-black: #000000;
  --aws-white: #FFFFFF;
  --aws-squid-ink: #161D26;
  --aws-cloud: #F3F3F7;
  --aws-blue: #41B3FF;
  --aws-purple: #AD5CFF;
  --aws-green: #00E500;
  --aws-pink: #FF5C85;
  --aws-orange: #FF693C;
  --aws-yellow: #FBD332;
  --aws-smile-orange: #FBAE40;
  --aws-link: #41B1E8;
  --color-bg: var(--aws-squid-ink);
  --color-fg: var(--aws-cloud);
  --color-heading: var(--aws-white);
  --color-accent: var(--aws-blue);
  --color-accent-secondary: var(--aws-smile-orange);
  --color-muted: #9CA3AF;
  --color-border: #374151;
  --color-surface: #1F2937;
  --font-heading: 'Amazon Ember Display', 'Inter', 'Helvetica Neue', sans-serif;
  --font-body: 'Amazon Ember', 'Inter', 'Helvetica Neue', sans-serif;
  --font-mono: 'Amazon Ember Mono', 'Consolas', 'Monaco', 'Courier New', monospace;
  --font-code: 'Consolas', 'Monaco', 'Courier New', monospace;
}

section {
  background-color: var(--color-bg);
  color: var(--color-fg);
  font-family: var(--font-body);
  font-weight: 400;
  font-size: 22px;
  line-height: 1.7;
  padding: 56px;
  border-top: 8px solid var(--color-accent);
  box-sizing: border-box;
  position: relative;
}

h1, h2, h3, h4, h5, h6 {
  font-family: var(--font-heading);
  font-weight: 700;
  color: var(--color-heading);
  margin: 0;
  padding: 0;
}

h1 { font-size: 54px; line-height: 1.3; letter-spacing: -0.02em; }

h2 {
  position: absolute;
  top: 40px;
  left: 56px;
  right: 56px;
  font-size: 38px;
  padding-bottom: 16px;
  border-bottom: 3px solid var(--color-accent);
}

h2 + * { margin-top: 112px; }

h3 {
  color: var(--color-accent);
  font-size: 26px;
  margin-top: 32px;
  margin-bottom: 12px;
  font-weight: 600;
}

ul, ol { padding-left: 32px; }
li { margin-bottom: 10px; line-height: 1.7; }
li::marker { color: var(--color-accent); }

a { color: var(--aws-link); text-decoration: none; }
strong { color: var(--color-heading); font-weight: 700; }

code {
  background-color: var(--color-surface);
  color: var(--color-heading);
  padding: 2px 6px;
  border-radius: 3px;
  font-family: var(--font-code);
  font-size: 0.9em;
}

pre {
  background-color: #0D1117;
  color: var(--aws-cloud);
  border: 1px solid #30363D;
  border-radius: 6px;
  padding: 24px;
  font-size: 18px;
  line-height: 1.5;
  overflow-x: auto;
}

pre code {
  background-color: transparent;
  color: inherit;
  padding: 0;
}

/* Syntax highlighting */
pre .hljs-comment, pre .hljs-quote { color: #8B949E; font-style: italic; }
pre .hljs-keyword, pre .hljs-selector-tag, pre .hljs-type { color: #FF7B72; }
pre .hljs-string, pre .hljs-addition { color: #A5D6FF; }
pre .hljs-number, pre .hljs-literal { color: #79C0FF; }
pre .hljs-built_in, pre .hljs-function { color: #FFA657; }
pre .hljs-variable, pre .hljs-attr, pre .hljs-template-variable { color: #D2A8FF; }
pre .hljs-title, pre .hljs-section { color: #D2A8FF; font-weight: 700; }
pre .hljs-deletion { color: #FF5C85; }
pre .hljs-meta { color: #79C0FF; }

table { border-collapse: collapse; width: 100%; margin: 20px 0; font-size: 18px; }
th, td { border: 1px solid var(--color-border); padding: 12px; text-align: left; }
th { background-color: var(--color-accent); color: var(--aws-white); font-family: var(--font-heading); font-weight: 700; }
td { background-color: var(--color-bg); color: var(--color-fg); }
tr:nth-child(even) td { background-color: var(--color-surface); }

footer { font-size: 14px; color: var(--color-muted); position: absolute; left: 56px; right: 56px; bottom: 32px; }
section::after { font-size: 14px; color: var(--color-muted); position: absolute; right: 56px; bottom: 32px; }

/* Cover */
section.cover {
  border-top: none;
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: flex-start;
  background: linear-gradient(135deg, var(--aws-squid-ink) 0%, #0D1117 100%);
  padding: 80px;
}
section.cover::before { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 8px; background: linear-gradient(90deg, var(--aws-smile-orange), var(--aws-blue)); }
section.cover h1 { font-size: 54px; color: var(--aws-white); margin-bottom: 24px; }
section.cover h2 { position: static; font-size: 28px; font-weight: 500; color: var(--color-muted); border-bottom: none; padding-bottom: 0; }
section.cover h2 + * { margin-top: 0; }
section.cover p { font-size: 22px; color: var(--color-fg); font-weight: 400; margin-top: 16px; }
section.cover footer { display: none; }
section.cover::after { display: none; }

/* Agenda */
section.agenda { padding-top: 120px; }
section.agenda ol { list-style: none; counter-reset: agenda; padding-left: 0; margin-top: 24px; }
section.agenda ol li {
  counter-increment: agenda;
  font-size: 26px; font-weight: 500;
  padding: 12px 0;
  border-bottom: 1px solid var(--color-border);
  display: flex; align-items: center; gap: 20px;
}
section.agenda ol li::before {
  content: counter(agenda);
  display: inline-flex; align-items: center; justify-content: center;
  width: 38px; height: 38px; border-radius: 50%;
  background-color: var(--color-accent); color: var(--aws-squid-ink);
  font-family: var(--font-heading); font-size: 19px; font-weight: 700; flex-shrink: 0;
}

/* Divider */
section.divider {
  border-top: none;
  display: flex; flex-direction: column; justify-content: center; align-items: center; text-align: center;
  background: linear-gradient(135deg, var(--color-accent) 0%, var(--aws-purple) 100%);
  color: var(--aws-white);
}
section.divider h1 { color: var(--aws-white); font-size: 54px; }
section.divider h2 { position: static; color: var(--aws-white); font-size: 42px; border-bottom: 3px solid rgba(255,255,255,0.4); display: inline-block; padding-bottom: 12px; }
section.divider h2 + * { margin-top: 0; }
section.divider p { color: rgba(255,255,255,0.85); font-size: 24px; margin-top: 16px; }
section.divider footer { display: none; }
section.divider::after { display: none; }

/* Comparison */
section.comparison { padding-top: 120px; }
section.comparison h3 { margin-top: 0; }
section.comparison ul { display: grid; grid-template-columns: 1fr 1fr; gap: 40px; list-style: none; padding-left: 0; }
section.comparison ul li { margin-bottom: 0; }

/* Showcase */
section.showcase { padding-top: 120px; }
section.showcase h3 { font-size: 22px; font-weight: 400; color: var(--color-muted); margin-top: 16px; }
section.showcase p img { max-height: 400px; border-radius: 8px; box-shadow: 0 4px 16px rgba(0, 0, 0, 0.3); }

/* Code */
section.code { background-color: #0D1117; color: var(--aws-cloud); border-top: 8px solid var(--color-accent); }
section.code h2 { color: var(--aws-white); border-bottom-color: var(--color-accent); }
section.code h3 { color: var(--color-accent); }
section.code pre { background-color: #000000; border: 1px solid #30363D; font-size: 16px; }
section.code code { background-color: rgba(255, 255, 255, 0.08); color: var(--aws-cloud); }
section.code strong { color: var(--aws-white); }
section.code footer { color: rgba(255, 255, 255, 0.3); }
section.code::after { color: rgba(255, 255, 255, 0.3); }

/* Closing */
section.closing {
  border-top: none;
  display: flex; flex-direction: column; justify-content: center; align-items: center; text-align: center;
  background: linear-gradient(135deg, var(--aws-squid-ink) 0%, #0D1117 100%);
}
section.closing::before { content: ''; position: absolute; bottom: 0; left: 0; right: 0; height: 8px; background: linear-gradient(90deg, var(--aws-smile-orange), var(--aws-blue)); }
section.closing h1 { font-size: 54px; color: var(--aws-white); margin-bottom: 24px; }
section.closing h2 { position: static; font-size: 28px; font-weight: 500; color: var(--color-muted); border-bottom: none; }
section.closing h2 + * { margin-top: 0; }
section.closing p { font-size: 20px; color: var(--color-muted); margin-top: 12px; }
section.closing footer { display: none; }
section.closing::after { display: none; }
</style>

<!-- _class: cover -->

# AWSome AI Gateway

## 요청을 전달하는(relay) 프록시가 아니라, 새로 만들어 보내는(re-origination) 통제 평면

Kyutae Park · AWS · 2026 · gateway-proxy rev75

---

<!-- _class: agenda -->

## Agenda

1. 개요 · 아키텍처 — 통제 평면과 8단 파이프라인
2. 키 발급 · 거버넌스 — 3-client 멀티계정 다섯 레버
3. 안전성 · 견고성 — 헤더·무효키·적대검증
4. 서버사이드 웹서치 · 기술 하이라이트 · 배포

<!--
10개 챕터의 논리 흐름을 먼저 보여줘 청중에게 지도를 줍니다. 개요·아키텍처로 프레임을 세우고, 키/거버넌스로 실체를 보인 뒤, 안전성 3챕터로 신뢰를 쌓고, 웹서치·기술·배포로 깊이를 더한 다음, 정직한 다음 단계로 닫는 구조라고 안내하십시오. 이 덱은 코드 근거와 라이브 검증을 모든 주장에 붙인다는 점을 첫 마디로 강조하십시오.
-->

---

<style scoped>
li { font-size: 18px; margin-bottom: 6px; line-height: 1.5; }
</style>

## 발표 전 30초: 이 덱의 핵심 용어 (1/2)

- **re-origination (재구성 발신)** — 이 덱의 핵심축. 받은 요청을 그대로 넘기지 않고, 게이트웨이가 허용된 항목만 골라 요청을 '새로 만들어' 모델에 보내는 방식 — 그래서 새어나갈 표면 자체가 없음
- **relay (전달)** — re-origination의 반대. 받은 헤더·키를 손대지 않고 그대로 뒤로 흘려보내는 '얇은 프록시' 방식이며, 남의 키가 섞여 새는 사고의 원인 (우리는 이걸 안 함)
- **control plane (통제 평면)** — 단순 중계기가 아니라 사내 모든 LLM 요청이 반드시 지나며 인증·권한·예산·속도·추적이 한 곳에서 강제되는 '단일 관문' 역할
- **VK (Virtual Key, 가상 키)** — 게이트웨이가 사용자에게 발급하는 임시 출입증. 실제 AWS 자격증명과 분리돼 있어 새어도 원본 클라우드 접근권은 안전
- **passthrough 프록시** — 받은 요청·헤더를 검사 없이 그대로 통과시키는 중계기 (LiteLLM류). 편하지만 남의 키가 새거나 통제가 안 되는 구조적 약점이 있음
- **whitelist 재구성 (화이트리스트 재구성)** — '허용 목록에 든 항목만 통과, 나머지는 버림' 방식으로 요청을 다시 조립하는 것 — 몰래 끼어든 필드·헤더가 뒤로 못 넘어감
- **8단 미들웨어** — 요청 1건이 반드시 순서대로 통과하는 8개의 검문소(관측→앱식별→인증→권한→예산→강등→속도제한→헤더정리). 통제가 코드 곳곳이 아니라 이 경로에 박혀 있음
- **circuit breaker (회로 차단기)** — 특정 모델이 계속 실패하면 자동으로 잠시 '차단'해 더 못 부르게 막는 안전장치 — 고장난 모델에 요청이 몰려 전체가 느려지는 걸 방지

<!--
발표 시작 시 이 두 슬라이드를 30초간 훑어 청중에게 반복될 핵심 용어의 뜻을 미리 심어 둡니다. 특히 re-origination과 relay는 덱 전체를 관통하는 축이니 반드시 짚으십시오 — "받은 요청을 그대로 흘리면 relay, 허용된 것만 골라 새로 만들어 보내면 re-origination이고 우리는 후자다"라고 한 문장으로 각인시키십시오. 나머지는 "뒤에서 나올 때마다 여기로 돌아오면 된다"고 안내하면 비전문가 청중의 이해도가 크게 올라갑니다.
-->

---

<style scoped>
li { font-size: 18px; margin-bottom: 6px; line-height: 1.5; }
</style>

## 발표 전 30초: 이 덱의 핵심 용어 (2/2)

- **degradation (열화/등급 강등)** — DB·캐시 등 인프라가 불안정해지면 시스템이 스스로 '건강 등급'을 낮춰 위험 요청을 차단하거나 축소 운영하는 상태 관리
- **routing_profiles (라우팅 규칙 행)** — '어느 클라이언트를 어느 AWS 계정·모델로 보낼지'를 정하는 DB의 한 줄. 이 한 줄만 바꾸면 재배포 없이 즉시 경로 변경·롤백 가능
- **cross-account (계정 간 접근)** — 게이트웨이가 자기 AWS 계정이 아닌 다른 계정의 모델을 안전하게 빌려 쓰는 것 (권한을 잠깐 위임받아 호출). 실패하면 자기 계정으로 조용히 되돌아감
- **SigV4 / IRSA** — 게이트웨이가 AWS에 접속할 때 쓰는 정식 서명 방식(SigV4)과, 그 서명 권한을 코드에 키를 심지 않고 쿠버네티스 파드에 안전하게 부여하는 방식(IRSA)
- **MCP / AgentCore (Runtime·Gateway)** — MCP는 AI가 외부 도구(예: 웹검색)를 부르는 표준 규격. AgentCore는 그 도구·에이전트를 실행해주는 AWS 관리형 백엔드(Runtime=실행, Gateway=웹검색 도구 제공)
- **tool_use (도구 호출) / 서버사이드 웹서치** — 모델이 '검색이 필요하다'고 스스로 내는 신호가 tool_use. 게이트웨이가 이걸 가로채 대신 검색·재투입하므로, 사용자는 아무 설정 없이 검색이 됨
- **Mantle** — 사내에서 GPT·Claude 같은 외부 모델을 대신 호출해주는 중개 백엔드. 게이트웨이는 이걸 통해 여러 벤더 모델을 같은 방식으로 사용
- **SSE / TTFT / fan-out** — SSE=답변을 한 글자씩 실시간으로 흘려보내는 스트리밍 방식, TTFT=첫 글자가 나오기까지 걸린 시간(체감 속도), fan-out=한 요청이 여러 갈래로 퍼져 나가 폭주하는 현상(가드레일로 억제)

<!--
용어집 두 번째 장입니다. tool_use·MCP·AgentCore는 웹서치 챕터에서, degradation·circuit breaker는 견고성 챕터에서, Mantle·cross-account는 아키텍처 챕터에서 각각 다시 나오니 "그때 이 정의로 돌아오라"고 안내하십시오. SigV4/IRSA는 AWS에 접속하는 정식 서명 방식과 키를 코드에 안 심는 권한 위임 방식이라고 한 번 더 풀어 주면 보안 청중이 안심합니다.
-->

---

<!-- _class: divider -->

## 01. 개요 + OSS(LiteLLM) 대비 차별점

왜 오픈소스를 사서 안 쓰고 직접 만들었나 — 받은 요청을 검사 없이 통과시키는 중계기(passthrough 프록시)가 아니라, 허용된 것만 골라 새로 만들어 보내는 게이트웨이(re-origination)

<!-- 이 챕터의 목적은 청중의 첫 오해 — 'LLM 게이트웨이 = OpenAI로 가는 얇은 중계기' — 를 깨는 것입니다. 사내의 모든 LLM 트래픽이 반드시 지나는 단일 관문(control plane)에서 인증·앱별 접근권(ACL)·모델 허용 목록·예산·호출 속도 제한·웹서치 켜고끄기·사용량 추적이 전부 한곳에서 강제됩니다. 발표 전체를 관통하는 축은 한 문장입니다: '우리는 받은 요청을 그대로 전달(relay)하지 않고, 허용된 항목만 골라 요청을 새로 만들어 보낸다(re-origination).' 이 챕터에서 LiteLLM 대비 5축을 세워두면, 이후 각 챕터가 그 축의 근거를 코드로 하나씩 채워가는 구조가 됩니다. -->

---

## 우리는 중계기가 아니라 '통제 평면'이다

<!-- CH 01 · DEEP DIVE — LLM 게이트웨이란 그리고 우리는 무엇이 다른가 (전반부) -->

- 사내 모든 LLM 요청이 반드시 지나는 단일 관문(control plane) — 인증·과금·경로 선택·추적을 한 곳에서 강제
- 3개 클라이언트(claude-code / codex / cowork)를 서로 다른 AWS 계정의 Bedrock으로 각각 라우팅
- **핵심 차이**: 받은 요청을 그대로 전달(relay)하지 않고, 허용 목록에 든 것만 골라 새로 만들어 보냄(re-origination)
- 사용자 임시 출입증(VK, Virtual Key)과 실제 클라우드 자격(IRSA/SigV4/broker)이 완전 분리 — 출입증이 새도 클라우드는 안전

근거: `gateway-proxy/src/app/main.py:373-386`

<!-- 청중이 게이트웨이를 얇은 중계기로 오해하는 것을 첫 문장에서 깨십시오. 우리 게이트웨이는 사내 모든 LLM 트래픽이 반드시 지나는 단일 관문이고, 여기서 인증·앱별 접근권(ACL)·모델 허용 목록·앱별 예산·호출 속도 제한·웹서치 켜고끄기·사용량 추적이 전부 강제됩니다. 가장 강조할 문장은 '우리는 요청을 그대로 전달(relay)하지 않고, 허용된 것만 골라 새로 만들어 보낸다(re-origination)'로, 이후 LiteLLM 대비 전체 서사의 축입니다. 3개 클라이언트가 실제로 서로 다른 AWS 계정으로 나갑니다(claude-code는 333 계정에서 Bedrock 직접 호출, codex는 게이트웨이와 같은 123 계정에서 Mantle 경유, cowork는 222 계정에서 Mantle 경유). 이 경로는 routing_profiles DB 한 줄로 결정되고, 그 한 줄만 바꾸면 재배포 없이 즉시 롤백됩니다. VK(Virtual Key)는 게이트웨이가 사용자에게 발급하는 임시 출입증인데, 실제 AWS 자격증명과 분리돼 있어 이게 새도 원본 클라우드 접근권은 안전합니다. 대기업 보안 심사에서 '한 사용자의 키가 다른 곳으로 새지 않는가'와 '누가 무엇을 얼마나 썼는지 통제·추적되는가'가 통과 기준이기 때문입니다. -->

---

## 모든 요청이 같은 8단 검문소를 지난다

<!-- CH 01 · DEEP DIVE — LLM 게이트웨이란 그리고 우리는 무엇이 다른가 (후반부) -->

- 단일 FastAPI + 8개 검문소(8단 미들웨어)로 모든 요청에 통제를 강제 — 한쪽 앱만 통제가 새는 구멍이 없음
- 설정 없이 바로 되는 서버사이드 웹서치까지 — 정품 Claude와 똑같은 사용 경험
- 결론: 단순 프록시를 넘어 사용 경험·거버넌스·보안을 통째로 소유하는 통합 플랫폼
- 예상 질문 "그냥 LiteLLM 쓰면 안 되나?" → 이 챕터 마지막 5축 비교에서 정면으로 답함

근거: `gateway-proxy/src/app/main.py:373-386`

<!-- 앞 슬라이드에 이어 게이트웨이가 단순 중계기가 아닌 이유를 마무리합니다. 단일 FastAPI에 8개 검문소(8단 미들웨어)를 박아 어떤 클라이언트에서도 통제가 균일하게 강제되므로, 특정 앱에서만 통제가 빠지는 구멍이 생기지 않습니다. 마지막 강조점인 '설정 없이 되는 서버사이드 웹서치'는 단순 게이트웨이를 넘어 사용 경험까지 소유한다는 복선입니다 — 정품 Claude와 똑같이 검색이 되지만 게이트웨이가 서버에서 대신 수행합니다. 예상 질문 '그냥 LiteLLM 쓰면 안 되나?'는 이 챕터의 마지막 5축 비교 슬라이드에서 정면으로 다룬다고 예고하십시오. -->

---

## 통제는 코드 곳곳이 아니라 이 경로 한 줄에 박혀 있다

<!-- CH 01 · ARCHITECTURE — 요청 한 건이 반드시 순서대로 지나는 8개의 검문소. 단일 FastAPI · Pure ASGI 미들웨어 · LIFO 등록 -->

```text
[Client] → ① OTel(관측) → ② ClientId(앱 식별) → ③ Auth(인증)
              ↓
         ④ ClientAuthZ(권한) → ⑤ Budget(예산) → ⑥ Downgrade(강등)
              ↓
         ⑦ RateLimit(속도) → ⑧ HeaderInjector(응답헤더 정리) → [Router → Provider]
```

- ① OTel: 모든 요청이 예외 없이 여기서 추적 시작  ·  ② ClientId: 요청 헤더로 어느 앱인지 식별
- ③ Auth: VK를 Redis 해시 조회로 검증 — DB는 거의 안 건드려 무효키 폭주에도 안전  ·  ④ ClientAuthZ: 앱별 접근 목록 확인
- ⑤ Budget: 앱·유저·팀 예산 초과 시 429  ·  ⑥ Downgrade: 임계 시 저렴한 모델로 강등  ·  ⑦ RateLimit: 호출 속도 제한  ·  ⑧ HeaderInjector: 우리 헤더만 새로 붙임
- **순서가 곧 계약**: 권한(④)은 인증(③)·앱식별(②)이 채운 정보를 읽어 반드시 뒤에 옴 — Starlette은 LIFO라 코드 등록 순서의 역순으로 실행

근거: `gateway-proxy/src/app/main.py:373-386`

<!-- 핵심은 '통제가 코드 여기저기에 흩어진 게 아니라, 요청이 지나는 경로에 8개의 검문소로 순서대로 박혀 있다'입니다. 각 검문소가 앞 검문소가 채워 놓은 정보를 읽기 때문에 순서 자체가 곧 정합성입니다 — 권한 확인(ClientAuthZ)은 인증(Auth)과 앱 식별(ClientId)이 채운 정보를 읽으므로 반드시 그 뒤에 와야 하고(main.py:375 주석), 모델 강등(Downgrade)은 예산(Budget)이 계산한 임계치를 읽으므로 예산 뒤에 옵니다(main.py:376). 반드시 짚을 기술 포인트: Starlette의 add_middleware는 나중에 등록한 것이 먼저 실행되는(LIFO) 방식이라, 코드에 적은 등록 순서는 실제 실행 순서의 역순입니다(코드에선 HeaderInjector부터 등록). 또 하나 자랑할 지점은 표준 미들웨어(BaseHTTPMiddleware)가 아니라 저수준(Pure ASGI)으로 짰다는 것 — 표준 방식은 답변을 한 글자씩 흘려보내는 스트리밍(SSE)과 궁합이 나빠, 순수 방식으로 구현해 스트리밍 중에도 통제가 유지됩니다. 이 구조 덕에 '웹서치 OFF면 검색 안 함, 모델이 비활성이면 404, 예산 초과면 429'가 실제 운영에서 끝단까지 강제됨을 증명했습니다. 예상 질문 '검문소 순서가 바뀌면?'에는, 각 단계가 앞 단계 정보에 의존하므로 순서가 곧 계약이며 근거를 코드 주석에 남겼다고 답하십시오. -->

---

<!-- _class: comparison -->

<style scoped>
h2 { font-size: 34px; }
ul { gap: 28px; }
strong { color: var(--color-accent); }
li { font-size: 15px; line-height: 1.4; margin-bottom: 3px; } h2 { font-size: 30px; } section.comparison { padding-top: 100px; }
</style>

## 오픈소스(LiteLLM)가 구조적으로 못 하는 5가지

<!-- CH 01 · COMPARISON — 받은 요청을 검사 없이 통과시키는 중계기(passthrough)의 구조적 한계 vs 허용된 것만 골라 새로 만들어 보내는 게이트웨이(re-origination) -->

- **범용 OSS (LiteLLM 류 · passthrough)**
  - 헤더·키: 받은 헤더를 모델 쪽으로 그대로 흘림(relay) — 남의 키가 새는 표면
  - 무효키 폭주: 요청마다 사용기록을 DB에 쓰다가 DB 폭발
  - 여러 계정: 설정 파일 고쳐 재배포해야 반영
  - 웹서치: 클라가 직접 검색(게이트웨이 우회 — 집계·안전장치 불가)
  - 장애: 연결 고갈되면 재시작해야만 복구

- **우리 게이트웨이 (re-origination)**
  - 본문·모델요청·응답헤더 3곳 모두 허용 목록으로 새로 조립 — 몰래 낀 것이 뒤로 못 넘어감
  - Redis 조회 실패→즉시 401, DB 연결은 아예 안 잡음(DB 부하 0)
  - routing_profiles DB 한 줄만 수정 — 재배포 없이 즉시 롤백(3계정)
  - 서버에서 검색 대신 수행·집계 — 90초당 5회로 폭주 억제
  - 상태 점검 전용 신호 + 연결 부족 시 즉시 실패로 스스로 우회

근거: `messages.py:79,164` · `auth_service.py:49,71`

<!-- 이 슬라이드가 챕터의 클라이맥스이자 '왜 직접 만들었나'의 정면 답입니다. LiteLLM 같은 오픈소스는 빠르게 시작하기엔 좋지만, 근본이 받은 요청을 검사 없이 그대로 통과시키는 중계기(passthrough 프록시)라, 우리 요구(대기업 보안·다계정·정밀 과금·통제)에는 구조적으로 안 맞습니다. 축1이 가장 강력합니다: 우리는 요청 본문을 허용 목록(_BEDROCK_ALLOWED_FIELDS)으로 다시 조립하고(messages.py:164), 모델로 나가는 인증 헤더는 broker bearer나 SigV4 서명으로 그 자리에서 새로 발급하며, 응답 헤더는 우리 것(x-llm-gateway-*)만 내보냅니다. 그래서 '남의 키가 새는 사고(header leak)'는 우리가 방어를 빠뜨린 게 아니라, 애초에 새어나갈 표면 자체가 없는 설계입니다. 축2는 타사(카카오) LiteLLM 장애와의 대조입니다 — 그쪽은 무효키 폭주가 요청마다 사용기록 DB 쓰기로 이어져 연결이 터졌지만, 우리는 형식 오류면 Redis도 안 보고, 미등록 출입증이면 Redis 2회 조회 실패로 401 종료하며, DB 연결(세션)은 필요할 때만 잠깐 여는 방식이라 아예 잡지 않습니다. 축3은 운영 민첩성 — 경로 규칙의 account_role_arn을 NULL로 바꾸고 Redis만 비우면 재배포 없이 123 계정으로 폴백됩니다. 축5의 '자가복구'는 조건부(특정 상황 한정)로 정직하게 하향 정정했고 순수 연결 고갈은 미검증이라, 상태 점검 전용 신호(readiness probe)를 새로 넣었다고 솔직히 말하면 신뢰도가 올라갑니다. 예상 질문 '오픈소스를 커스터마이징하면 안 됐나?'에는, 헤더 재구성·다계정 권한 위임·서버사이드 웹서치 통합은 중계기 위에 패치로 얹는 게 아니라 아키텍처 자체가 달라야 한다고 답하십시오. -->

---

<!-- _class: divider -->

## 02. 아키텍처 전체

단일 FastAPI · 8단 Pure ASGI 미들웨어 파이프라인 + Provider 추상화 — 요청 1건이 8개 검문소를 지난 뒤 하나의 공통 어댑터로 재발신된다

<!--
이 챕터는 게이트웨이의 뼈대입니다. 한 문장으로 하면 "사내 LLM 요청 한 건은 반드시 8개의 검문소를 정해진 순서대로 통과한 뒤, 마지막에 하나의 공통 어댑터(ProviderAdapter)를 거쳐 4개 백엔드 어디로든 같은 방식으로 나간다"입니다. claude-code든 codex든 cowork든, Bedrock을 직접 부르든 Mantle(외부 모델을 대신 호출해주는 백엔드)을 거치든 전부 같은 파이프라인을 지나므로, 어떤 클라이언트에서만 통제가 빠지는 구멍이 생기지 않습니다. 여기서 뿌리는 두 씨앗 — SSE 스트리밍을 깨지 않으려 미들웨어를 손으로 짠 것, 요청 내내 DB 연결을 붙잡지 않은 것 — 이 뒤 안전성 챕터의 토대입니다. 짧은 전환 노트로 넘어가십시오.
-->

---

## 요청 한 건은 반드시 8개 검문소를 순서대로 지난다 (①~④)

단일 FastAPI · Pure ASGI 미들웨어 · 통제는 코드 곳곳이 아니라 이 경로 한 줄에 박혀 있다

```
[클라 요청]
   ↓
① OTel(관측) ── 요청마다 고유 ID 발급·추적·지연/에러 기록, 예외 없이 모든 요청이 여기부터 시작
   ↓
② ClientId(앱 식별) ── 요청 지문으로 claude-code/codex/cowork 분류 (실패해도 튕기지 않고 other로 계속)
   ↓
③ Auth(인증) ── VK(게이트웨이 발급 임시 출입증)를 Redis 먼저 확인, 필요할 때만 DB를 잠깐 열었다 닫음 → 무효키는 DB를 아예 안 건드림
   ↓
④ ClientAuthZ(권한) ── 이 사용자에게 허용된 앱 목록 밖이면 403 차단
   ↓  (계속 →)
```

<!--
게이트웨이의 심장인 요청 흐름 전반부입니다. 핵심 메시지는 "요청 한 건은 반드시 8개 검문소를 정해진 순서대로 통과하며, 그 순서가 코드에 강제로 박혀 있다"입니다. VK는 Virtual Key, 게이트웨이가 사용자에게 발급하는 임시 출입증이라고 첫 등장에서 풀어 주십시오. OTel은 OpenTelemetry, 모든 요청을 관측·추적하는 표준입니다. 순서가 왜 중요한지 미리 못 박으십시오 — 관측(OTel)과 앱 식별(ClientId)은 인증에 실패한 요청도 기록·분류해야 하니 앞에 두고, 권한 검사(ClientAuthZ)는 인증(Auth)과 앱 식별(ClientId)이 채운 정보를 읽으므로 반드시 그 뒤여야 합니다(main.py:375 주석). 인증이 Redis를 먼저 보고 DB는 잠깐만 여는 설계는 뒤 챕터의 무효키 폭주 방어와 직결됩니다. 근거: gateway-proxy/src/app/main.py:373-387. 다음 슬라이드에서 나머지 4개 검문소로 이어집니다.
-->

---

## …나머지 4개 검문소 뒤 Router → ProviderAdapter로 재발신 (⑤~⑧)

예산·강등·속도제한·헤더정리 후 하나의 공통 어댑터로 나간다

```
   ↓  (④ ClientAuthZ 에서 이어짐)
⑤ Budget(예산) ── 개인/팀/앱 예산을 넘으면 429로 즉시 차단 (과금 폭주 방지)
   ↓
⑥ Downgrade(등급 강등) ── 예산 임계·팀 정책 시 요청 모델을 더 저렴한 모델로 자동 바꿔치기
   ↓
⑦ RateLimit(속도 제한) ── 개인·팀·전체 3단계, Redis가 죽어도 각 서버 메모리로 계속 동작
   ↓
⑧ HeaderInjector(응답 헤더 정리) ── 우리 표준 헤더(요청ID·예산·속도 잔량)만 새로 붙임 (계정 폴백 시 그 사실도 표시)
   ↓
[Router] → [ProviderAdapter] → [Bedrock / Mantle 등 4백엔드]
```

<!--
후반부 4개 검문소와 발신부입니다. 반드시 짚을 기술 포인트: Starlette의 add_middleware는 나중에 등록한 것부터 실행하는(LIFO) 방식이라, 코드에 적은 등록 순서는 실제 실행 순서의 역순입니다 — 코드에선 HeaderInjector부터 등록하지만 실행은 맨 마지막에 등록한 OTel이 가장 바깥에서 제일 먼저, HeaderInjector가 가장 안쪽에서 제일 나중입니다. 순서는 취향이 아니라 계약입니다: 등급 강등(Downgrade)은 예산(Budget)이 계산한 임계치를 읽으므로 예산 뒤에 옵니다(main.py:376). 청중에게 던질 이득은 "무설정 균일 게이팅" — 어떤 앱이든 어떤 백엔드든 같은 8단을 지나니 통제가 한쪽만 새지 않는다는 점입니다. 예상 질문 "검문소 순서가 바뀌면?"에는 각 단계가 앞 단계 정보에 의존하므로 순서가 곧 계약이며 근거를 코드 주석에 남겼다고 답하십시오. 근거: gateway-proxy/src/app/main.py:373-387.
-->

---

## 왜 표준 데코레이터가 아니라 raw ASGI로 손수 짰나

SSE 스트리밍 비호환 회피 · LIFO 조립 · 세션 lazy

- 8개 미들웨어 전부를 저수준(raw ASGI) 방식으로 손수 구현 — FastAPI 기본 데코레이터(BaseHTTPMiddleware) 미사용
- 이유: 기본 데코레이터는 스트리밍 응답과 궁합이 나빠, 답을 한 글자씩 흘리는 SSE(Server-Sent Events, 답변을 실시간으로 흘려보내는 방식)가 500 오류로 죽음
- 증상: 스트림이 끊기며 'No response returned' — 실시간 답변(stream=true)이 통째로 붕괴 (내부적으로 anyio.EndOfStream)
- 조립 방식: 나중에 등록한 것부터 실행되므로 코드 순서와 실행 순서가 역순 (main.py 주석에 명시)

<!--
"왜 FastAPI가 주는 표준 미들웨어를 안 쓰고 손으로 저수준 코드를 짰나"에 답하는 슬라이드 전반부입니다. 근거는 main.py:396-402 주석에 못박혀 있습니다: FastAPI의 표준 데코레이터 미들웨어(BaseHTTPMiddleware)는 스트리밍 응답과 호환되지 않아, 답을 한 글자씩 흘리는 stream=true 요청이 스트림 끝 오류(anyio.EndOfStream)를 만나 'No response returned' 500으로 죽는 알려진 문제가 있습니다. 답변을 실시간으로 흘려주는 SSE가 이 게이트웨이의 핵심 사용자 경험이라 절대 깨지면 안 되므로, 모든 미들웨어를 요청을 밑바닥부터 다루는 저수준(raw ASGI) 클래스로 직접 작성했습니다. 조립 순서는 앞 슬라이드에서 다룬 LIFO를 재확인하는 정도로 짚으십시오. 다음 슬라이드에서 가장 안쪽 미들웨어의 DB 세션 lazy 설계로 이어집니다. 근거: gateway-proxy/src/app/main.py:396-426.
-->

---

## 가장 안쪽 미들웨어는 DB 연결을 미리 열지 않는다

StateInjection — 모델 호출 직전에 도구만 실어주고, 세션은 필요할 때만

- 모델 호출 직전(가장 안쪽 StateInjection)에서 Redis·DB 연결 도구·degradation(인프라 불안정 시 스스로 등급을 낮춰 방어) 관리자·보안 탐지기만 요청에 실어줌
- 단, DB 연결(세션)은 여기서 안 엶 — SSE로 답이 흐르는 수십 초 동안 DB를 붙잡으면 연결 풀이 말라 장애로 번짐
- 붙잡을 경우 DB가 'idle in transaction'(트랜잭션 안에서 노는) 상태로 묶여 연결 풀 고갈 → 전체가 degradation으로 확산
- 대신 실제로 DB가 필요한 순간에만 잠깐 열고 즉시 닫음 — 타사 LiteLLM 커넥션 풀 고갈 장애와 대비되는 구조적 강점의 토대

<!--
Pure ASGI 챕터의 후반부, 가장 안쪽 미들웨어(StateInjection, main.py:403-426) 설명입니다. 이건 모델 호출 직전에서 Redis, DB 연결 도구, degradation(인프라가 불안정하면 스스로 등급을 낮춰 방어하는 열화 상태 관리) 관리자, 보안 탐지기를 요청에 실어주되, DB 연결(세션)은 절대 미리 열지 않습니다. 주석(main.py:419-423)대로 요청 내내 세션을 붙잡으면 SSE가 수십 초 흐르는 동안 DB가 'idle in transaction' 상태로 묶여 연결 풀이 마르고, 결국 전체가 열화로 번집니다. 대신 실제로 DB가 필요한 순간에만 잠깐 열고 즉시 닫습니다. 이 설계가 뒤(6장)에서 다룰 타사 LiteLLM 커넥션 풀 고갈 장애와 대비되는 구조적 강점의 토대라는 점을 반드시 연결하십시오. 근거: gateway-proxy/src/app/main.py:396-426.
-->

---

## ProviderAdapter ABC 하나로 4개 백엔드를 균일화한다

<style scoped>
h2 { font-size: 40px; }
</style>

공통 어댑터 규격 · invoke / invoke_stream 두 메서드 · 라우터는 뒤가 뭔지 몰라도 됨

- 공통 어댑터 규격(ProviderAdapter ABC): 백엔드가 뭐든 '한 번 호출(invoke)/스트리밍 호출(invoke_stream)' 두 메서드로 통일 — 라우터는 뒤가 뭔지 몰라도 됨
- 반환값도 (상태, 본문 또는 스트림, 헤더, 사용량) 형태로 통일 → 어떤 벤더든 같은 방식으로 다룸
- 이 규격으로 4개 백엔드 등록: ① Bedrock 직접호출(claude-code) · ② 오픈모델(httpx) · ③ Mantle-Bedrock(cowork) · ④ Mantle-OpenAI(codex)
- 새 백엔드 추가 = 공통 규격만 구현해 등록 한 줄이면 8단 파이프라인은 그대로 재사용

```
[Router]
   ↓  (뒤가 뭔지 몰라도 됨 — invoke / invoke_stream 만 안다)
[ProviderAdapter (ABC + Registry)]
   ├─ ① BedrockAdapter      → Bedrock 직접호출 (claude-code)
   ├─ ② OpenModelAdapter    → 오픈모델 (httpx)
   ├─ ③ MantleBedrockAdapter → Mantle-Bedrock, Anthropic Messages (cowork)
   └─ ④ MantleOpenAIAdapter  → Mantle-OpenAI, OpenAI Responses (codex)
```

<!--
파이프라인 끝단, 즉 "게이트웨이가 실제 모델을 어떻게 호출하나"의 추상화입니다. 핵심 추상화는 base.py의 공통 어댑터 규격(ProviderAdapter ABC = 추상 기반 클래스)으로, 어느 백엔드든 딱 두 가지 — 한 번 호출(invoke)과 스트리밍 호출(invoke_stream) — 만 구현하도록 강제합니다. 반환값도 (상태, 본문 또는 스트림, 헤더, 사용량) 형태로 통일돼 있어서 라우터는 뒤가 어떤 벤더든 신경 쓰지 않습니다. 네 종류 백엔드를 등록합니다(main.py:151-192): claude-code용 Bedrock 직접호출, 오픈모델(httpx), cowork용 Mantle-Bedrock(Anthropic 규격), codex용 Mantle-OpenAI(OpenAI Responses 규격). 예상 질문 "새 백엔드 추가는?"에는 공통 어댑터 규격만 구현해 등록 한 줄이면 8단 파이프라인은 그대로 재사용된다고 답하십시오. 다음 슬라이드에서 이 어댑터의 투명 폴백과 데이터플레인으로 이어집니다. 근거: gateway-proxy/src/app/providers/base.py:12-32; gateway-proxy/src/app/main.py:151-192.
-->

---

## 계정 간 투명 폴백 + Aurora·Redis·AgentCore 데이터플레인

re-origination으로 재발신 · claude-code는 절대 안 죽는다

- Bedrock 호출: 333 계정 권한을 잠깐 빌려(cross-account) 부르고, 실패하면 자기 계정 123로 조용히 되돌아감 → claude-code는 절대 안 죽음 (라이브 실증: AccessDenied→123 200)
- re-origination(요청을 새로 만들어 발신): AWS 정식 서명(SigV4)이나 Mantle 브로커 토큰으로 새로 인증 — 사용자 VK는 뒤로 절대 안 넘어감
- 데이터: Aurora PostgreSQL(인증·모델·예산·사용량 원본 DB) · Redis(라우팅·모델·예산 빠른 경로 캐시)
- AgentCore Runtime(BI·퀵챗 에이전트 실행) + AgentCore Gateway(웹검색 도구 제공, MCP 규격)

```
[BedrockAdapter._get_client]
   ├─ 정상:  123 → AssumeRole(333) → Bedrock 호출
   └─ 실패:  AccessDenied → 예외 catch → 123 자기 계정으로 조용히 폴백 → 200
──────────────────────────────────────────────
데이터플레인:  [Aurora=원본 DB] · [Redis=캐시] · [AgentCore Runtime=실행 / Gateway=웹검색]
```

<!--
어댑터의 킬러 대목, 계정 간 투명 폴백과 데이터플레인입니다. 가장 인상적인 대목은 계정 간 투명 폴백입니다 — claude-code는 다른 계정(333)의 권한을 잠깐 위임받아(cross-account, 권한을 잠깐 빌리는 것) Bedrock을 부르는데, 그 위임이 실패하면 BedrockAdapter._get_client가 예외를 잡아 자기 계정(123)으로 조용히 되돌아가 계속 응답합니다. 데브로그의 "라이브 실증: AccessDenied→123 200"이 바로 이것이고, 그래서 claude-code는 절대 죽지 않습니다. re-origination(허용 항목만 골라 요청을 새로 만들어 발신)은 뒤로 나갈 때 SigV4(AWS 정식 서명) 또는 Mantle 브로커 토큰으로 새 자격을 만들어 보내므로, 사용자 VK(임시 출입증)는 뒤로 절대 안 넘어갑니다 — 남의 키가 새어나갈 표면 자체가 없다는 5장 안전성 챕터와 연결하십시오. 데이터는 세 축입니다: Aurora PostgreSQL(모든 값의 원본), Redis(빠른 경로 캐시), AgentCore(Runtime=BI 퀵챗 에이전트 실행, Gateway=웹검색 도구를 MCP 규격으로 제공). 근거: gateway-proxy/src/app/providers/base.py:12-32; gateway-proxy/src/app/main.py:151-192.
-->

---

<!-- _class: divider -->

## 3. 키 발급 구조/원리 + IdP 유연성

발급 즉시 원문 소멸 · 인증 공급자(IdP)는 코드가 아니라 설정값(env)으로 교체

<!--
전환 노트: 앞 챕터에서 "요청을 새로 만들어 보낸다(re-origination)"는 축을 세웠다면, 이 챕터는 그 출발점인 "출입증을 어떻게 발급하고 검증하는가"를 다룹니다. 두 축을 예고하십시오 — 첫째, VK(Virtual Key, 사용자에게 발급하는 임시 출입증)는 발급 순간 원문이 사라져 새어도 원본 복원이 불가능한 유출-표면-0 설계. 둘째, 로그인 시스템(IdP)은 특정 벤더에 묶이지 않고 OIDC 표준 위에서 코드 0줄로 교체됩니다. 근거는 admin-api/src/app/services/key_service.py 와 core/oidc_verifier.py 로, 뒤 슬라이드에서 file:line 으로 하나씩 확정합니다.
-->

---

<style scoped>
h2 { font-size: 30px; }
</style>

## 새어도 원본을 복원할 수 없는 출입증

<!-- CH 03 · DEEP DIVE — Virtual Key(VK, 임시 출입증) 발급 -->

- 포맷: `vk-` + `secrets.token_hex(32)` → 67자 추측 불가 토큰, 화면 표시는 앞 11자만
- DB(auth.virtual_keys)엔 AES-256-GCM 암호문만 저장 — 평문 VK를 담는 칸 자체가 없음
- Redis엔 sha256(VK) 해시만 저장 → 원문 없이도 조회 성립(새어도 역산 불가)
- 발급은 SQL 한 번에: 기존 키 만료 + 새 키 발급을 단일 트랜잭션(CTE)으로 처리 → 중복 활성 키 0
- 암호문 = `v1:` + IV(12) + ciphertext + GCM tag(16) — 버전 표식(prefix)으로 여러 키 세대를 복호

`근거: admin-api/src/app/services/key_service.py:63-103`

<!--
핵심 메시지는 "유출 표면 자체를 없앴다"입니다. 발급 순간 raw_key = vk- + secrets.token_hex(32) 로 암호학적 난수 32바이트를 만들고 즉시 두 갈래로 변형합니다 — DB에는 AES-256-GCM 암호문을, Redis에는 sha256 해시를 씁니다. 어디에도 평문 VK(임시 출입증 원문)가 저장되지 않으며, 스키마에 평문 칸조차 없습니다. 왜 중요한가: DB가 통째로 유출돼도 원문 복원엔 별도 KMS 키가 필요하고, Redis가 유출돼도 해시라 역산이 불가능합니다 — 즉 새어도 실제 접근에는 못 씁니다. 발급 원자성도 강조하십시오 — '이전 키 무효화 + 신규 발급'을 CTE(단일 SQL)로 한 번에 처리해, 한 사용자가 유효한 키 두 개를 동시에 갖는 위험한 순간이 없습니다. 암호문의 v1: 버전 표식은 뒤 슬라이드의 키 회전을 위한 씨앗입니다. 예상 질문 '앞 11자는 왜 노출하나?' → 감사·목록 화면에서 어느 키인지 식별하는 용도이며, 32바이트 난수의 극히 일부라 이걸로 키를 추측할 수 없습니다.
-->

---

## TTL 1시간은 짧지 않다 — 체감 0의 안전 마진

<!-- 앞 슬라이드에서 이어지는 VK 수명·세션 연동 -->

- OIDC(표준 로그인)로 발급한 VK는 수명 기본 1시간 — 도용 가능 시간을 구조적으로 최소화
- SSO 세션보다 VK 수명이 길면, VK 만료 시점을 세션 만료 시점으로 자동 축소 정렬
- 짧은 수명이 곧 보안 이점(다음 슬라이드의 silent refresh가 사용자 체감을 0으로 만듦)
- 수명 1시간 덕에 로그인 시스템의 그룹(권한) 변경이 재배포 없이 약 2시간 내 자동 반영

`근거: admin-api/src/app/services/key_service.py:63-103`

<!--
이 슬라이드는 앞 발급 슬라이드에서 6불릿이던 내용을 쪼갠 후반부입니다 — TTL(수명) 정책만 따로 강조합니다. 'TTL 1시간이면 너무 짧지 않나?'라는 반론을 여기서 정면으로 받으십시오. 짧은 수명은 약점이 아니라 도용 가능 시간을 최소화하는 보안 이점이고, 다음 슬라이드의 무중단 자동 갱신(silent refresh)이 사용자 체감을 0으로 만들기 때문에 짧게 가져갈 수 있습니다. 또 SSO 세션보다 VK가 더 길게 살면 안 되므로, 세션 만료 시점으로 VK 수명을 맞춰 내립니다. 수명 1시간이라는 설계가 로그인 시스템의 그룹·권한 변경을 약 2시간 안에 재배포 없이 반영시키는 부수 효과도 함께 언급하십시오 — 이는 4챕터의 거버넌스 즉시성과도 연결됩니다.
-->

---

## 정상 경로는 캐시로 끝, 무효 키는 DB를 못 건드린다

<!-- CH 03 · DEEP DIVE — 인증 검증(Redis 우선) -->

- 1단계: VK를 sha256 해시로 캐시 조회 — 있으면 DB 왕복 없이 즉시 통과
- 2단계: 캐시 미스면 해시로 user_id만 조회, 그것도 없으면 곧장 401(권한 없음)
- 형식이 깨진 헤더는 토큰 추출 단계에서 바로 튕겨 Redis조차 안 감(Redis 0회)
- 무효 키 폭주 = 캐시 미스 후 401로 끝 → DB 연결을 아예 안 잡음(연결 풀 고갈 없음)
- 답변 스트리밍(SSE, 답을 한 글자씩 실시간으로 흘리는 방식) 중에도 DB 연결을 붙들지 않음

`근거: gateway-proxy/src/app/services/auth_service.py:45-76`

<!--
설계 의도는 '정상 경로에서 DB를 최대한 안 만난다'입니다. 인증 순서를 따라가십시오: ① 받은 VK(임시 출입증)를 sha256으로 해시해 Redis에서 완성된 인증정보를 통째로 조회, 있으면 DB 왕복 없이 통과. ② 없으면 해시로 user_id만 조회하고 그것도 없으면 곧바로 401로 끝냅니다. 즉 등록 안 된 무효 키는 캐시 조회 두 번으로 끝나고 DB 연결을 아예 잡지 않습니다. 형식이 깨진 헤더는 그 이전 토큰 추출 단계에서 튕겨 Redis조차 안 갑니다. 반드시 타사 프록시 대비로 말하십시오: 다른 곳(카카오 사례)은 요청마다 사용량 로그를 DB에 기록해서 잘못된 키가 몰리면 곧 DB 폭발로 이어졌는데, 우리는 인증 조회 시 DB 연결을 짧게 열고 즉시 반납하며, SSE(답을 한 글자씩 흘리는 실시간 스트리밍)가 30초 이어져도 DB 연결을 인질로 잡지 않습니다. 무효 키가 폭주해도 DB 부하가 구조적으로 0인 이유입니다. 이 대목은 6챕터(무효키 폭주 방어)의 복선입니다.
-->

---

## 캐시에 있어도 계정 상태를 재확인 — 퇴사자 즉시 차단

<!-- 인증 검증 후반 — fail-closed 안전장치 -->

- 캐시 히트에도 계정 활성 여부(is_active)를 재확인 → 정지된 계정은 캐시 유효시간 내에도 즉시 차단
- '퇴사자 차단이 늦다'는 반론 차단 — 캐시 TTL을 기다리지 않고 그 즉시 끊김
- fail-closed(실패 시 막는 쪽): 사용자 허용 모델 조회가 DB 오류로 실패하면 인증을 통과시키지 않음
- 국가핵심기술 보호 원칙 — 애매하면 여는 게 아니라 막는다

`근거: gateway-proxy/src/app/services/auth_service.py:45-76`

<!--
이 슬라이드는 앞 인증 검증 슬라이드의 후반 두 불릿을 떼어 안전장치 관점으로 묶은 것입니다. 두 가지를 강조하십시오. 첫째, 안전 디테일: 캐시(Redis)에 값이 있어도 DB로 계정 활성 여부(is_active)를 재확인해, 캐시 유효시간 안에 정지된 계정을 즉시 끊습니다 — 이는 '퇴사자·정지 계정 차단이 캐시 TTL만큼 늦어지지 않느냐'는 예상 반론을 정면으로 막는 설계입니다. 둘째, fail-closed(실패 시 막는 쪽): 사용자 허용 모델 조회가 DB 오류로 실패하면 국가핵심기술 보호 원칙에 따라 인증을 통과시키지 않고 막습니다. 애매하면 여는 게 아니라 막는다는 원칙을 한 줄로 남기면 보안 심사관에게 강한 인상을 줍니다. 근거는 auth_service.py:45-76 로, 앞 슬라이드와 같은 파일 범위 안에 함께 배선돼 있습니다.
-->

---

<!-- _class: comparison -->

<style scoped>
table { font-size: 17px; }
</style>

## IdP 교체는 코드가 아니라 설정값(env)으로 — 0줄 수정

<!-- CH 03 · COMPARISON — 표준 OIDC 위, 벤더 차이는 전부 env 흡수 -->

| 검증 항목 | Cognito (현재) | 표준 OIDC (Okta/Entra/Keycloak) |
|---|---|---|
| Issuer | `OIDC_ISSUER_URL=cognito-idp...` | `=okta.com/oauth2` 등 issuer만 교체 |
| groups claim | `cognito:groups` | `OIDC_GROUPS_CLAIM` env로 흡수 |
| audience | 비움(access token은 aud 없음) | `aud` 명시 → env 하나로 검증 ON |
| 사용자 식별 | `sub` 식별·자동 프로비저닝 | 동일 (provider 컬럼으로 다중 IdP 분리) |
| 서명키(JWKS) | discovery로 자동 발견 | 동일 — `kid` 매칭 rotation 자동 |

- RS256/384/512만 허용 — HS256·`alg=none`(서명 없음) 공격은 차단

`근거: admin-api/src/app/core/oidc_verifier.py:40-126`

<!--
이 슬라이드가 챕터의 하이라이트입니다. 핵심 주장: '로그인 시스템(IdP, 인증 공급자)을 바꾸는 데 코드를 한 줄도 안 고친다.' 근거는 우리 검증기가 특정 벤더가 아니라 OIDC(업계 표준 로그인 규격)에 붙어 있다는 것으로, 코드 주석에 Keycloak/Cognito/Identity Center/Okta/Azure AD 모두 대상이라 명시돼 있습니다. 검증기는 설정값에 적힌 발급자 주소(issuer)에서 규격 문서를 자동으로 찾아와(discovery) 서명 검증용 공개키(JWKS)를 발견하고, 토큰의 키 식별자(kid)로 맞춰 서명을 검증합니다. 벤더 차이는 전부 env로 흡수됩니다: 발급자 주소·대상(audience)·그룹 필드 이름·사용자 ID 필드·공급자 이름이 각각 env 필드로 빠져 있습니다. 특히 대상(audience): Cognito 액세스 토큰은 표준 aud가 없어 비워두면 검증을 건너뛰고, Okta/Entra/Keycloak은 이 값을 채우면 대상 검증이 켜집니다. 여러 IdP 동시 사용도 됩니다 — 사용자 테이블의 provider 칸에 oidc:cognito / oidc:keycloak 로 구분합니다. 예상 질문 '서명 없음(alg=none) 공격은?' → RS256/384/512만 허용하고 HS256·none은 차단합니다. 근거는 oidc_verifier.py:40-126.
-->

---

## 짧은 수명 + 3경로 폐기 — 도난 영향을 최소화

<!-- CH 03 · DEEP DIVE — 회전형 키 · silent refresh · 3경로 폐기 -->

- 무중단 자동 갱신(silent refresh): CLI가 만료 5분 전 갱신 토큰으로 새 VK를 조용히 교환(재로그인 0, 체감 0)
- 폐기① 단건 폐기 — 키 하나를 무효 처리 + 캐시·역인덱스에서 제거
- 폐기② 팀 일괄 폐기 — 팀 전원의 유효 키를 한 번에 무효화(퇴사·보안사고 즉시 대응)
- 폐기③ 암호화 키 회전 — 버전 표식(`v1:`)+구키 보관으로 서비스 중단 없이 재암호화
- 관심사 분리: 로그인 토큰 검증은 읽기 전용, 사용자·팀 생성은 별도 교환 단계로 격리

`근거: gateway-cli api_key_helper/main.py:223-273(만료 5분전 refresh→VK 교환)`

<!--
'짧은 수명 + 다중 무효화'로 키 도난의 영향을 최소화하는 운영 모델입니다. 무중단 자동 갱신(silent refresh): OIDC로 발급한 VK(임시 출입증)는 기본 수명이 1시간으로 매우 짧지만, CLI 도구가 만료 5분 전을 감지해 갱신 토큰으로 새 로그인 토큰을 받아 관리 API에 재요청, 새 VK를 조용히 캐시합니다 — 사용자는 재로그인 없이 그대로 씁니다. 근거는 gateway-cli api_key_helper/main.py:223-273. 폐기 3경로를 또렷이 하십시오: ① 단건 폐기는 키 하나를 무효로 바꾸고 암호문을 복호→해시로 캐시 키를 무효화하며 팀 역인덱스에서도 뺍니다. ② 팀 일괄 폐기는 팀 멤버 전원의 유효 키를 한 번에 무효화 — 퇴사나 보안사고 시 1시간 자연 만료를 기다리지 않고 즉시 차단하고, 다음 호출부터 401 후 재발급으로 자동 복구됩니다. ③ 암호화 키 회전은 버전 표식(v1:)과 구키 보관 맵으로 옛 키 복호는 유지한 채 새 데이터는 새 키로 암호화하는 무중단 방식입니다. 관심사 분리도 강조: 로그인 토큰 검증기는 읽기 전용(검증만)이고, 사용자·팀 생성은 별도 교환 단계가 담당해 인증 신뢰와 계정 생성 부작용이 나뉩니다. 예상 질문 '회전 중 다운타임은?' → 옛 키가 보관 맵에 남아 진행 중인 요청도 복호 가능하므로 무중단입니다.
-->

---

<!-- _class: divider -->

## 04. 3-client 통제·거버넌스

하나의 게이트웨이, 세 계정, 다섯 레버 — 겉은 단일 관문, 속은 세 개의 AWS 계정

<!--
챕터 전환 노트입니다. 이 챕터는 게이트웨이의 거버넌스 가치를 보여줍니다. 겉으로는 하나의 접속 주소지만 속으로는 세 개의 완전히 다른 AWS 계정으로 트래픽이 갈라지고, 그 갈래는 routing_profiles라는 DB 한 줄로 결정됩니다. 그리고 다섯 개의 거버넌스 레버 — 누가·무슨 모델을·얼마까지·검색 되나·얼마나 빨리 — 가 스위치를 켰을 때 실제로 막히는가까지 라이브로 검증됐다는 것이 이 챕터의 결론입니다. 비즈니스로 옮기면 신규 클라이언트·모델·예산 정책을 코드 배포 없이 DB나 관리 화면에서 즉시 통제할 수 있다는 뜻입니다.
-->

---

## 세 계정으로 갈라져도 통제는 하나로 흡수된다

- 3개 클라이언트(claude-code·codex·cowork)가 각기 다른 AWS 계정·리전·모델·API 방언 사용
- 하지만 사내 모든 LLM 요청이 반드시 지나는 단일 관문(control plane, 통제 평면)이 이를 하나로 흡수
- 라우팅은 DB 한 줄 — routing_profiles(어느 클라이언트를 어느 계정·모델로 보낼지 정하는 규칙 행)
- 그 한 줄만 바꾸면 재배포 없이 즉시 경로 변경·롤백
- 5개 거버넌스 레버가 "스위치를 켜면 실제로 막히는가"까지 라이브 검증됨

<!--
챕터 오프닝 슬라이드입니다. 핵심은 겉과 속의 대비입니다 — 사용자에게는 하나의 접속 주소지만, 내부에서는 세 개의 완전히 다른 AWS 계정으로 트래픽이 갈라집니다. 그 갈래를 결정하는 것이 routing_profiles라는 DB 한 줄이고, 이 한 줄만 바꾸면 재배포 없이 즉시 롤백이 됩니다. 다섯 레버는 다음 슬라이드들에서 하나씩 풀어줄 예고이고, 여기서는 "코드 배포 없이 DB나 관리 화면에서 즉시 통제"라는 비즈니스 메시지를 각인시키십시오. 근거는 slide 16 note 및 messages.py:243-267 계열입니다.
-->

---

## routing_profiles 한 줄이 세 계정으로 요청을 갈라놓는다

<style scoped>
h2 { font-size: 34px; }
</style>

```
[클라 요청] → [ALB] → [gateway-proxy · 123 EKS Fargate · 서울]
                                │
                                ▼
              [routing_profiles(client) 조회]
              → backend · account_role_arn · region
                                │
        ┌───────────────────────┼────────────────────────┐
        ▼                       ▼                        ▼
  claude-code               codex                     cowork
  333 빌려쓰기              123 자기계정               222 빌려쓰기
  (cross-account            (IRSA, role_arn NULL)     (AssumeRole)
   AssumeRole,              → Mantle GPT-5.5          → Mantle Opus 4.8
   ExternalId)               us-east-2 Responses       도쿄 Messages
  → Bedrock 직접호출
        │
        ▼
  333 실패 시 → 123 자기계정으로 조용히 되돌아감
  (투명 폴백: 사용자 못 느끼고 서비스 안 죽음)
```

<!--
핵심 메시지는 겉으로는 하나의 접속 주소지만 속으로는 세 개의 완전히 다른 AWS 계정으로 트래픽이 갈라진다는 것입니다. claude-code는 자기 계정이 아닌 333 계정의 모델을 빌려 씁니다 — cross-account AssumeRole(잠깐 권한을 위임받는 방식, ExternalId=claude-code-bedrock으로 상대가 우릴 확인)로 들어가 Bedrock을 직접 호출합니다. codex는 셋 중 유일하게 게이트웨이와 같은 123 계정에 그대로 머물며(그래서 남의 계정을 빌릴 필요가 없고 account_role_arn이 비어 있으며, IRSA=코드에 키를 안 심고 파드에 AWS 권한 부여로 인증) Mantle(외부 모델 중개 백엔드)의 GPT-5.5로 오하이오 us-east-2 OpenAI Responses 규격으로 나갑니다. cowork는 222 계정을 빌려 Mantle의 Opus 4.8(도쿄)로 갑니다. 어느 경로로 갈지는 messages.py의 _select_backend와 _xacct 분기가 routing_profiles 한 줄만 보고 결정합니다(messages.py:243-267). 반드시 강조할 점은 claude-code의 투명 폴백입니다 — 333 빌려쓰기가 실패해도 BedrockAdapter._get_client가 예외를 잡아 123로 조용히 되돌리므로 claude-code는 절대 죽지 않으며, 라이브에서 AccessDenied가 나도 123로 200 응답이 나온 것으로 실증됐습니다. 예상 질문 "왜 codex만 자기 계정이냐"에는 codex 백엔드가 게이트웨이 배포 계정 123와 같아 account_role_arn이 NULL이기 때문이라 답하십시오. 리전이 셋 다 다른 이유는 각 모델이 서비스되는 지역(Bedrock 서울·Mantle GPT-5.5 오하이오·Mantle Opus 도쿄)을 따라간 것입니다.
-->

---

<!-- _class: comparison -->

## 통제 매트릭스 — 5개 레버 × 라이브 검증된 효과

- **레버 (control)** — 누가 쓰나: 앱 ACL(허용 클라 목록 allowed_clients) · 무슨 모델: 화이트리스트(user/team allowed_models) · 얼마까지: 앱/유저/팀 예산(HARD_BLOCK) · 검색 켤 수 있나: 웹서치 토글(web_search_enabled) · 얼마나 빨리: 속도 제한 3단(USER→TEAM→GLOBAL)
- **효과 (effect, 라이브 검증 Suite 3)** — 미허용 클라 → **403** permission_error · INACTIVE 모델 → **404** Model is inactive · 한도 초과 → **429** budget_exceeded · 웹서치 OFF → 미검색 / ON → 실검색+URL · RPM/TPM 초과 → **429** *_rpm_exceeded

<!--
거버넌스의 심장입니다. 다섯 개의 독립된 레버를 왼쪽에, 각 레버가 실제로 내는 효과를 오른쪽에 놓고, "스위치를 켜면 정말 그렇게 막히는가"까지 요청부터 응답까지 라이브로 검증됐다(Suite 3)는 점을 강조합니다. 다섯 레버를 청중 언어로 읽으십시오 — 누가 쓸 수 있나(앱 ACL), 무슨 모델(모델 화이트리스트), 얼마까지(예산), 검색 켤 수 있나(웹서치 토글), 얼마나 빨리(속도 제한). 앱 ACL은 client_authz.py:36-49에서 허용 목록에 없는 클라이언트를 403으로 막습니다. 라이브 검증에서 웹서치를 끄면 미검색, 없는 모델은 404, 예산 초과는 429가 전부 실제로 관찰됐습니다. 예상 질문 "왜 어떤 건 403이고 어떤 건 404/429냐"에는 각각 권한 없음(403)·리소스 상태(404)·한도 초과(429)라는 HTTP 표준 의미를 정확히 따른 것이라 답하십시오. 다음 슬라이드에서 이 레버들의 신뢰 모델과 fail-open/fail-closed 차이를 이어서 설명합니다.
-->

---

## 클라 이름표는 못 믿는다 — 정책은 신뢰 가능한 user_id에 건다

- 클라이언트 식별(User-Agent/originator)은 이름표일 뿐, 신뢰의 기준이 아님
- 정책은 신뢰할 수 있는 user_id에 걸림 — 이름표는 허용 범위 밖으로 넘는 것만 막음
- 모델 화이트리스트는 user > team > none 우선순위
- DB 조회 실패 시 fail-closed(안전하게 막는 쪽, 인증 자체 거부) — 앱 ACL의 fail-open(열어두는 쪽)과 대비
- 비즈니스 가치: 신규 클라이언트·모델·예산 정책을 코드 배포 없이 즉시 통제

<!--
앞 매트릭스가 "무엇이 막히나"였다면 이 슬라이드는 "그 통제를 무엇에 걸었나" — 신뢰 모델입니다. 반드시 짚을 것: 클라이언트 식별(User-Agent/originator)은 이름표일 뿐 신뢰의 기준이 아닙니다. 정책은 신뢰할 수 있는 user_id에 걸려 있고, 클라 이름표는 허용된 범위 밖으로 넘어가는 것만 막지 범위 안에서 이름을 사칭하는 것까지 막지는 않습니다. 모델 화이트리스트는 user>team>none 우선순위이며 DB 조회가 실패하면 안전하게 막는 쪽(fail-closed, 인증 자체를 거부)으로 동작해, 앱 ACL의 열어두는 쪽(fail-open)과 대비됩니다. 예산은 user/team/app 3계층에서 한도 넘으면 차단(HARD_BLOCK), 속도 제한은 개인→팀→전체 순으로 먼저 걸리는 곳에서 바로 막는 3단입니다. 마무리 메시지는 비즈니스 가치 — 신규 클라이언트·모델·예산 정책을 코드 배포 없이 즉시 통제할 수 있다는 것입니다. 근거는 slide 18 note 및 client_authz.py:36-49입니다.
-->

---

## 이종 API 방언을 한 게이트웨이가 동시 수용

- claude-code·cowork → Anthropic Messages 규격(/v1/messages)
- codex → OpenAI Responses 규격(/v1/responses), 안 엉키게 별도 _handle_responses 경로
- 받은 본문을 relay(그대로 넘김) 안 함 — 허용 12필드(_BEDROCK_ALLOWED_FIELDS)만 골라 재조립, 몰래 낀 필드는 뒤로 못 넘어감(re-origination)
- Mantle(외부 모델 중개) 방언 변환: anthropic_version 제거 · model 주입 · metadata에 user_id
- 즉시 롤백: account_role_arn 비우면(NULL) 다음 요청부터 123 복귀 + Redis 캐시(5분 유효) 비우기로 끝

<!--
전하고 싶은 것은 세 클라이언트가 서로 다른 API 규격(방언)으로 말하는데도 게이트웨이가 이를 하나로 흡수한다는 점입니다. claude-code와 cowork는 Anthropic Messages 규격(/v1/messages), codex는 완전히 다른 OpenAI Responses 규격(/v1/responses)을 씁니다. 후자는 openai_compat.py의 _handle_responses라는 별도 경로로 처리되어 기존 동작과 엉키지 않습니다. 중요한 건 게이트웨이가 받은 본문을 그대로 뒤로 흘려보내지(relay하지) 않고 허용 목록 12개 필드(_BEDROCK_ALLOWED_FIELDS: messages·max_tokens·system·tools 등)만 골라 요청을 새로 조립한다는 점입니다 — 이것이 re-origination(허용 항목만 골라 요청을 새로 만들어 발신)이며, 몰래 낀 필드나 헤더는 뒤로 넘어갈 표면 자체가 없다는 것이 안전성 챕터와 이어집니다. Mantle 경로는 anthropic_version을 제거하고 실제 모델 ID를 model 필드에 넣고 sso_subject를 metadata에 넣는 방언 변환을 합니다. 운영 관점의 킬러 기능: 라우팅이 코드가 아니라 routing_profiles DB 한 줄이라 재배포 없이 즉시 롤백이 됩니다 — account_role_arn을 비우고(NULL) Redis의 라우팅 캐시 키를 비우면 다음 요청부터 123로 되돌아갑니다. 예상 질문 "규격이 늘면 코드가 폭발하지 않나"에는 백엔드가 ProviderAdapter라는 공통 틀(ABC+Registry)로 추상화돼 있고 규격 변환은 각 어댑터·핸들러에만 국소화돼 있어 한곳만 손대면 된다고 답하십시오. 근거: messages.py:79-92,325-351.
-->

---

<!-- _class: divider -->

## 05. 안전성A — 헤더 탈취

passthrough leak이 아니라 re-origination 게이트웨이 — 본문·업스트림헤더·응답헤더 3곳을 전부 허용 목록으로 새로 만든다

<!--
전환 노트: 앞선 아키텍처·거버넌스 챕터에서 세운 "우리는 relay가 아니라 re-origination(허용 항목만 골라 요청을 새로 만들어 발신) 한다"는 축을, 이 챕터에서 보안으로 증명합니다. 배경을 먼저 프레이밍하십시오 — 과거 LiteLLM류에서 클라이언트 헤더·키를 업스트림·응답으로 그대로 흘리다가(passthrough=받은 걸 검사 없이 뒤로 넘기는 얇은 중계) 한 사용자의 Authorization/api-key가 다른 사용자에게 교차 노출된 header leak 사건이 있었습니다. 이 챕터는 우리 게이트웨이가 그 부류에서 왜 구조적으로 안전한지를 코드로 증명하는 자리입니다. 청중에게 남길 한 줄: 남의 키가 섞여 새어 나갈 통로 자체가 설계상 존재하지 않는다.
-->

---

<!--_class: default-->

## 남의 키가 새어나갈 표면이 애초에 없다

- passthrough leak(받은 클라 헤더를 검사 없이 뒤로 흘리는 얇은 중계)은 LiteLLM류 특유의 위험 — 한 사용자의 키가 다른 사용자에게 섞여 새는 사고
- 우리는 본문·업스트림 헤더·응답 헤더 3곳을 전부 허용 목록(화이트리스트)으로 새로 만든다(re-origination = 요청 재구성 발신)
- 사용자 임시 출입증 VK(Virtual Key, 게이트웨이가 발급하는 임시 키)와 게이트웨이가 실제 클라우드에 쓰는 자격이 완전 분리
- 세 방어는 서로 독립 — 하나가 뚫려도 나머지가 유출을 막는 다층 구조
- 결론: 방어를 나중에 채운 게 아니라, 설계상 새어나갈 표면 자체가 없다

<!--
이 챕터의 프레임 슬라이드입니다. 핵심 메시지를 반복해 못박으십시오: "우리는 받은 걸 그대로 넘기는 프록시가 아니라, 허용된 것만 골라 요청을 새로 만들어 보내는 re-origination 게이트웨이다." 프록시는 받은 걸 그대로 relay(전달)하지만 우리는 본문·업스트림헤더·응답헤더 세 지점 모두에서 통과가 아니라 재구성을 합니다. 세 방어가 서로 독립이라는 점을 강조하면 "하나 뚫리면 끝 아니냐"는 반론을 미리 막습니다. 다음 두 슬라이드에서 이 세 지점을 다이어그램(어디서 재구성하나)과 인증 경계 분리(왜 근본적으로 안전한가)로 각각 증명한다고 예고하십시오. 예상 질문 "그럼 header leak이 우리한테도 날 수 있나?"에는 "방어를 빠뜨린 게 아니라 애초에 흘려보낼 경로가 없는 설계"라고 답하십시오.
-->

---

<!--_class: default-->

<style scoped>
pre { font-size: 14px; line-height: 1.35; padding: 16px; } li { font-size: 18px; }
</style>

## re-origination 3곳: 본문·업스트림·응답을 전부 새로 만든다

클라 요청을 relay하지 않고 화이트리스트로 재구성 — 근거: gateway-proxy/src/app/routers/messages.py:79,164

```text
[클라 요청]                [게이트웨이 = re-origination]              [업스트림 모델]
Authorization: VK ─┐
+ 몰래 낀 헤더/필드  │
                   ▼
        ① 요청 본문 재구성: _BEDROCK_ALLOWED_FIELDS 12필드만 골라
           새로 조립(messages.py:164) → 나머지는 전부 버림
                   │
                   ▼
        ② 업스트림 인증 헤더 신규 발급(mint):
           Mantle=broker 임시 Bearer 1개(+정적 2헤더)  ─────────────▶ [Bedrock / Mantle]
           Bedrock=AWS 정식 서명(SigV4)                              클라 헤더는 여기 안 씀
                   │
                   ▼  ◀───── ③ 응답 헤더: 어댑터가 통째로 버림(빈 값 {} 반환)
        ④ 클라에겐 우리가 만든 X-Budget-*/X-RateLimit-*만
           (+폴백 시 x-llm-gateway-fallback-from) — 업스트림 헤더 전달 0
```

- ⑤ 웹서치 turn 본문도 동일 경로(_build_candidate_body, messages.py:330/343)로 화이트리스트 재구성

<!--
챕터의 뼈대(원본 slide 21)입니다. 다이어그램을 왼쪽 클라 요청 → 게이트웨이 재구성 → 오른쪽 업스트림 순으로 짚으십시오. ① 본문 재구성: 허용 목록(_BEDROCK_ALLOWED_FIELDS)에 든 12개 필드(messages·max_tokens·system·tools 등)만 골라 딕셔너리 컴프리헨션(messages.py:164)으로 새 본문을 만들고, 클라가 몰래 끼운 나머지 필드는 뒤로 못 넘어갑니다. ② 업스트림 인증 헤더를 게이트웨이 자기 자격으로 새로 발급(mint) — Mantle 경로는 broker(외부 모델 중개)가 만든 임시 Bearer, Bedrock은 AWS 정식 서명(SigV4). 클라가 보낸 헤더는 여기 절대 안 씁니다. ③ 모델이 돌려준 업스트림 응답 헤더는 어댑터가 성공·실패 모두 빈 값 {}로 통째로 버려서 뒤쪽 정보가 클라로 안 샙니다. ④ 클라에겐 우리가 만든 X-헤더만 붙이고, 폴백이 일어나면 x-llm-gateway-fallback-from만 추가로 붙입니다. ⑤ 게이트웨이가 대신 검색해 재투입하는 웹서치 turn 본문도 같은 _build_candidate_body가 동일 허용 목록을 쓴다는 점(messages.py:330/343)을 짚어, "왕복 경로는 다른 코드 아니냐"는 질문을 막으십시오. 마지막으로 세 방어가 서로 독립이라 하나 뚫려도 나머지가 유출을 막는 다층 구조임을 언급하면 좋습니다.
-->

---

<!--_class: default-->

## 인증 경계 완전 분리 — 클라 VK와 업스트림 자격은 만나지 않는다

클라 헤더가 업스트림에 도달하는 경로 자체가 없음 — 근거: mantle_adapter.py:37; auth_service.py:49

- 클라 Authorization(VK, 임시 출입증)은 게이트웨이 인증에만 쓰고 뒤쪽 모델(업스트림)엔 절대 전달 안 함
- 업스트림 호출은 게이트웨이가 자기 권한으로 독립 서명 — IRSA(코드에 키를 안 심고 파드에 부여받은 AWS 권한)/broker bearer/SigV4
- VK가 새어도 그건 게이트웨이 출입증일 뿐, 원본 AWS Bedrock 접근권이 아님 — passthrough 프록시와의 결정적 차이
- VK는 원문이 아니라 sha256 해시로만 Redis 조회, 평문은 어디에도 저장 안 함
- 로그엔 user_id/sso_subject(누구인지)만 남김 — Authorization/Bearer 실제 값은 소스 전체 grep 0건으로 확인

<!--
앞 다이어그램이 "어디서 재구성하나"였다면 이 슬라이드(원본 slide 22)는 "왜 그것이 근본적으로 안전한가" — 인증 경계 분리를 설명합니다. 핵심은 클라이언트가 쓰는 자격과 게이트웨이가 업스트림에 쓰는 자격이 서로 다른 체계이고 코드 어디에서도 섞이지 않는다는 것입니다. 클라는 VK로 게이트웨이에 인증하고, 게이트웨이는 자기 자격으로 뒤쪽에 서명합니다 — Mantle 경로는 broker가 발급한 단명 Bearer 3개 헤더(Authorization/anthropic-version/content-type)만 새로 만들고, Bedrock native는 boto3가 SigV4를 하니 "클라 헤더를 그대로 뒤로 흘린다(passthrough)"는 개념 자체가 성립하지 않습니다. 그래서 클라 VK가 유출돼도 그것은 게이트웨이 출입증일 뿐 원본 AWS Bedrock 접근 권한이 전혀 아니라는 점을 강조하십시오. 보강 근거로 VK는 sha256 해시로만 Redis 조회하고 평문 미저장이며, 로그에 Authorization/Bearer/token 실제 값을 남기는 호출이 소스 전체 grep에서 0건이었다는 사실을 제시하십시오. 예상 질문 "업스트림 응답의 request-id 같은 게 클라로 새지 않나"에는 어댑터가 성공·실패 모두 헤더 자리에 빈 값 {}를 반환하고 라우터는 폴백 시 x-llm-gateway-fallback-from만 붙이므로 업스트림 헤더는 물리적으로 전달 경로가 없다고 답하십시오. 정직한 범위 표시로, admin-api의 CORS/쿠키 정책과 서드파티 라이브러리(httpx/botocore)의 DEBUG 헤더 덤프는 이 분석 범위 밖이라 배포 로그레벨에서 별도 확인을 권한다고 덧붙이면 신뢰도가 올라갑니다.
-->

---

<!-- _class: divider -->

## 06. 안전성B — 무효키 폭주가 왜 DB 풀을 못 무너뜨리나

카카오 LiteLLM 장애 대조 · 강점은 코드로 확증, 자가복구는 조건부로 정직하게 하향 정정

<!--
전환 노트: 안전성A가 "키가 새지 않는가"였다면 안전성B는 "잘못된 키가 몰려와도 DB가 안 죽는가"입니다. 이 챕터는 실제 타사(카카오) LiteLLM 장애 사례를 우리 설계와 대조해 왜 구조적으로 안전한지 코드로 증명하면서, 동시에 아직 증명 못 한 것을 정직히 밝히는 자리라고 예고하십시오. 자랑과 정직 정정을 함께 담는 챕터라는 톤을 미리 세우면 마지막 "정직한 판정" 슬라이드의 임팩트가 살아납니다.
-->

---

<!--_class: default-->

<style scoped> h2 { font-size: 46px; color: var(--color-accent); } </style>

## 잘못된 키가 DB 폭발로 가는 통로를 코드로 끊었다

- 타사(카카오) LiteLLM 장애: 무효/빈 키 요청이 쏟아지자 → CPU는 멀쩡한데 응답만 느려짐 → 재배포해야만 복구
- 그 지문("CPU 여유 + 처리량 0 + 재시작으로만 복구")은 교과서적 DB 커넥션 풀 고갈 증상
- 우리는 무효 키가 DB 커넥션을 아예 못 잡는 설계 — DB에 닿기 전 3번 걸러지고 세션은 lazy(필요할 때만 열림)
- 강점은 코드로 확증 — 커넥션이 다 차서 생기는 마비의 자동 복구는 아직 조건부(실부하 미검증)
- 정직하게 선을 긋는 것이 이 챕터의 신뢰 자산

<!--
챕터 개요(원본 slide 23)입니다. 카카오 장애의 지문 — "CPU는 여유 있는데 처리량은 0이고, 재시작해야만 복구" — 은 DB 커넥션 풀(동시에 열 수 있는 DB 연결의 한정된 슬롯)이 다 차서 새 요청이 슬롯을 기다리며 밀리는, 교과서적 고갈 증상임을 먼저 규정하십시오. 우리는 무효 키가 DB에 닿기 전 세 번 걸러지고 DB 세션이 필요할 때만 열리는(lazy) 구조라 무효 키로는 커넥션을 아예 안 잡습니다. 다만 이 강점은 코드로 확증됐지만, 순수하게 커넥션이 다 차서 생기는 마비를 시스템이 스스로 복구하는 부분은 아직 조건부이고 실부하로 미검증이라는 점을 마지막 슬라이드에서 정직히 정정한다고 예고하십시오. 이 슬라이드는 지표성 프레임이라 scoped CSS로 h2를 강조했습니다.
-->

---

<!-- _class: comparison -->

## 무효 키 요청: LiteLLM vs 우리 — DB를 언제 건드리나

카카오 장애의 핵심 = 무효 키가 DB 풀을 인질로 잡음 · 근거: auth_service.py:24-76; middleware/auth.py:73-103

- **LiteLLM (passthrough)** — ① 요청마다 사용량 기록(spend-log)을 DB에 씀 → 잘못된 키 폭주가 곧 DB 쓰기 폭풍 ② 키 검증도 DB 히트 경향 ③ 무효 키가 DB 커넥션 슬롯을 붙잡고 대기 → 슬롯 고갈 ④ CPU는 멀쩡한데 느려짐 → 재배포해야만 복구(전형적 풀 고갈)
- **우리 (re-origination 게이트웨이)** — ① 인증 실패 시 DB write 0 ② Redis 먼저 조회, miss=401(auth_service.py:52,70-76) ③ DB 세션이 실제 쿼리가 있을 때만 열림(lazy) — 무효 키는 커넥션 슬롯 안 잡음 ④ 형식오류=Redis 0회 / 미등록 VK=Redis 2 miss→401

<!--
챕터의 심장(원본 slide 24)입니다. 카카오 장애를 먼저 정확히 규정하십시오: Team Plan으로 전환한 사용자 일부가 settings.json을 안 지워서 무효·빈 키로 요청이 계속 나갔고, LiteLLM 로그에 "No api key passed in"이 대량으로 쌓이며 느려졌습니다. CloudWatch상 CPU·메모리는 정상인데 강제 롤링 재배포로만 풀렸고, 결국 일요일 새벽 주기적 재시작으로 우회하는 안을 논의했다고 공유받았습니다. 이 지문 — "CPU 여유 + 처리량 0 + 재시작으로만 복구" — 은 DB 커넥션 풀 고갈의 교과서적 증상입니다. 요청이 실제 처리 중이 아니라 빈 슬롯을 기다리며 대기하니 CPU는 놀고 처리량만 0이 되고, 재시작은 풀을 리셋할 뿐 새는 지점을 못 찾은 대증요법입니다. 왜 LiteLLM이 취약한가: passthrough 프록시라 요청마다 spend-log를 DB에 쓰는 경로가 있어 무효 키라도 검증·기록 단계에서 DB를 건드립니다. 우리는 정반대로 오른쪽이 전부 코드 근거입니다 — 인증 실패면 DB 쓰기 0, 키 검증은 Redis 먼저 보고 없으면 401, 세션은 쿼리가 있을 때만 열려 커넥션 슬롯을 안 잡습니다. 예상 질문 "LiteLLM이 진짜 요청마다 쓰냐"에는 "공유받은 장애 지문과 LiteLLM의 spend-log 구조에 근거한 추정이며, 우리 쪽은 코드로 확증했다"고 정직하게 선을 그으십시오.
-->

---

<!--_class: default-->

## 무효 키 한 건이 DB에 닿기 전 3번 걸러진다

auth_service.py — DB 세션은 마지막에야, 그것도 유효 키만 · 근거: auth_service.py:24-76; middleware/auth.py:74

```text
[무효 키 1건]
    │
    ▼ ① 빈/형식 깨진 헤더 → _extract_bearer_token ValueError → 즉시 401
    │   (Redis 조회조차 0회)
    ▼ ② 형식은 맞는 키 → 원문 대신 sha256 해시로만 Redis 조회
    │   (auth_service.py:49,52,70 — cache:vk, vk 매핑 2회)
    ▼ ③ Redis miss → 그 자리에서 거부 PermissionError
    │   "Invalid or inactive virtual key" (:75-76) → 401
    ▼ ④ 여기서 끝 — session_factory()로 세션 컨텍스트에 들어와도
    │   Redis에 없어 select(User) 쿼리를 안 돌림 → 커밋·I/O 0
    ▼
[결과] 무효 키가 아무리 쏟아져도 = Redis 확인 후 401뿐, DB 커넥션 부하 0
```

- 기술 근거: SQLAlchemy asyncpg 세션은 실제 쿼리가 있을 때만 커넥션을 잡는(lazy) 구조 — 쿼리 없으면 체크아웃 자체가 안 일어남

<!--
흐름을 단계로 짚으며 "무효 키는 이 사다리를 끝까지 못 올라온다"를 강조하십시오(원본 slide 25). 1단계: Authorization 헤더가 비었거나 Bearer로 시작 안 하면 토큰 추출 단계(_extract_bearer_token)에서 ValueError로 튕겨 미들웨어가 바로 401을 냅니다 — Redis 조회조차 안 합니다(형식오류=Redis 0회). 2단계: 키가 형식은 맞으면 평문을 저장하지 않고 sha256 해시로만 Redis를 두 번 봅니다(완성된 인증정보 캐시 cache:vk, user_id 매핑 vk). 3단계: 둘 다 없으면(miss) 그 자리에서 거부(PermissionError)해 401 — 이 시점까지 DB는 단 한 번도 안 열렸습니다. 결정적 포인트: middleware/auth.py:74의 async with session_factory() as db는 strategy.authenticate를 호출하려고 세션 컨텍스트를 열지만, VKAuthStrategy는 Redis에 유효 매핑이 있어야만 실제 select(User) 쿼리로 갑니다. 즉 무효 키는 세션 컨텍스트에 들어와도 쿼리를 실행하지 않고, SQLAlchemy asyncpg 세션은 실제 쿼리가 있을 때만 커넥션을 잡는(lazy) 구조라 쿼리가 없으면 커넥션 체크아웃 자체가 안 일어납니다 — 이게 커넥션 풀을 안 잡는 기술적 근거입니다. 예상 질문 "세션을 열면 커넥션 잡는 것 아니냐"에 정확히 이 lazy 획득 원리로 답하십시오.
-->

---

<!--_class: default-->

## 정직한 판정 — 강점은 확증, 자가복구는 조건부 하향 정정

rev75(1.0.51-resilience): 6축 적대검증 + HIGH 갭 2건 실배선 · 근거: health.py:49-110; db.py:32; config.py:75-78

- 강점(코드로 확증): 무효 키는 DB 쿼리 미실행·30초 SSE 스트리밍 중에도 커넥션 미점유·인증 실패 시 DB 쓰기 0 → 슬롯 회전율 높음
- [정정] "재시작 불필요"는 과장이었음 → DB/Redis 응답장애·둘 다 불안정(BOTH_DEGRADED)일 때로 한정해 자동 복구
- 갭 수정1: 상태 점검 전용 /health/ready 신설 — 커넥션 소진(hard_cap)/DB 강등 시 503 → 고장 파드를 트래픽 배분에서 자동 제외
- 갭 수정2: DB 커넥션 대기 30→10초로 실제 배선(db.py:32, config.py:75) — "CPU는 멀쩡한데 느림"을 차단
- 미검증(최우선): 무효 키 수천 건 실부하 재현 — 순수 커넥션 고갈의 자동 복구는 아직 보증 못 함

<!--
이 슬라이드로 신뢰를 삽니다(원본 slide 26) — 자랑만 하지 않고 아직 못 한 것을 먼저 인정합니다. 먼저 코드로 확증된 강점 3가지를 못 박으십시오: (1) 무효 키는 DB 세션을 못 잡음(앞 슬라이드), (2) 답변을 한 글자씩 흘려보내는 실시간 스트리밍(SSE) 중에도 DB 커넥션을 붙잡지 않음 — auth.py:73 주석대로 인증·라우팅 조회는 열고 즉시 반납하고 30초 스트림 동안 커넥션 슬롯을 안 잡습니다(카카오와 정반대로 슬롯 회전율이 높음), (3) 인증 실패 시 DB 쓰기 0. 여기까지가 "카카오식 잘못된 키=DB 폭발의 핵심 경로를 설계로 막았다"입니다. 그다음 정직 정정이 핵심입니다: 원래 데브로그에 "fail-fast 자가복구로 재시작 불필요"라 썼는데, 13개 독립 에이전트의 6축 적대검증에서 이걸 조건부로 하향했습니다 — DB/Redis 응답장애나 둘 다 불안정한 상태(BOTH_DEGRADED)에는 재시작 없이 복구 경로가 있지만, 순수하게 커넥션이 다 차는 고갈·동시 요청 폭주는 능동 헬스체크로 감지되지 않고 트래픽 제외 경로로도 안 빠져 카카오처럼 수동 개입이 필요할 수 있습니다. 그래서 rev75에서 HIGH 2건을 실제로 배선했습니다: 상태 점검 전용 /health/ready를 신설해 커넥션이 다 차면 503을 내 고장 난 파드를 ALB 트래픽 배분에서 빼고(살아있음 판정용 /health는 그대로 둬 재시작 연쇄를 회피), DB 커넥션 대기 시간(pool_timeout)을 실효 없던 30초 설정에서 실제 10초 빠른 실패로 배선했습니다. 라이브로 rev74→75를 배포해 /health/ready의 200·503 로직과 3-client 회귀 200을 실증했고 283개 유닛 테스트가 통과했습니다. 최우선 남은 것 — 무효 키 수천 건 실부하 재현으로 커넥션/메모리 실측(mock 금지) — 을 밝혀 "견고하다"를 증거 기반으로 만들겠다고 닫으십시오. 예상 질문 "그럼 지금 카카오 상황이 오면 안 죽나"에는 "잘못된 키→DB 폭발 경로는 막혔고 커넥션 고갈 파드도 이제 트래픽에서 빠지지만, 순수 커넥션 고갈의 자동 복구는 부하 재현 전까지는 보증 못 한다"고 정직하게 답하십시오.
-->

---

<!-- _class: divider -->

## 7. 견고성 강화 — 적대검증 → 수정 → 배포

'견고하다'는 주장을 13개 독립 AI 검증팀이 6축으로 공격해, 스스로 반박·정정하고 코드로 확정한 규율 (rev75 / gateway-proxy 1.0.51-resilience)

<!--
전환 노트입니다. 앞선 안전성 A·B 챕터에서 '무효키가 몰려도 DB가 안 죽는다', '주기적 재시작이 필요 없다'고 자신했습니다. 이번 챕터는 그 주장을 그대로 두지 않고 일부러 공격해(적대검증) 스스로 반박·정정한 이야기입니다. 톤은 '방어적'이 아니라 '과장을 증거로 바꾸는 엔지니어링 규율'입니다. 성과는 새 기능이 아니라, 우리 주장을 스스로 공격해 코드로 확정하고 못 증명한 건 못 증명했다고 정직히 남긴 규율이라는 점을 전환 멘트로 던지십시오.
-->

---

## 과장을 증거로 바꾸는 규율 — 자기 주장을 스스로 공격하다

- 적대검증(adversarial) = '내 주장이 틀렸다'고 가정하고 코드로 반증을 시도하는 검증
- 13개 독립 AI 검증팀 × 6가지 견고성 관점(6축)으로 동시 교차검수
- 남이 우리 시스템에 제기한 주장 2건 반박 + 내가 쓴 과장 1건 스스로 하향
- 심각도 높음(HIGH) 2건 · 중간(MED) 2건을 실제로 코드 수정
- 283개 자동 테스트 통과 · 코드리뷰 2회 · rev75 실서버 배포로 검증

<!--
이번 발표에서 새로 강화한 핵심입니다. 세 종류의 결과를 예고하십시오 — 남이 우리에게 제기한 주장 2건 반박, 내가 쓴 과장 1건을 스스로 하향 정정, HIGH 2건·MED 2건을 실제 코드 수정. 모두 file:line 코드 근거와 함께 제시합니다. 검증의 3층(283 자동테스트·리뷰 2회·rev75 배포)이 뒤 슬라이드들에서 하나씩 채워집니다. 데이터 출처를 정직히: 이미지 태그 1.0.51-resilience는 배포 설정값(values)으로, helm rev75는 실제 배포 이력(helm history)으로 확인했고, 신규 14개 테스트는 직접 확인, 283개 총합은 문서값입니다. 예상 질문 '적대검증이 뭐냐'에는 '내 결론을 코드로 반증하려는 시도이며, 통과하면 그때 확증'이라고 답하십시오. 근거: 소스 슬라이드 27.
-->

---

<!-- _class: comparison -->

## 검증의 세 얼굴 — 남의 주장 반박, 내 과장 하향, 실수정 도출

- **검증 결과(반박 2건)** — Redis 응답 대기시간 제한(socket_timeout) 미설정 주장 → 실제 2초로 설정됨(반박) · readiness 경로 고장 주장 → 정상 동작 확인(반박)
- **정정 + 도출(내 과장 1건 → 확정 4건)** — 'fail-fast 자가복구' → DB·캐시 둘 다 나쁜 경우(BOTH_DEGRADED)로 한정해 스스로 하향 · 연결자리 고갈·요청 폭주·메모리 누적만으로 죽는 경우는 자동회복 보장 못 함(정직 명시) · 결과 HIGH 2 + MED 2 확정

<!--
이번 챕터의 지적 정직성을 파는 슬라이드입니다. 세 종류의 결과를 또렷이 나누십시오. 첫째, 옆 프로젝트가 우리 시스템에 제기한 주장 2건은 코드로 반박했습니다 — Redis(캐시) 응답 대기시간 제한이 안 걸려 있다는 주장은 실제로 2초로 설정돼 있었고, 고장 감지 경로(readiness)도 정상이었습니다. 둘째, 가장 중요한 건 내가 예전에 쓴 과장을 내가 스스로 정정한 것입니다 — '재시작 없이 스스로 회복한다'를 'DB·캐시가 둘 다 나빠진 특정 상황(BOTH_DEGRADED)에 한정된다'로 낮췄습니다. 연결자리 고갈, 요청 동시 폭주, 메모리 누적으로 죽는 경우는 자동 건강검진으로 감지되지 않고 트래픽 차단 대상에서도 안 빠져 카카오 사례처럼 사람이 손대야 할 수 있다고 정직히 적었습니다. 근거로 health.py:34-35를 열어 기존 /health가 DB_DEGRADED 상태에서도 200을 반환했음을 보여줄 수 있습니다(health.py:32-37). 예상 질문 '왜 처음부터 정확히 안 썼나'에는 '주장은 검증 전 가설, 적대검증이 그 가설을 코드로 확정·반박하는 절차'라 답하십시오. 근거: 소스 슬라이드 28.
-->

---

## 수정 4건 ①/② — 고장 파드 자동 제외 + 죽은 설정 실배선(HIGH)

- 수정 전 공통 진단: HIGH 2건 모두 '값은 정의됐으나 실제로 연결 안 됨'
- [HIGH#1] 고장 감지 전용 /health/ready 신설 → 아픈 서버를 트래픽 대상에서 자동 제외
- 살아있음 검진(/health)은 관대하게 유지 → 파드 줄줄이 재시작(연쇄) 회피
- [HIGH#2] DB 연결 대기 상한 30초→10초 · 연결 재활용 1시간을 실제 배선
- 이름만 있고 안 읽히던 pool_timeout → 숨은 기본 30초 = '바쁠 때 요청당 최대 30초 지연' 차단

<!--
4건 중 심각도 높음 2건입니다. HIGH#1: 기존 건강검진(/health)은 DB가 나빠진 상태에서도 200으로 통과시켜(health.py:34-35) 연결자리가 다 찬 아픈 서버가 계속 트래픽을 받았습니다. 새로 만든 고장 감지 전용 검진(/health/ready, health.py:49-110)은 건강 등급이 정상이 아니거나 DB 연결자리가 완전히 소진되면 503을 내 그 서버를 트래픽 분배 대상에서 뺍니다. 살아있는지 보는 검진(liveness)은 관대한 /health를 유지해 서버가 줄줄이 재시작되는 사태를 피합니다. HIGH#2가 가장 교훈적입니다 — 과거 db.py가 배포 설정의 pool_timeout 값을 안 읽어 이름만 있고 효과가 없었고, SQLAlchemy 기본값 30초가 숨어 '연결자리 고갈 시 요청당 최대 30초 지연 = CPU는 노는데 느림'이라는 카카오식 증상을 그대로 안고 있었습니다. 지금은 config.py:75(=10초)/78(=1시간 재활용)이 db.py:32/34에 실제 연결됐습니다. 근거: health.py:49-110, config.py:75-78, db.py:32. 소스 슬라이드 29.
-->

---

## 수정 4건 ③/④ — 숨은 5회 재시도·무한 메모리 차단(MED)

- [MED#3] Bedrock 재시도 횟수 배선: 정의만 있고 안 쓰이던 죽은 설정 해소 → 숨은 5회를 1회로
- 그동안 AWS 라이브러리 기본 5회 × 대체모델 후보 최대 6개 = 요청 폭풍 위험
- 응답 대기(read_timeout)는 긴 스트리밍용 300초 유지 — 낮추면 실시간 스트림이 끊김
- [MED#4] 보안 감지기 저장소를 무제한 dict → 상한 자료구조(최대 4096개, 오래된 것부터 삭제)
- 위조 IP를 계속 바꿔 보내 메모리가 터지는(OOM) 경로 차단 — DB·캐시 검진에 안 걸리는 누적 계열

<!--
4건 중 중간 심각도 2건입니다. MED#3: Bedrock 재시도 상한(=1)이 정의만 있고 어디서도 참조되지 않는 죽은 설정이었습니다. 그동안 AWS 라이브러리 기본 재시도(총 5회)가 몰래 동작했고, 대체 모델 후보 최대 6개와 곱해져 요청 폭풍을 키울 수 있었습니다. main.py:110·190에서 실제로 재시도 설정에 연결했습니다. 응답 대기시간(read_timeout)은 긴 스트리밍(300초)을 유지 — 같은 연결이 오래 이어지는 실시간 응답에도 쓰이므로 낮추면 스트림이 끊깁니다. MED#4: 보안 감지기의 카운터가 상한 없는 일반 딕셔너리라, 위조된 접속 IP를 계속 바꿔가며 보내면 서버 메모리가 무한정 커질 수 있었고, 이건 DB·캐시 건강검진으로는 안 잡히는 '카카오식으로 모니터링에 안 걸리는 누적' 계열이었습니다. 최대 4096개까지만 담고 오래된 것부터 버리는 자료구조로 상한을 걸었습니다. 예상 질문 '이름만 있고 안 읽히는 설정이 왜 생겼나'에는 'admin-api 쪽 db.py는 두 값을 제대로 명시했는데 요청이 실제로 몰리는 gateway-proxy만 누락된 불일치였고 — 적대검증이 이 비대칭을 잡았다'고 답하십시오. 근거: main.py:97-110,188-190, event_detector.py:20,33,92. 소스 슬라이드 29.
-->

---

## 수정이 새 장애를 낳지 않게 — '바쁨'이 아니라 '진짜 고갈'로

```
[1차 초안] 정상 연결 수(regular_size) 기준 = "바쁨"으로 포화 판정
      ↓
[리뷰 지적] 서버 1대(HPA min=1)면 트래픽 급증 시 유일한 서버가 스스로 이탈
      ↓  → 트래픽 받을 서버 0대 = 자기가 부른 서비스 중단
[2차 교정] hard_cap(정상 자리 + 여분 자리 전부 소진) = "진짜 고갈"로 상향
      ↓
[역할 분담] 즉시성 보조 신호 = '모든 자리 소진'
      ↓        1차 감지 = 건강 등급(degradation)
[degradation] 같은 풀의 헬스체크 쿼리 SELECT 1 → 대기 상한 10초 실패 → DB 등급 하락 → 503
      ↓
[내부속성 제거] 여분 자리 수(max_overflow)를 설정값에서 직접 읽음(라이브러리 속성 미의존)
```

<!--
견고성을 고치다 오히려 새 취약점을 만들 수 있다는 메타 교훈입니다. 1차 초안은 DB 연결 포화를 '정상 연결 수(예: 20개)를 넘었나 = 바쁨'으로 판정했습니다. 코드리뷰가 지적한 함정: 이 환경은 서버를 최소 1대만 띄우는 설정(HPA minReplicas=1)이라, 트래픽이 급증하면 그 유일한 서버가 '연결 20개 넘음'을 이유로 스스로 '트래픽 받을 준비 안 됨' 판정을 내려 빠져버립니다. 재시작도 안 하므로 트래픽 받을 서버가 0대가 되는 '자기가 부른 서비스 중단'이 됩니다. 2차 교정: 포화 판정을 '진짜 고갈 = 정상 자리 + 여분 자리(overflow)까지 전부 소진'으로 올렸습니다(health.py:80-88). 오래 지속되는 고갈은 건강 등급(degradation)이 1차로 잡습니다 — 헬스체크가 던지는 SELECT 1 쿼리가 같은 연결풀을 쓰므로 자리가 없으면 대기 상한 10초 안에 실패를 보고해 DB 건강등급을 떨어뜨리고 그게 503을 만듭니다. 즉 '모든 자리 소진'은 즉시성 보조 신호, 건강 등급이 1차 감지입니다. 추가로 여분 자리 수(max_overflow)를 라이브러리 내부 속성이 아니라 설정값에서 직접 읽게 바꿨습니다 — 속성 이름이 바뀌면 기준값이 조용히 정상 자리 수로 붕괴해 너무 일찍 빠질 위험 때문입니다(health.py:76-79). 예상 질문 '진짜 고갈 기준이면 너무 늦게 빠지나'에는 '건강 등급이 대기 상한 10초로 이미 1차 감지하므로 이중 안전망'이라 답하십시오. 근거: health.py:76-96, values-eks-fargate-dev.yaml(minReplicas:1). 소스 슬라이드 30.
-->

---

## 검증 3층 — 283유닛 · 2패스 리뷰 · rev75 라이브

<style scoped>
h2 { font-size: 44px; color: var(--color-accent); }
</style>

- 283개 자동 테스트 통과(+14): 고장 감지 검진·보안 감지기 상한 테스트 파일 신설
- 신규 테스트 파일: test_readiness_probe.py · test_event_detector_bounded.py
- 코드리뷰 2회(작성과 분리): '서버 1대 자가 이탈'·'라이브러리 내부 속성 의존' 지적 반영·재교정
- rev74→75 실서버 배포: /health/ready의 200/503 동작 실증
- 배포 후 3개 클라이언트(claude-code·codex·cowork) 전부 정상(200) 회귀 확인

<!--
검증은 세 층입니다. (1) 283개 자동 테스트 통과 — 이전 269개에서 +14개, 새 테스트 파일 test_readiness_probe.py(고장 감지 검진)와 test_event_detector_bounded.py(보안 감지기 상한)가 실제로 존재하고 '진짜 고갈 판정·연결 포화·상한 자료구조·오래된 항목 삭제'를 커버합니다. (2) 코드리뷰를 작성과 분리해 2회 돌렸고, 그 결과 앞 슬라이드의 '서버 1대 자가 이탈' 교정과 '라이브러리 내부 속성 의존 제거'가 나왔습니다 — 리뷰가 실제로 코드를 바꿨다는 증거입니다. (3) rev74→rev75(gateway-proxy 1.0.51-resilience) 실서버 배포로 고장 감지 검진의 200/503 동작과 3개 클라이언트 정상(200)을 실증했습니다. 데이터 출처: 이미지 태그 1.0.51-resilience는 배포 설정값(values), helm rev75는 배포 이력(helm history), 신규 14개 테스트는 직접 확인, 283 총합은 문서값입니다. 근거: tests/unit/test_readiness_probe.py, tests/unit/test_event_detector_bounded.py, values-eks-fargate-dev.yaml(1.0.51-resilience). 소스 슬라이드 31.
-->

---

## 미검증은 미검증이라 말한다 — 남은 것과 정직한 결론

- 일부러 안 고침① 요청 동시처리 상한(--limit-concurrency): 값 잘못 잡으면 정상 트래픽까지 막아 실부하 A/B 후로
- 일부러 안 고침② 무제한 백그라운드 작업 생성: #4로 메모리 위험 대부분 해소, 작업 개수 상한은 후속
- 최우선 남은 것: 무효키 수천 건 실부하 재현으로 연결풀·캐시·메모리 실측(가짜 데이터 금지)
- 카카오 대비 결론(조건부 유지): 무효키→DB 폭발 핵심 경로 차단 · 헤더 유출 표면 구조적 부재
- 단, 순수 연결풀 고갈의 자동 회복은 실부하로 아직 증명 못 함 — '견고' 최종 판정 유보

<!--
챕터의 결론이자 정직성의 클라이맥스입니다. 두 건은 일부러 안 고쳤습니다. 요청 동시처리 상한(--limit-concurrency)은 실시간 스트리밍이 오래 이어지는 특성상 값을 잘못 잡으면 정상 트래픽까지 막으므로 실제 부하 A/B 테스트 후로 미뤘습니다. 무제한 백그라운드 작업 생성은 앞의 4번 수정(보안 감지기 IP 상한)으로 위험이 대부분 해소돼 후속으로 뒀습니다. 가장 중요한 정직함: '견고하다'는 최종 판정을 아직 안 내렸습니다. 최우선 남은 것은 무효키 수천 건을 실제로 쏘는 부하 재현으로 연결풀·캐시·메모리를 실측하는 것입니다(mock·합성 데이터가 아닌 진짜 부하로). 카카오 대비 결론은 조건부로 유지합니다 — 무효키가 DB를 터뜨리는 핵심 경로는 설계로 차단됐고 헤더가 새는 표면은 구조적으로 없지만, 자동 회복은 특정 장애 상황(BOTH_DEGRADED)에 한정되고 순수 연결풀 고갈 회복은 실부하로 아직 증명 못 했습니다. 예상 질문 '그럼 지금 카카오 상황이 오면 안 죽나'에는 '잘못된 키→DB 폭발 경로는 막혔고 고갈 파드도 이제 트래픽에서 빠지지만, 순수 커넥션 고갈의 자동 복구는 부하 재현 전까지 보증 못 한다'고 답하십시오. 마무리 멘트: '이번 챕터의 성과는 새 기능이 아니라, 우리 주장을 스스로 공격해 코드로 확정하고, 못 증명한 건 못 증명했다고 정직히 남긴 규율'입니다. 근거: tests/unit 신규 파일, values-eks-fargate-dev.yaml. 소스 슬라이드 31.
-->

---

<!-- _class: divider -->

## 8. 서버사이드 웹서치 (Architecture C)

클라 무설정으로 1P Claude와 똑같이 검색된다 — 게이트웨이가 검색 도구를 쥐고 tool_use(모델의 '검색 필요' 신호)를 가로챈다

<!--
전환 노트입니다. 앞 챕터가 '견고성을 스스로 공격해 증명했다'였다면, 이번 챕터는 게이트웨이가 단순 대체를 넘어 사용자 경험(UX)까지 소유함을 보여줍니다. 핵심 축 한 문장: '모델이 검색하고 싶다며 tool_use(도구 호출 신호)를 냈을 때 그걸 누가 받아 실행하느냐' — 사용자 기기(A)가 아니라 게이트웨이(C)가 받는다는 것입니다. tool_use는 모델이 스스로 '이건 내가 모르니 검색이 필요하다'고 내는 신호라고 풀어 주십시오. C를 택한 이유는 검색 횟수·비용·정책을 한 곳에서 보고 통제하는 게 게이트웨이의 존재 이유와 맞기 때문임을 예고하고 넘어가십시오.
-->

---

## 검색은 사용자가 아니라 게이트웨이가 대신 돈다

- 클라 무설정: 사용자는 아무것도 안 깔아도 임시 출입증(VK)만 있으면 검색이 그냥 됨 — 정품(1P) Claude와 동일한 경험
- 게이트웨이가 검색 도구를 쥐고 요청에 몰래 끼워넣음 → 모델이 낸 tool_use('검색 필요' 신호)를 가로챔 → 대신 검색·결과 재투입
- 검색으로 나뉜 여러 왕복을 답변 하나의 실시간 스트림(SSE, 답을 한 글자씩 흘리는 방식)으로 이어 붙임
- A(사용자 기기가 직접 검색)가 아닌 C(서버가 검색)를 택함 — 검색 횟수·비용·정책을 한 곳에서 보고 통제하려고
- 공수는 큼: 서버가 검색을 대신 도는 루프 + 두 API 규격(방언)에 맞춘 조각 이어붙이기를 감수한 설계

<!--
이 챕터의 오프닝입니다. 핵심 축은 '모델이 tool_use(도구 호출 신호)를 냈을 때 그걸 누가 받아 실행하느냐'이며, 우리는 게이트웨이가 받습니다 — 이걸 아키텍처 C라고 부릅니다. 사용자 기기가 직접 검색하는 방식(A)과 대비됩니다. C를 택한 덕에 검색이 몇 번 일어났는지 집계하고, 폭주하지 않게 상한을 걸고, 클라이언트별로 켜고 끄는 통제가 전부 가능하며, 사용자는 VK만 있으면 아무 설정 없이 검색이 됩니다. 공수가 크다는 점(서버가 검색 루프를 돌고, 두 종류 방언에 맞춰 답변 조각을 이어붙임)도 정직하게 말하되, 게이트웨이의 존재 이유(모든 요청을 한 곳에서 보고 통제·과금)와 맞기에 감수했다고 마무리하십시오. 근거: 원본 slide 32.
-->

---

## 검색으로 나뉜 여러 왕복을, 사용자는 한 스트림으로만 본다

```
[클라] "검색해줘"(도구 얘기 0)
   │  ①  게이트웨이가 web_search 도구를 요청에 몰래 끼워넣고 모델 호출
   ▼
[모델] ──②── tool_use(name==web_search, '검색 필요' 신호)
   │        게이트웨이가 가로챔(사용자에겐 안 보임)
   ▼
[gateway-proxy(서울)]
   │  ③  AgentCore Gateway(MCP 규격·SigV4 서명·us-east-1)
   ▼
[AWS 관리형 검색 백엔드] ──▶ 실제 검색 결과
   │  ④  tool_result로 대화에 재투입 → 모델 재호출
   │      (모델이 더는 검색 요청 안 할 때까지 반복)
   ▼
[클라] ⑤ 검색 과정은 감추고 최종 답변 글자만 끊김 없는 SSE 스트림으로
```

<!--
챕터의 뼈대입니다. 흐름을 순서대로 짚으십시오. (1) 사용자는 검색 도구 얘기를 전혀 안 하고 그냥 질문만 던집니다. (2) 게이트웨이가 _with_web_search_tool로 web_search 도구 정의를 요청에 끼워넣습니다. (3) 모델이 검색 신호(tool_use)를 내면 이름이 정확히 'web_search'인 것만 가로채고, 이 신호는 사용자에게 절대 흘러가지 않습니다. (4) 게이트웨이가 AWS 관리형 검색 백엔드(AgentCore Gateway)를 MCP라는 표준 규격으로 불러(SigV4 정식 서명, us-east-1) 실제 검색을 시킵니다. MCP는 AI가 외부 도구를 부르는 표준 규격이라 한 마디 붙이십시오. (5) 검색 결과를 대화에 다시 붙여 재호출하고, web_search_loop.py:210의 반복문이 모델이 더는 검색을 요청하지 않을 때까지 돕니다. 이게 1P Claude 웹서치와 같은 구조입니다. 라이브 실증: E2E에서 claude-code가 학습 시점 이후인 'Python 3.13.14, 2026-06-10'을 출처 URL과 함께 답했고, 검색 신호는 사용자에게 노출되지 않았습니다. 예상 질문 'gateway가 두 개라 헷갈린다'에는 우리 gateway-proxy(서울, 지휘자)가 AWS의 AgentCore Gateway(us-east-1, 검색 실행기)를 부르는 것이고 사용자는 앞의 것만 안다고 답하십시오. 근거: web_search_loop.py:210, agentcore_mcp_client.py.
-->

---

## '몰래 주입'은 마법이 아니다 — tools 목록을 다시 짜는 것

```
요청 방향(끼워넣기)              응답 방향(가로채기·지우기)
──────────────────────         ──────────────────────
① tools에 web_search 1개 덧붙임   ③ tool_use 이름이 'web_search'와
   (_with_web_search_tool)          정확히 일치하면 낚아챔
        │                              (뜻 짐작 아님 · 이름 완전 일치)
        ▼                              │
② 이건 LLM API 표준 도구             ▼
   모델은 진짜 도구로 믿고           ④ 그 tool_use·tool_result를
   설명 읽은 뒤 스스로 호출             사용자에게 안 보냄
                                        → 사용자 쪽 web_search 흔적 0
```

- 사용자가 원래 쓰던 도구는 그대로 둠(같은 것만 중복 제거) · 이미 web_search를 직접 선언했으면 가로채지 않고 통과(F-7)

<!--
기술 청중이 반드시 던지는 질문 — '게이트웨이가 어떻게 웹서치를 몰래 넣나?' — 에 대한 정면 답입니다. 핵심 메시지: 이건 특별한 해킹이 아니라, LLM API가 원래 제공하는 도구 목록(tools) 파라미터를 요청이 지나는 길목에서 다시 조립하는 것뿐입니다. (1) 끼워넣기 — _with_web_search_tool(web_search_loop.py:102-118)이 사용자 요청을 복사해 tools에 web_search 정의 하나를 덧붙입니다. (2) 사용자는 그 도구를 준 적이 없지만 모델에겐 진짜 도구로 보이고, 설명을 읽은 모델이 스스로 호출 여부를 판단합니다. (3) 가로채기 — 모델 응답을 읽다가 tool_use 이름이 정확히 'web_search'면(GW_WEB_SEARCH_NAME, :48/:122) 낚아챕니다. 키워드 분석도 뜻 짐작도 아닌 이름 완전 일치라 오탐이 없습니다 — 우리가 심은 이름을 도로 잡는 것입니다. (4) 흔적 지우기 — 그 tool_use와 tool_result를 사용자에게 안 보내(:252-275) 사용자는 최종 답변만 봅니다. 안전장치 둘: 사용자 자기 도구는 그대로 두고(같은 것만 중복 제거), 사용자가 이미 web_search를 직접 선언했으면 가로채지 않고 통과시킵니다(F-7, :877-901). 이 '요청엔 도구를 더하고, 응답에선 흔적을 지우는' 방식이 이 덱의 핵심축 re-origination(받은 요청을 그대로 넘기지 않고 새로 조립해 발신)의 응용입니다. 예상 반론 '모델이 우리 도구인 걸 아나?'→모름, 껍데기와 진짜를 구분 못 함. '사용자가 눈치채나?'→못 챔, 흔적을 지웠으니까. 다음 장에 폭주 방지 가드레일을 이어 설명합니다. 근거: web_search_loop.py:102-118·48·122·252-275·877-901.
-->

---

## 폭주하면 도구를 빼서 모델이 답을 마치게 강제한다

- 가드레일(폭주 방지 안전선): 5회/90초 한도에 닿으면 다음 왕복부터 tools에서 web_search를 빼(include=False)
- 도구가 빠지면 모델은 더 검색 못 하고 반드시 최종 답을 마침 — 검색이 여러 갈래로 퍼지는 폭주(fan-out) 억제
- 매 검색마다 시간 제한을 다시 확인하는 이중 장치 — Codex 교차검증 2라운드에서 잡힌 것
- 가로채기 기준은 이름 완전 일치라 오탐 0 — 우리가 심은 web_search 이름을 도로 낚아챌 뿐
- 사용자가 직접 web_search를 선언한 경우엔 통과(F-7) → 사용자 의도를 덮어쓰지 않음

<!--
앞 장의 '주입·가로채기·지우기' 메커니즘을 폭주 방지와 사용자 존중 관점에서 마무리하는 슬라이드입니다(원본 slide 34의 6번째 단계 + 5번째 단계를 손실 없이 담기 위해 분리했습니다). 가드레일: web_search_loop.py:211에서 검색 시도가 5회를 넘거나 90초를 넘으면 다음 왕복 요청의 tools에서 web_search를 빼(include=False) 모델이 반드시 최종 답을 하게 강제합니다. 요청이 여러 갈래로 폭주하는 것(fan-out)을 막으려고 매 검색마다 시간 제한을 다시 확인하는 장치도 있는데, 이건 Codex 교차검증 2라운드에서 잡힌 것입니다. 가로채기는 이름 완전 일치라 오탐이 없고, 사용자가 이미 자기 web_search를 선언했으면 우리가 가로채지 않고 통과(F-7)시켜 사용자 의도를 덮어쓰지 않습니다. 예상 질문 '5회면 너무 적지 않나'에는 대부분의 질문은 1-2회 검색으로 끝나고, 상한은 악성·무한 루프 방어선이라 답하십시오. 근거: web_search_loop.py:211·877-901.
-->

---

<!-- _class: comparison -->

## 검색 백엔드는 같다 — 다른 건 '누가 검색을 실행하느냐'뿐

- **A — 사용자 기기가 직접 검색**: 검색 실행이 기기 안(게이트웨이 우회) · 기기마다 도구 등록 필요(.mcp.json 등) · CoWork는 SigV4 불가라 Cognito 기계 간 토큰(M2M JWT) · '툴 추가'로 보임 · 검색 횟수(web_search_count) 셀 수 없음 · 가드레일은 기기가 정함(우리 통제 밖)
- **C — 게이트웨이가 대신 검색**: 검색 실행이 게이트웨이 코드(web_search_loop.py) 안 · 무설정(VK만 있으면 됨) · AWS 정식 서명(SigV4)·키를 코드에 안 심고 파드에 권한(IRSA) · 그냥 검색됨(투명) · 검색 횟수 per-client 기록 · 5회/90초 게이트웨이가 강제

<!--
설계 판단의 근거를 방어합니다. 먼저 공정하게 시작하십시오: A와 C는 같은 AWS 관리형 검색 백엔드(AgentCore Gateway), 같은 호출 코드(agentcore_mcp_client.py:142)를 씁니다 — 차이는 딱 하나, '모델의 검색 요청을 누가 받아 실행하느냐'입니다. A(형제 프로젝트가 채택한 방식)는 tool_use를 사용자 기기에 넘기고 기기가 직접 검색 백엔드를 호출합니다. 기기마다 도구 등록이 필요하고, CoWork는 SigV4를 못 해서 Cognito 기계 간 토큰(M2M JWT)을 씁니다. 정직하게 정정할 점: 모델 토큰 자체는 A도 매 왕복 게이트웨이를 지나 집계됩니다. A의 진짜 약점은 검색 실행이 기기 안에서 일어나 게이트웨이를 건너뛴다는 것 — 그래서 검색이 몇 번인지 모르고, 검색 한 번짜리 대화가 게이트웨이 입장에선 서로 무관한 여러 독립 요청으로 흩어져 하나의 사용 단위로 안 묶입니다. C는 검색 실행이 web_search_loop.py 안이라 검색 횟수 집계, 검색 비용($7/1k) 귀속, 요청 묶기가 전부 가능하고, 폭주 방지 가드레일(5회·90초)도 C에만 있습니다. 결론으로 목적 정합을 못박으십시오: 게이트웨이의 존재 이유가 모든 요청을 한 곳에서 보고 통제·과금하는 것인데, 검색을 밖으로 내보내면 그 목적을 스스로 포기하는 셈입니다. 예상 질문 'Cowork는 원래 A 전용 아니냐'에는 맞지만 C는 세 클라이언트 모두를 티 안 나게 지원한다고 답하십시오. 근거: agentcore_mcp_client.py:142.
-->

---

## 검색이 되게 만든 게 아니라, 검색을 통제 가능하게 만들었다

- 클라이언트별 켜고 끄기: 라우팅 규칙 한 줄(routing_profiles.web_search_enabled) — 재배포 없이 Redis 캐시만 비우면 즉시 반영
- 폭주 방지 안전선: 최대 5회·90초 제한을 게이트웨이가 강제(매 검색마다 재확인)
- 검색이 실패해도 답변 스트림은 안 끊김: 실패를 결과로 넣어주면 모델이 자기 지식으로 이어서 답함(조용히 검색 없는 호출로 낮춤)
- 검색 횟수(web_search_count)는 성공한 검색만 셈 → 클라이언트별로 DB에 정확히 귀속(usage_logs)

<!--
'검색이 되게 만들었다'를 넘어 '검색을 통제 가능하게 만들었다'를 보여주는 두 장 중 첫 장입니다(원본 slide 36의 6불릿을 손실 없이 2장으로 나눴습니다). 거버넌스부터: 라우팅 규칙 한 줄(routing_profiles.web_search_enabled)로 클라이언트별 켜고 끄기를 하고, admin-api 토글 API를 부르면 Redis 캐시가 즉시 비워져 재배포 없이 켜고 끕니다. 라이브로 '설정→효과'를 실증했다고 인용하십시오: 끄면 모델이 '검색할 수 없다'며 안 하고, 켜면 최신 정보(3.13.14)와 출처 URL로 실제 검색합니다. 가드레일은 web_search_loop.py:211에서 5회·90초를 넘으면 다음 왕복부터 도구를 빼 모델이 최종 답을 하게 만듭니다. 회복탄력성: 검색 함수는 절대 예외를 던지지 않고, 실패하면 실패 사실을 결과로 넣어줘 답변 스트림이 안 끊깁니다 — 검색 백엔드 연결이 실패해도 검색 없는 일반 호출로 조용히 낮추고 사용량 집계는 유지합니다. 관측: web_search_count는 성공한 검색만 세고, 실패는 폭주 방지용 카운터로 따로 세서 과금 정확성을 지킵니다. 예상 질문 '검색어를 로깅하나'에는 프라이버시 안전장치로 '무엇을 검색했는지'는 저장하지 않고 '몇 번·얼마나'만 집계한다고 답하십시오. 근거: messages.py:366, web_search_loop.py:210-211.
-->

---

## 꺼두면 기존 경로가 한 바이트도 안 바뀐다 (opt-in)

- 선택 활성(opt-in): 꺼두면 검색 도구 없음 → 분기 자체를 건너뜀 → 기존 경로가 한 바이트도 안 바뀜
- 검색 왕복의 요청 본문도 허용 목록(_BEDROCK_ALLOWED_FIELDS)으로 새로 조립(re-origination) → 헤더·필드가 뒤로 새어나갈 표면 자체가 없음
- 즉 웹서치는 기존 안전성 축(재구성 발신)을 그대로 물려받음 — 검색이 새 유출 통로를 열지 않음

<!--
이 챕터의 마무리 슬라이드로, 웹서치가 기존 설계 원칙(선택 활성·재구성 발신)을 그대로 지킨다는 점을 못박습니다(원본 slide 36의 opt-in·헤더 안전성 두 불릿). 배선의 안전성: 선택 활성이라 검색 도구가 꺼져 있으면 그 분기를 통째로 건너뛰어 기존 경로가 한 바이트도 안 바뀝니다 — 즉 웹서치를 도입해도 기존 요청 흐름의 회귀 위험이 0이라는 뜻입니다. 헤더 안전성과의 연결이 핵심입니다: 검색 왕복의 요청 본문도 앞 챕터에서 다룬 허용 목록(_BEDROCK_ALLOWED_FIELDS)으로 새로 조립돼(re-origination) 사용자가 임의로 넣은 필드가 뒤 모델로 새어나가지 않습니다. 즉 웹서치라는 새 기능이 기존 안전성 챕터의 '재구성 발신' 원칙을 그대로 물려받아, 검색이 새로운 유출 통로를 열지 않는다는 것을 강조하며 챕터를 닫으십시오. 다음 챕터(기술 하이라이트)로 자연스럽게 넘어갑니다. 근거: messages.py:366, web_search_loop.py:210-211.
-->

---

<!-- _class: divider -->

## 09. 기술 하이라이트 — 가용성·비용유실0·text2SQL

한 곳이 고장나도 전체가 멈추지 않는 세 축 — 다층 가용성 · 비용 기록 유실 0 · text2SQL 정확도 안전망을 코드로 증명한다

<!--
전환 노트. 앞선 안전성·견고성 챕터를 보완하는 세 가지 기술 축입니다. 셋 다 한 문장으로 하면 "한 곳이 고장나도 전체가 멈추거나 사용자를 붙잡아두지 않는다"입니다. 첫째 특정 모델이 아파도 요청을 인질로 잡지 않는 다층 가용성(circuit breaker=고장난 모델 자동 차단, fallback=대체 모델 전환, degradation=인프라가 흔들리면 스스로 건강 등급을 낮춤), 둘째 돈 관련 기록이 어떤 경로로도 조용히 사라지지 않는 비용 파이프라인, 셋째 숫자를 다루는 곳에 LLM의 답이 매번 달라지는 특성을 결정적 규칙으로 감싸는 text2SQL 5계층 안전망입니다. 모두 실제 파일·함수에 근거한 확정 설계라는 점을 강조하고 넘어가십시오. (근거: slide 37 note)
-->

---

<style scoped>
pre { font-size: 13px; line-height: 1.3; padding: 14px; } li { font-size: 17px; }
</style>

## 한 곳이 아파도 요청을 인질로 잡지 않는다

방어를 세 겹으로 겹쳐, 장애 반경을 모델 → 요청 → 인프라 순으로 좁힌다. 아래로 갈수록 방어 범위가 넓어진다.

```
장애 반경                        방어 계층 (범위)
─────────────────────────────────────────────────────
[모델 1개]  ──▶  ① Circuit Breaker (모델 단위)
                   최근 30초 에러율 ≥50% + 최소 5콜 → OPEN(차단)
                   모델별로 따로 차단
                          │
                          ▼
[요청 1건]  ──▶  ② Fallback (요청 단위)
                   같은 벤더 안에서만 대체 모델로 전환
                   허용 안 된 건 기본 차단 · 최대 5개
                          │
                          ▼
[인프라]    ──▶  ③ Degradation (인프라 단위)
                   5연속 실패로 강등 · 회복은 천천히
                   간헐 장애도 놓치지 않음
```

- **Circuit Breaker**: 고장난 모델에 요청이 계속 몰려 전체가 느려지는 걸 차단 — 상태는 공유 저장소 Redis에 심음
- **Fallback**: 원 요청 모델을 항상 첫 후보로, 같은 벤더·같은 허용 목록 안에서 품질순 대체 (다른 AWS 계정으로 절대 안 샘)
- **Degradation**: 능동 헬스체크 결과를 집계해 건강 등급 관리 — 인프라 전체가 흔들리면 스스로 몸을 낮춤

<!--
"장애가 나도 요청을 붙잡아두지 않는다"가 핵심입니다. 세 겹 방어를 청중 언어로 먼저 말하십시오 — 아픈 모델 하나를 자동으로 잠시 끊는 회로 차단기(모델 단위), 살아있는 대체 모델로 갈아타는 폴백(요청 단위), 인프라 전체가 흔들리면 스스로 몸을 낮추는 등급 강등(인프라 단위). 회로 차단기(circuit breaker)는 모델별로 최근 30초 동안 에러율 50%·최소 5콜을 넘으면 차단(OPEN) 상태를 Redis에 심어, 고장난 모델에 요청이 계속 몰리는 걸 막습니다. 근거는 circuit_breaker.py:82-93. 폴백은 원래 요청 모델을 항상 첫 후보로 두고 같은 벤더·같은 허용 목록 안에서 품질 높은 순으로 대체 후보를 만들며, 다른 AWS 계정으로는 절대 넘어가지 않는 게 안전 계약입니다(fallback_loop.py:283-307). 등급 강등(degradation)은 능동 헬스체크 결과를 집계하는 상태 관리입니다(degradation/manager.py:18-28). 다음 슬라이드에서 각 계층의 정밀한 동작 디테일(half-open 탐침·타임아웃 면책·fail-open)을 이어서 다룬다고 예고하십시오. (근거: slide 38 note)
-->

---

## 죽은 모델을 여럿이 동시에 두드리지 않게 한다

한 계층을 고칠 때 새 장애를 만들지 않는 정밀 규칙 — 복구 탐침 단일화 · 타임아웃 면책 · 예약 롤백 · fail-open.

- **복구 탐침 딱 1개(half-open)**: 서버가 여러 대라도 죽은 모델을 다시 두드리는 건 전체에서 오직 하나만 통과(`SET NX PX`), 랜덤 지연은 Redis 서버 시각 기준이라 서버 시계가 어긋나도 일관
- **타임아웃 면책**: 진짜 서버 오류(502/503)만 차단기에 실패로 기록, 느려서 난 타임아웃(504)은 모델 탓이 아니므로 벌점 제외 — 판단 기준을 오염시키지 않음
- **예약 롤백**: 대체 전환 시 앞서 잡아둔 예산·속도 예약을 되돌린 뒤 다음 후보로 → 다음 계산에 찌꺼기가 안 남음
- **Degradation 오탐 교정**: 3연속→5연속 실패로 완화(바쁠 때 오탐 방지), 성공은 카운터를 1만 깎아 간헐 장애를 놓치던 버그 수정
- **차단기 저장소(Redis) 다운 시 fail-open**: 차단 정보를 못 읽는다고 답변까지 막으면 더 큰 장애 → 가용성 우선으로 열어둠

<!--
앞 슬라이드가 "세 겹 방어가 무엇인가"였다면 이 슬라이드는 "각 계층을 정밀하게 어떻게 동작시키나"입니다. 디테일 둘부터: 첫째 '이제 살았나' 확인하는 복구 탐침(half-open)을 서버 전체에서 딱 하나만 통과시켜(SET NX PX) 여러 서버가 동시에 죽은 모델을 두드리지 않게 합니다(circuit_breaker.lua:39-42). 둘째 이때 쓰는 랜덤 지연값을 각 서버 시계가 아니라 Redis 서버 시각으로 맞춰 서버마다 시계가 어긋나도 일관되게 동작합니다. 타임아웃 면책이 중요합니다 — 느려서 난 타임아웃(504)은 모델 자체의 고장이 아니므로 차단기에 벌점을 주지 않아 판단 기준을 오염시키지 않고, 진짜 서버 오류(502/503)만 실패로 기록합니다. 실패하면 그 후보가 미리 잡아둔 예산·속도 예약을 되돌려 다음 요청 계산에 찌꺼기가 남지 않게 합니다. degradation은 과거 3연속 실패 기준이 서버가 바쁠 때 멀쩡한데도 강등되는 오탐을 냈던 걸 5회로 완화했고, 강등 안 된 상태에서 한 번 성공했다고 카운터를 0으로 밀어 간헐 장애를 놓치던 버그를 '성공은 1만 깎기'로 고쳤습니다. 예상 질문 '왜 저장소가 죽었을 때 막지 않고 열어두느냐'에는, 차단 정보를 못 읽는다고 답변 자체를 막으면 그게 더 큰 장애이기 때문이라고 답하십시오. (근거: slide 38 note)
-->

---

## 비용 기록은 어떤 경로로도 조용히 사라지지 않는다

<style scoped>
h2 { font-size: 44px; color: var(--aws-blue); }
</style>

요청 경로는 1-2ms로 가볍게, 무거운 DB 쓰기는 뒤에서 별도 워커가 처리한다.

```
[요청 종료]  finalize()  ── 동기, 오직 Redis만 (DB 손 안 댐) ── 1-2ms
     │  · 예산 차감(사용자/팀/앱)
     │  · 분/시간/토큰당 비용 정산
     │  · 마지막에 대기줄에 한 줄 추가 → XADD cost:stream
     ▼
[cost:stream]  대기줄(Redis Stream)
     │                              ↓ XADD 실패(Redis 다운)
     │                        [메모리 Spool] ── 복구 감지 시 재발행
     ▼
[cost-recorder-worker]  ── 비동기 오프로드, 무거운 DB 쓰기
        사용 로그 저장 · 예산 갱신 · 당일 집계 · 임계 알림
```

- **동기 경로(1-2ms)**: 예산 차감·비용 정산은 Redis에서만, 마지막에 `XADD cost:stream` 한 줄 — 이 경로에 DB 쓰기 0
- **비동기 오프로드**: 무거운 DB 쓰기(사용 로그·예산 갱신·집계·알림)는 전담 워커가 대기줄을 읽어 모아서 처리
- **Dead-letter Spool**: XADD가 실패해도(Redis 다운) 임시 버퍼에 담고 복구 시 재발행 → 기록 영구 유실 방지

<!--
메시지는 "돈 관련 기록은 어떤 경로로도 조용히 사라지지 않는다"입니다. 비용 정산을 담당하는 finalize는 요청이 끝나자마자 빠른 처리 경로에서 오직 Redis 연산만 합니다 — 예산 차감(사용자/팀/앱), 분당·시간당·토큰당 비용 정산, 그리고 마지막에 비용 기록을 대기줄(cost:stream)에 한 줄 추가(XADD)뿐입니다. 이 경로에 DB 쓰기가 전혀 없어서 1-2ms로 가볍습니다(cost_recorder.py:42-60). 무거운 DB 쓰기(사용 로그 저장·예산 사용량 갱신·당일 집계·임계 알림)는 전담 워커(cost-recorder-worker)가 그 대기줄을 읽어 모아서 처리합니다 — 스트리밍 응답이 연결·DB를 붙잡지 않게 한 설계와 같은 철학입니다. 핵심 강조점은 임시 버퍼(dead-letter spool)입니다: Redis가 하필 정산 시점에 죽어 XADD가 실패하면, 예전엔 경고만 찍고 기록이 영구히 사라졌는데, 이제 그 기록을 메모리 안 임시 버퍼에 담아뒀다가 헬스체크가 Redis 복구를 감지하면 같은 대기줄로 다시 넣습니다(cost_stream_spool.py:80-126). 다음 슬라이드에서 이 버퍼의 무결성 보장 디테일(멱등성·pop-and-hold·drain)과 정직한 한계를 이어서 다룬다고 예고하십시오. (근거: slide 39 note)
-->

---

## 다시 넣어도 두 번 집계되지 않는다

임시 버퍼가 안전한 전제 — 중복 방지 · 처리 중 항목 보호 · 손실을 보이게.

- **중복 방지(멱등성)**: 요청ID 유일 제약 + 예산은 재적용해도 안전 → 재발행해도 비용이 두 번 집계되지 않음
- **꺼내 들고 처리(pop-and-hold)**: 처리 중 항목은 손에 쥔 채라 새 항목이 밀어내지 못하고, 중간에 취소돼도 되돌려 담아(`finally`) 안 잃음
- **버퍼가 꽉 차면**: 가장 오래된 것부터 버리되 카운터·로그로 남김 → 손실을 '보이게' 만듦
- **종료 직전 drain**: 서버 종료 전 남은 항목을 마지막까지 대기줄로 비움
- **[정직한 한계]** 이 버퍼는 메모리라 서버가 통째로 꺼지면 못 버팀 → 초~분 단위 짧은 장애 완화책, 완전 영속화는 후속 과제(Redis 밖 별도 저장)

<!--
앞 슬라이드가 "3계층 비용 흐름"이었다면 이 슬라이드는 "임시 버퍼가 왜 안전한가"의 무결성 디테일입니다. 이 버퍼가 안전한 전제는 중복 방지입니다 — 요청ID가 유일하고 예산은 다시 적용해도 안전하게 설계돼 있어, 다시 넣어도 비용이 두 번 집계되지 않습니다. 엔지니어링 디테일: 버퍼를 비울 때 항목을 손에 꺼내 든 채로(pop-and-hold) 재발행하기 때문에, 동시에 들어오는 새 항목이 처리 중인 것을 절대 밀어내지 못합니다. 되돌려 담는 처리를 finally에 둬서, 종료 중 태스크가 취소돼도 항목을 안 잃습니다. 버퍼가 꽉 차면 가장 오래된 것부터 버리되 카운터·로그로 남겨 손실을 '보이게' 만들고, 서버 종료 직전 남은 것을 마지막까지 비웁니다(drain). 정직하게 한계도 말하십시오 — 이건 메모리라 서버가 통째로 꺼지면 못 버티는, 초~분 단위 짧은 장애 완화책이고, 완전히 안전한 저장은 Redis 밖에 별도 저장이 필요한 후속 과제입니다(cost_stream_spool.py:80-126). 예상 질문 '그럼 서버가 죽으면 그 순간 버퍼 기록은?'에는 그 짧은 창은 아직 보증 못 하며 영속화가 로드맵 항목이라 정직하게 답하십시오. (근거: slide 39 note, slide 49)
-->

---

## 숫자는 말로 추론하지 않고 결과값에서만 참조한다

<style scoped>
table { font-size: 18px; }
th { color: var(--aws-blue); }
</style>

답이 매번 달라질 수 있는 LLM 위에, 절대 넘지 않는 안전 하한선을 규칙으로 깐다. 위쪽이 실행 전, 아래쪽이 실행 후 검증.

| 계층 | 언제 | 무엇을 막나 | 방식 |
|---|---|---|---|
| **BI 5-agent** | 질문 접수 | 역할 혼선 | 총괄이 SQL·코드·검증·시각화·리포트 담당을 도구처럼 호출 |
| **L0/L1** | 실행 **전** | 유령 컬럼·시간대·중복합산 | DB에 없는 컬럼 거부 · KST 누락/N배 뻥튀기/상태값 경고 |
| **L2** | SQL 나오면 | AI 재량 오염 | 코드가 **항상** 자동 검증 실행 (사람·AI 재량 0) |
| **L3** | 실행 **후** | 확률적 오답 | 같은 질문 k번 풀어 실제 실행 → 다수결(self-consistency) |
| **L4** | 고위험만 | 미탐 잔여 | 다른 계열 모델 교차 검토 — 막진 않고 경고만(fail-soft) |

- **단가는 사람이 승인해야 반영**: AWS 공식 Price List → 미리보기 → 변경 대조(diff) → 운영자 승인 후에만 적용 (자동 반영 원천 금지)

<!--
메시지는 "숫자를 다루는 곳엔 LLM의 답이 매번 달라지는 특성을 결정적 규칙으로 감싼다"입니다. BI Insight는 하나의 질문을 5개의 전문 AI로 분업하는 구조로, 총괄(orchestrator)이 SQL작성·코드·검증·시각화·리포트 담당 에이전트를 도구처럼 불러 씁니다(main.py:402-458). 정확도 방어는 5계층입니다. L0/L1은 SQL을 실제 Aurora에서 돌리기 '전에' 검사합니다 — DB 스키마에 없는 유령·모호 컬럼은 결정적으로 거부해 스스로 고치게 하고, 시간값을 한국시간 기준(Asia/Seoul) 없이 쓰면 경고해 자정 9시간 오차를 막고, 1:N로 조인한 뒤 부모 값을 합산해 N배로 뻥튀기되는 중복 합산을 경고하며, 대시보드 상태값(성공만 집계) 정합도 경고합니다(sql_guard.py:90-125,254-331). L2는 SQL이 나오면 총괄이 검증을 부를지 말지에 맡기지 않고 코드가 항상 자동 검증을 돌린다는 것입니다(사람·AI 재량 0). L3는 같은 질문을 여러 전략으로 k번 풀어 각각 실제로 실행한 뒤, 결과가 같은 것끼리 묶어 가장 많이 나온 답을 채택합니다 — 정답은 여러 경로로도 같은 결과에 수렴하고 오답은 제각각 흩어진다는 원리입니다. 반드시 짚을 함정: 이 다수결은 AI가 말로 투표하는 게 아니라 코드가 결과값을 결정적으로 집계해야 하고, 예전에 모든 후보가 엉뚱하게 탈락하던 버그도 고쳤습니다. L0 사전 검사는 모든 후보가 똑같이 틀리는(시간대·팀 조건 같은) 공통 실수를 다수결이 못 잡을 때의 최종 하한 안전망입니다. L4는 고위험 심층분석일 때만 다른 계열 모델이 답을 거꾸로 되짚어 검토하는 약한 검문으로, 막지는 않고 경고만 합니다(fail-soft). (근거: slide 40 note)
-->

---

## 청구 오류는 사람 승인 게이트로 끊는다

단가 관리 — 예전엔 손입력하다 청구 오류가 났고, 이제 자동 반영을 원천 금지한다.

- **원천(source)**: AWS 공식 Price List에서 단가를 가져옴 — 사람이 손으로 입력하지 않음
- **미리보기 → 대조(diff)**: 가져온 단가를 기존 값과 변경 대조로 보여줌 (무엇이 얼마나 바뀌는지 시각화)
- **운영자 승인 게이트**: 운영자가 승인해야만 반영, 자동 반영은 원천 금지 (`pricing_sync_service`)
- **반영은 기존 로직 재사용**: 기존 단가 설정 로직을 그대로 타서 이력·감사로그·캐시 갱신을 자동으로 얻음 (`models.py:112-143`)
- **환각 차단의 본질**: "숫자는 말로 다시 추론하지 말고, 구조화된 결과 값에서만 참조한다" — 5계층·단가 게이트가 모두 이 규율의 구현

<!--
마지막 단가 관리 파트입니다. 예전엔 단가를 사람이 손으로 입력하다 청구 오류가 났는데, 이제 AWS 공식 Price List에서 가져와 변경 내용을 미리보기·대조하고 운영자가 승인해야만 반영합니다 — 자동 반영은 원천 금지고, 반영은 기존 단가 설정 로직을 재사용해 이력·감사로그·캐시 갱신을 그대로 얻습니다(pricing_sync_service.py:1-20, models.py:112-143). 이 슬라이드로 이 챕터 전체를 하나의 규율로 묶어 닫으십시오: 환각 차단의 본질은 '숫자는 말로 다시 추론하지 말고, 구조화된 결과 값에서만 참조한다'는 것이고, text2SQL 5계층도 단가 승인 게이트도 전부 이 한 규율의 구현입니다. 예상 질문 '왜 자동 반영을 안 하나'에는 청구에 직결되는 값이라 자동화의 편의보다 사람 확인의 안전을 택했고, diff 미리보기로 승인 부담을 최소화했다고 답하십시오. (근거: slide 40 note)
-->

---

<!-- _class: divider -->

## 10. 설치·배포 가이드 — Mac / Windows

claude-code · codex · cowork 를 사내 게이트웨이에 붙이기 — 셀프 온보딩 5분에서 회사 PC 함대 배포까지

<!--
이 챕터는 "게이트웨이를 어떻게 쓰기 시작하나"에 대한 실무 가이드로 넘어가는 전환입니다. 앞 아홉 챕터가 "왜 안전한가·왜 다른가"였다면 여기서부터는 청중 중 엔지니어가 발표 직후 바로 따라 할 수 있는 정확한 명령어와 파일 경로를 다룹니다. 세 가지 축으로 안내한다고 예고하십시오 — 개발자용 셀프 온보딩 5분, 3-client 각각의 연결 방식, 그리고 운영자용 함대(MDM) 대량 배포입니다. 근거는 소스 slide 41 note입니다.
-->

---

## 로그인 1회면 끝, 이후 인증은 자동이다

- 개발자 셀프 온보딩 약 5분 — `gateway-cli` 로 회사 계정 로그인(OIDC=표준 싱글사인온) 1회면 이후 손이 안 감
- 로그인 후 VK(Virtual Key, 실제 클라우드 자격과 분리된 임시 출입증) 발급·1시간 갱신이 자동
- 세 클라이언트가 붙는 방식이 다름 — claude-code=apiKeyHelper, codex=config.toml, cowork=앱 설정
- 대량 배포는 MDM(회사가 단말을 원격 관리하는 방식) 프로파일 — macOS `.mobileconfig` / Windows `.reg`
- 설명 축 2개: "개발자용 셀프 온보딩" vs "운영자용 함대 배포"로 나눠 봄

<!--
이 챕터의 지도를 세 문장으로 먼저 주십시오. (1) 개발자는 gateway-cli 로 회사 계정 로그인(OIDC 표준 싱글사인온) 한 번이면 이후 VK 발급·갱신이 자동이라 5분 셋업 후 손이 안 갑니다 — VK는 가상 키, 실제 클라우드 자격과 분리된 임시 출입증이라고 첫 등장 시 풀어 주십시오. (2) 세 클라이언트가 붙는 방식이 다릅니다: claude-code 는 apiKeyHelper(키를 자동 넣어주는 헬퍼)+접속 주소, codex 는 config.toml 설정파일, cowork 는 데스크톱 앱 설정입니다. (3) 대량 배포는 MDM(단말 원격관리) 프로파일로 수백 대 PC에 한 번에 뿌립니다. 발표 시 "개발자용 셀프 온보딩"과 "운영자용 함대 배포"를 나눠 설명하십시오. 근거: 소스 slide 41.
-->

---

## 신원 인증 → VK 발급 흐름은 세 클라이언트가 공유한다

- macOS/Linux: 운영자 패키지 → `/usr/local/bin/{gateway-cli, api-key-helper}`
- Windows(관리자 PowerShell): `Expand-Archive` → `$env:ProgramFiles\GatewayCLI` + PATH 등록
- 소스 설치(uv=파이썬 격리 설치 도구): `uv tool install --from ./gateway-cli gateway-cli`
- 환경변수(어느 로그인·어느 게이트웨이로): `OIDC_ISSUER_URL` · `OIDC_CLIENT_ID` · `ADMIN_API_URL` · `ANTHROPIC_BASE_URL`
- 로그인: `gateway-cli login` → 브라우저 회사계정 로그인(PKCE=비밀번호가 CLI를 안 거치는 안전한 방식) → 토큰은 `~/.gateway-cli/oidc-tokens.json` 본인만 읽기(0600)

`근거: guides/user-guide.md §3-4 · gateway-cli login(PKCE, localhost:8090-8092 콜백) · uv 설치 QUICKSTART.md:35 / connect.md:54`

<!--
이 슬라이드가 온보딩의 관문입니다. 세 클라이언트 무엇을 쓰든 신원 인증(회사 계정 로그인, OIDC 표준)→VK(임시 출입증) 발급 흐름은 동일하고 gateway-cli 가 그걸 담당합니다. macOS/Linux 는 운영자 제공 tar 를 /usr/local/bin 에 풀고, Windows 는 관리자 PowerShell 에서 Expand-Archive 후 ProgramFiles 에 넣고 PATH 에 등록합니다(둘 다 user-guide §3 에 실제 명령 있음). 개발자라면 uv(파이썬 격리 설치 도구)로 설치해 나중에 uv tool uninstall 로 깨끗이 지울 수 있고 기존 Claude 환경을 안 건드린다는 점이 실무자 안심 포인트입니다. gateway-cli login 은 브라우저 PKCE 플로우라 비밀번호가 CLI 를 거치지 않고, 토큰은 ~/.gateway-cli/ 에 본인만 읽는 권한(0600)으로만 저장됩니다. 이후 VK 발급·1시간 갱신은 api-key-helper 가 자동입니다(다음 불릿에서 이어짐). 예상 질문 "토큰 만료되면?"엔 갱신 자동, 그룹(권한) 변경도 자동 갱신(silent refresh, 재로그인 없이 조용히 새 토큰 교환)으로 반영된다고 답하십시오. 근거: 소스 slide 42.
-->

---

## 한 번 로그인 후 VK 발급·갱신은 손대지 않는다

- 로그인이 끝나면 VK(임시 출입증) 발급·1시간마다 갱신을 `api-key-helper` 가 매 요청 자동 수행
- 재로그인 불필요 — 개발자는 로그인 1회 후 인증에 신경 쓸 게 없음
- 로그인 시스템의 그룹(권한) 변경도 자동 갱신(silent refresh)으로 반영
- 정리: gateway-cli(로그인·발급 지휘) + api-key-helper(매 요청 자동 주입·갱신) 2인 1조

<!--
앞 슬라이드가 "설치와 첫 로그인"이었다면 이 장은 "그 뒤로 왜 손이 안 가나"를 못 박는 자리입니다. 핵심은 gateway-cli login 한 번 뒤부터는 api-key-helper 가 매 요청마다 VK(임시 출입증)를 자동 주입하고 1시간마다 조용히 갱신한다는 것입니다. 재로그인이 필요 없고, 로그인 시스템(IdP)에서 그룹·권한이 바뀌어도 silent refresh(재로그인 없이 백그라운드로 새 토큰을 교환하는 방식)로 자동 반영됩니다. 두 도구의 역할을 2인 1조로 요약하십시오 — gateway-cli 는 로그인과 발급을 지휘하고, api-key-helper 는 실행 시점마다 키를 넣어주고 갱신합니다. 이 슬라이드는 소스 slide 41-42의 "5분 셋업 후 손이 안 감" 메시지를 놓치지 않으려 분리한 것입니다.
-->

---

<!-- _class: comparison -->

<style scoped>
table { font-size: 17px; }
table td, table th { padding: 10px 12px; border-color: var(--color-border); }
table { font-size: 15px; } td, th { padding: 8px 10px; } li { font-size: 15px; }
</style>

## 연결 방식은 셋 다 달라도, 게이트웨이가 앱 서명으로 자동 식별한다

| 클라이언트 / 설치 | 연결 설정 (무엇을 · 어디에) |
|---|---|
| **claude-code** (`npm i -g @anthropic-ai/claude-code`) | `gateway-cli setup` → apiKeyHelper + `ANTHROPIC_BASE_URL` |
| **codex** (`@openai/codex`) | `~/.codex/config.toml`: `base_url=<GW>/v1`, `wire_api=responses`(OpenAI 응답 규격), `env_key=GATEWAY_VK`(키 담은 환경변수) |
| **cowork** (Claude 데스크톱 앱) | `configLibrary/<uuid>.json`: `inferenceProvider=gateway`(호출 대상을 게이트웨이로) + baseUrl + VK + `authScheme=bearer` |

- 앱 자동 식별: claude-code=UA `claude-cli/`(external,cli) · codex=originator `codex_cli_rs`
- 라우팅(현 dev): claude-code → **333** native · codex → **123** Mantle GPT-5.5

`근거: docs/guides/connect.md · gateway-clients/codex-box/entrypoint.sh · COWORK-GATEWAY-SETUP.md §2`

<!--
이 표가 챕터의 핵심 참조 자료입니다. 세 클라이언트가 연결하는 방식이 근본적으로 다르다는 걸 명확히 하십시오. claude-code 는 gateway-cli setup 한 줄이 apiKeyHelper(키 자동 주입 헬퍼)와 접속 주소(base URL)를 설정에 기록하면 끝입니다. codex 는 OpenAI 응답 규격(Responses API)을 쓰므로 ~/.codex/config.toml 에 model_provider=gateway, base_url=<게이트웨이>/v1, wire_api=responses(응답 규격 지정)를 넣고 VK 를 GATEWAY_VK 환경변수로 참조합니다. cowork 는 Claude 데스크톱 앱이라 CLI 가 아니라 앱 설정파일(configLibrary/<uuid>.json)의 inferenceProvider(호출 대상)를 gateway 로 바꾸고 baseUrl·VK·authScheme=bearer(인증방식)를 채웁니다. 결정적 포인트: 개발자는 어느 클라이언트인지 게이트웨이에 따로 알릴 필요가 없습니다 — 게이트웨이가 앱마다 다른 서명(User-Agent claude-cli/, originator codex_cli_rs)으로 자동 식별해 사용 기록(usage_logs.client)에 남기고 routing_profiles(어느 앱을 어느 계정으로 보낼지 정하는 DB 규칙 한 줄)로 각각 다른 AWS 계정에 라우팅합니다 — 현 dev 기준 claude-code 는 333 계정 native, codex 는 123 계정 Mantle GPT-5.5. 예상 질문 "앱마다 키 따로?"엔 아니오, 같은 로그인 신원에서 발급된 VK 하나를 세 곳에 넣을 수 있다고 답하십시오. 근거: 소스 slide 43.
-->

---

## claude-code 연동 — gateway-cli setup 이 settings 를 자동 작성한다

```text
[1] claude --version            (없으면 npm install -g @anthropic-ai/claude-code)
      ↓
[2] gateway-cli login           브라우저 OIDC 로그인 (1회)
      ↓
[3] gateway-cli setup --gateway-url <GW> --admin-api-url <ADMIN>
      ↓  설정 자동 기록
[4] macOS: /etc/claude-code/managed-settings.d/gateway.json  (sudo)
           또는  ~/.config/Claude/settings.json
      ↓
[5] Claude Code 재시작 → 이후 `claude` 실행만 (apiKeyHelper 가 VK 매 요청 자동 첨부)
      ↓
[6] 원복(한 줄): gateway-cli disable → 게이트웨이 우회, 기존 직접 API 로 즉시 복귀
```

`근거: guides/user-guide.md §5 · 부록A 명령어 요약`

<!--
claude-code 는 가장 매끄러운 경로입니다. 핵심은 gateway-cli setup 한 줄이 Claude Code 가 읽는 설정 파일에 apiKeyHelper(키 자동 주입 헬퍼) 경로와 접속 주소(ANTHROPIC_BASE_URL)를 자동으로 써준다는 것입니다. 우선순위는 회사 관리 설정(managed-settings.d, 가장 강함)이 개인 설정(~/.config/Claude/settings.json)보다 우선이며, setup 은 전자에 쓰려 하므로 sudo(본인 PC 로그인 암호)를 물을 수 있습니다. 권한이 없으면 수동으로 개인 설정에 apiKeyHelper+env 블록을 넣는 방법도 user-guide §5.2 에 있습니다. Windows 는 관리 설정 경로가 다르지만 개념은 동일하고, 실무에선 운영자 MDM(단말 원격관리) 배포(.reg)로 관리하는 것이 깔끔합니다. 강조: 설정 후엔 개발자가 그냥 claude 를 실행하면 되고 VK(임시 출입증)는 매 요청 자동 주입·갱신됩니다. 되돌리려면 gateway-cli disable 한 줄이면 직접 API 로 복귀합니다 — 이 "쉬운 원복"이 도입 저항을 낮춥니다. 근거: 소스 slide 44.
-->

---

## codex 연동 — 로컬 설정 또는 컨테이너 격리 실행

- codex 로컬: `~/.codex/config.toml` 에 gateway provider — `base_url=<GW>/v1`, `wire_api=responses`(OpenAI 응답 규격) + `GATEWAY_VK`(키 환경변수)
- codex 격리: `gateway-clients/codex-box`(Docker) — 내 PC의 `~/.codex` 는 손 안 대고 컨테이너 안에서만 config 생성(환경 오염 0)
- `wire_api=responses` 가 핵심 — codex 는 OpenAI Responses 규격을 쓰기 때문
- 두 갈래 선택: 로컬 직접 설정(빠름) vs 컨테이너(개발자 맥 환경 오염 걱정 없음)

`근거: gateway-clients/README.md · cowork_setup.md`

<!--
codex 와 cowork 는 claude-code 보다 손이 조금 더 갑니다 — 이 슬라이드는 codex 만 다룹니다. codex 는 두 가지 길이 있습니다. 로컬은 ~/.codex/config.toml 에 gateway 를 호출 대상으로 직접 넣습니다 — base_url 뒤에 /v1, 그리고 wire_api=responses 가 핵심인데, codex 는 OpenAI Responses(응답) 규격을 쓰기 때문입니다. 격리를 원하면 gateway-clients/codex-box 도커 이미지가 내 PC 설정을 전혀 안 건드리고 컨테이너 안에서만 config.toml 을 생성해 실행하므로 개발자 맥 환경 오염 걱정이 없습니다. 두 갈래 중 하나를 고르면 됩니다 — 로컬은 빠르고, 컨테이너는 깨끗합니다. 예상 질문 "회사 표준은?"엔 개인은 취향, 운영 배포는 뒤에 나올 MDM 이 표준이라고 답하십시오. 근거: 소스 slide 45(codex 부분).
-->

---

## cowork 연동 — 데스크톱 앱 설정파일을 직접 편집한다

- cowork(macOS): `~/Library/Application Support/Claude-3p/configLibrary/<uuid>.json` 편집 → 앱 재시작
- 필수 4키: `inferenceProvider=gateway`(호출 대상) · `inferenceGatewayBaseUrl`(HTTPS 접속주소) · `…ApiKey=VK` · `…AuthScheme=bearer`(인증방식)
- cowork 는 문서상 HTTPS 접속주소 요구 → 운영 전환 시 CloudFront HTTPS 진입점 필요(현 dev 는 HTTP 실측 통과, 운영은 HTTPS)
- 원복: config 백업 복원 후 앱 재시작 → `inferenceProvider=bedrock` 직결로 안전 복귀

`근거: COWORK-GATEWAY-SETUP.md §3-4 · cowork_setup.md`

<!--
cowork 는 CLI 가 아니라 데스크톱 앱이라 앱 설정파일을 직접 편집합니다. macOS 경로는 ~/Library/Application Support/Claude-3p/configLibrary/<uuid>.json 이고, 여기서 inferenceProvider(호출 대상)를 gateway 로 바꾸고 네 키 — baseUrl·VK·authScheme=bearer(인증방식) 를 채운 뒤 앱을 재시작합니다. 여기서 반드시 짚을 것: cowork 는 문서상 HTTPS 접속주소를 요구하므로 운영 전환 시 CloudFront HTTPS 진입점이 필요합니다. 현재 dev 는 HTTP 로도 실측 통과하지만 운영은 HTTPS 여야 한다는 점을 정직하게 밝히십시오. 원복은 config 백업 복원 후 재시작이면 inferenceProvider=bedrock 직결로 안전하게 돌아갑니다. 예상 질문 "Windows cowork 경로는?"엔 개인 편집보다 운영자 .reg/MDM 배포가 표준이라고 답하십시오. 근거: 소스 slide 45(cowork 부분).
-->

---

## 수백 대 PC 는 MDM 프로파일로 한 번에 배포한다

- 대량 배포: MDM(단말 원격관리) 프로파일 — macOS `.mobileconfig` / Windows `.reg`
- Admin UI 의 **Export** 버튼이 프로파일을 자동 생성 → 수작업 없음
- 개발자용 셀프 온보딩과 운영자용 함대 배포를 분리 — 운영자는 Export 프로파일로 수백 대 일괄 반영
- 원복도 함대 단위: 프로파일 회수/백업 복원으로 직접 API·bedrock 직결 복귀

<!--
이 슬라이드는 "운영자용 함대 배포" 편입니다. 앞의 개발자 셀프 온보딩과 대비해, 회사 PC 수백 대를 손으로 하나씩 설정하지 않고 MDM(회사가 단말을 원격 관리하는 방식) 프로파일로 한 번에 뿌린다는 점이 핵심입니다. macOS 는 .mobileconfig, Windows 는 .reg 형식이며, 중요한 건 이걸 사람이 직접 만들지 않고 Admin UI 의 Export 버튼이 자동 생성해 준다는 것입니다. 즉 운영자는 관리 화면에서 프로파일을 뽑아 MDM 으로 배포하면 끝입니다. 원복도 함대 단위로 프로파일 회수나 백업 복원을 통해 직접 API·bedrock 직결로 되돌립니다. 발표 포인트: 도입 규모가 커도 개별 개발자 손을 빌리지 않고 중앙에서 통제·배포·원복이 된다는 운영 민첩성입니다. 근거: 소스 slide 41, 45(MDM 부분).
-->

---

## 복붙용 자료가 저장소에 있어 발표 후 바로 실행된다

- `guides/QUICKSTART.md` — 3-client × Mac/Windows 1페이지 빠른시작(복붙 명령 전부)
- `scripts/onboard-macos-linux.sh` — 설치+로그인 자동화(`--setup-claude-code` 옵션 줄 때만 설정 변경)
- `scripts/onboard-windows.ps1` — Windows PowerShell 동일 자동화(`-SetupClaudeCode`)
- `guides/user-guide.md`(Claude Code 상세) · `docs/guides/connect.md`(Claude Code+Cowork) · `gateway-clients/`(격리 실행 컨테이너)
- 안전장치: 스크립트 기본은 로그인만(`~/.gateway-cli` 에만 기록), 기존 설정 변경은 옵션 플래그 줄 때만

`근거: guides/QUICKSTART.md · scripts/onboard-{macos-linux.sh,windows.ps1}`

<!--
이 슬라이드는 발표를 "실행 가능"하게 만드는 마무리입니다. 슬라이드는 보여주기용이지만 개발자가 실제로 따라 하려면 복사할 수 있는 명령과 스크립트가 저장소에 있어야 합니다. 그래서 세 가지를 만들었습니다: (1) guides/QUICKSTART.md 는 세 클라이언트 × 두 OS 를 한 페이지에 담은 복붙용 빠른시작으로, 앞 슬라이드들의 명령을 그대로 옮겨 정확도를 맞췄습니다. (2) scripts/onboard-macos-linux.sh 와 (3) onboard-windows.ps1 은 공통 1단계(gateway-cli 설치+회사계정 로그인)를 자동화하되, 안전을 위해 기본 동작은 로그인(~/.gateway-cli 에만 기록)까지만이고 기존 Claude Code 설정 변경은 --setup-claude-code / -SetupClaudeCode 플래그를 명시할 때만 수행합니다 — 개발자의 기존 환경을 함부로 안 건드린다는 원칙입니다. 기존 문서(user-guide.md, connect.md)와 격리 컨테이너(gateway-clients/)도 링크로 안내합니다. 발표 시엔 "이 자료들이 레포에 있으니 발표 후 바로 따라 하시면 된다"로 닫으십시오. 근거: 소스 slide 46.
-->

---

<!-- _class: divider -->

## 11. 마무리 — 핵심 요약 + 정직한 다음 단계

relay 가 아니라 re-origination · 과장을 증거로 바꾸는 규율 — 5개 차별점과 아직 못 증명한 것

<!--
발표를 두 문장으로 닫는 챕터입니다. 우리는 받은 헤더·키를 그대로 뒤로 흘리는(relay=전달하는) 범용 프록시가 아니라, 사내 모든 LLM 요청이 반드시 지나며 통제가 강제되는 단일 관문(통제 평면)이고, 받은 요청을 허용목록만 골라 새로 만들어 보내는 re-origination(재구성 발신) 방식이라 애초에 새어나갈 표면 자체가 없습니다. 그 축 위에서 3-client 멀티계정·5개 거버넌스 레버·설정 없이 되는 서버사이드 웹서치를 단일 8단 파이프라인으로 강제합니다. 이번에 새로 강화한 것은 새 기능이 아니라 "견고하다"는 주장을 스스로 공격해(적대검증) 코드로 확정하고, 못 증명한 건 정직히 남긴 규율입니다. 청중이 기술의사결정자라면 여기서 신뢰를 얻습니다. 근거: 소스 slide 47.
-->

---

<style scoped>
h2 { font-size: 44px; }
</style>

## 5개 차별점 + 거버넌스, 모두 코드·라이브 근거가 붙는다

- **re-origination**(요청을 허용목록만 골라 새로 만듦): 본문·업스트림헤더·응답헤더 3곳 재구성 → 키가 샐 표면 자체가 없음
- **무효키 방어**: Redis 먼저 조회·DB 세션은 필요할 때만(lazy) → 가짜 키 폭주가 DB 무너뜨리는 경로 차단
- **멀티계정 민첩성**: `routing_profiles`(경로 규칙) DB 한 행만 바꾸면 → 재배포 없이 즉시 경로 변경·롤백(3계정 라이브)
- **서버사이드 웹서치**: 모델이 tool_use(검색 필요 신호) 내면 게이트웨이가 가로채 대신 검색 → 무설정·횟수집계·5회/90초 상한
- **견고성 + 거버넌스**: 6관점 적대검증·HIGH 2+MED 2 수정·283 유닛·rev75 라이브 / 5레버 실효 검증(403·404·429·미검색)

`근거: messages.py:79,164 · auth_service.py:45-76 · health.py:49-110`

<!--
요약 슬라이드는 각 주장에 반드시 근거가 붙는다는 우리 발표의 규율을 재확인하는 자리입니다. 다섯 차별점에 거버넌스를 더해 압축하되, 각 줄이 앞 챕터의 코드·라이브 근거로 이미 증명됐음을 상기시키십시오. 첫 줄은 re-origination(받은 요청을 그대로 넘기지 않고 허용목록만 골라 새로 만들어 발신) — 본문·업스트림 헤더·응답 헤더 세 곳을 다 새로 만드니 키가 샐 표면 자체가 없습니다(messages.py:79,164). 무효키 방어는 Redis 를 먼저 보고 DB 세션은 필요할 때만 짧게 여는(lazy) 설계라 가짜 키 폭주가 DB 폭발로 이어지는 경로를 끊습니다(auth_service.py:45-76). 멀티계정 민첩성은 routing_profiles(경로 규칙) DB 한 행만 바꾸면 재배포 없이 즉시 경로 변경·롤백입니다 — 333/123/222 3계정 라이브. 서버사이드 웹서치는 모델이 스스로 내는 tool_use(검색 필요 신호)를 게이트웨이가 가로채 대신 검색·재투입하므로 사용자는 무설정이고, 횟수 집계와 5회/90초 가드레일이 강제됩니다. 견고성은 6개 관점 적대검증으로 HIGH 2·MED 2 수정, 283개 유닛테스트, rev75 라이브 실증(health.py:49-110), 거버넌스는 5레버가 실제 효과(403 권한거부/404 모델비활성/429 한도초과/미검색)를 내는지 라이브로 확인했습니다. 이 슬라이드는 청중이 기억할 "한 장"이니, 질문이 나오면 각 줄의 근거 파일로 바로 이동해 코드를 열 수 있게 준비하십시오. 근거: 소스 slide 48.
-->

---

## 미검증은 미검증이라 말한다 — 정직한 다음 단계

- **최우선**: 가짜 키 수천 건 실제 부하 재현으로 DB 커넥션 풀·Redis·메모리 실측(모의실험 금지, 진짜 인프라로만)
- 순수 커넥션 풀 고갈 시 자가복구는 실부하 검증 전까지 미보증(현재는 특정 조건에서만 확인)
- 요청 동시처리 상한(`--limit-concurrency`): 스트리밍(SSE) 부하 A/B 후 적용 · 무제한 백그라운드 작업(auth.py:101): #4로 IP 메모리는 막았으나 개수 상한은 후속
- 비용 기록 버퍼(cost spool) 완전 영속화: 메모리 저장 → 서버 죽어도 안 잃는 저장으로
- 웹서치 검색어 관측: 개인정보 안전장치(fail-safe) 유지하며 집계 범위 확대 로드맵

`근거: tests/unit/test_readiness_probe.py · tests/unit/test_event_detector_bounded.py`

<!--
이 슬라이드가 정직성의 클라이맥스이자 발표의 마지막입니다. "견고하다"는 최종 판정을 아직 안 내렸다는 점을 분명히 하십시오. 최우선 남은 것은 가짜 키 수천 건을 실제로 쏘는 부하 재현으로 DB 커넥션 풀/Redis/메모리를 실측하는 것이며, 모의실험(mock)은 금지 — 진짜 인프라로만 증명합니다. 순수 커넥션 풀 고갈 시 자가복구는 그 실부하 검증 전까지 미보증입니다(현재는 특정 조건 — 헬스체크 실패·양쪽 인프라 등급 하락·풀 상한 소진 — 에서만 확인됨). 들어오는 요청 동시처리 상한(--limit-concurrency)과 상한 없는 백그라운드 작업 생성(auth.py:101)은 의도적으로 미수정이며 그 이유(스트리밍 오설정 위험, 앞선 수정 #4로 대부분 완화)를 정확히 밝힙니다. 비용 기록 버퍼(cost spool)를 서버가 죽어도 안 잃게 영속화하는 것과 웹서치 검색어 관측 확대(fail-safe=실패해도 개인정보를 노출하지 않는 안전장치 유지)는 로드맵 항목입니다. 마무리 멘트: "이번 발표의 성과는 새 기능의 양이 아니라, 우리 주장을 스스로 공격해 코드로 확정하고 못 증명한 것은 못 증명했다고 남긴 규율"이라고 닫으면 기술의사결정자에게 신뢰를 남깁니다. 근거: 소스 slide 49.
-->

---

<!-- _class: closing -->

# Thank You

## 요청을 전달하지 않고, 새로 만들어 보낸다

github.com/ren-ai-ssance/AWEsom-AI-Gateway-Proto · AWS
