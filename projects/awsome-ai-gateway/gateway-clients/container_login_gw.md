# 컨테이너로 LLM Gateway 붙어서 Claude Code / Codex 쓰기 — 따라하기

호스트 맥 환경을 **전혀 안 건드리고**, 격리 컨테이너에서 게이트웨이로 붙어
Claude Code 와 Codex 를 쓰는 전체 순서. 위에서부터 한 줄씩 복붙하면 된다.

> **Cowork 제외**: Cowork 는 macOS 데스크톱 GUI 앱이라 컨테이너(헤드리스) 불가. 여기선 CLI 인
> claude-code / codex 둘만 다룬다. (Cowork 는 데스크톱 앱 config 방식 — `docs/guides/COWORK-GATEWAY-SETUP.md` 참고)

---

## 0. 사전 준비 (최초 1회)

### 0-1. 컨테이너 런타임 켜기 (docker 또는 finch)
```bash
# colima(docker) 사용 시
colima start --cpu 2 --memory 4
docker ps    # 동작 확인

# 또는 finch 사용 시
finch vm start
```

### 0-2. 이 폴더로 이동
```bash
cd <repo>/gateway-clients
```

### 0-3. 게이트웨이 주소 확인 (이미 gw.sh 에 dev ALB 가 기본값으로 박혀 있음)
```bash
# 기본값(dev): http://<ALB_DNS>
# 바꾸려면: export GW_URL="http://<게이트웨이 ALB>"  /  export ADMIN_URL="http://<admin-api ALB>"
```

---

## 1. 컨테이너 이미지 빌드 (최초 1회, ~2분)
```bash
./gw.sh build
```
- `claude-box`(claude-code), `codex-box`(codex) 두 이미지가 만들어진다.
- 확인: `docker images | grep -E 'claude-box|codex-box'`

---

## 2. 게이트웨이 로그인 = VK(Virtual Key) 발급

게이트웨이는 `Authorization: Bearer <VK>` 로 인증한다. VK 를 `~/.gateway-vk` 에 저장해두면
컨테이너가 자동으로 읽어 쓴다. **VK 는 1시간 만료** → 만료되면 이 단계만 다시 한다.

### 방법 A — 정식 (OIDC 로그인, 내 실제 사용량으로 집계) [권장]
호스트 맥에서 브라우저 Cognito 로그인. `gateway-cli` 가 호스트에 설치돼 있어야 한다.
```bash
./gw.sh vk
# → 브라우저가 열리며 Cognito 로그인 → VK 가 ~/.gateway-vk 에 저장됨
```
> `gateway-cli` 미설치 시: `uv tool install --from <repo>/gateway-cli gateway-cli` 후 재실행.

### 방법 B — dev 테스트 헬퍼 (브라우저 없이, 가장 빠름 / 테스트 user 로 집계)
dev 환경 전용. 인증 없이 테스트 VK 를 받아 `~/.gateway-vk` 에 저장한다.
```bash
ADMIN_URL="http://<ALB_DNS>"
VK=$(curl -s -X POST "$ADMIN_URL/internal/test/issue-key" \
      -H "content-type: application/json" -d '{}' \
      | python3 -c "import sys,json; print(json.load(sys.stdin)['virtual_key'])")
printf '%s' "$VK" > ~/.gateway-vk && chmod 600 ~/.gateway-vk
echo "VK 저장 완료. 길이: ${#VK}"      # 값 자체는 출력 안 함(노출 방지)
```

> ⚠️ VK 는 **plain 문자열로 `~/.gateway-vk`(권한 600)** 에만 저장된다. 호스트의 ~/.claude, ~/.codex,
> 셸 설정은 전혀 안 건드린다.

---

## 3. 사용하기

현재 디렉터리(`$PWD`)가 컨테이너 안 `/work` 로 마운트된다. 작업할 코드 폴더에서 실행하면 된다.

### 3-1. Claude Code
```bash
cd ~/my-project           # 작업할 폴더로 이동
<repo>/gateway-clients/gw.sh claude "이 코드 리뷰해줘"
# 인터랙티브로 쓰려면 인자 없이:
<repo>/gateway-clients/gw.sh claude
```
- 게이트웨이 `/v1/messages` 로 Claude 호출 → 게이트웨이가 `client=claude-code` 로 집계.

### 3-2. Codex
```bash
cd ~/my-project
<repo>/gateway-clients/gw.sh codex "버그 고쳐줘"
# 인터랙티브:
<repo>/gateway-clients/gw.sh codex
```
- 게이트웨이 `/v1/responses`(GPT-5.5, Responses API) 로 호출 → `client=codex` 로 집계.
- entrypoint 가 컨테이너 안에 `~/.codex/config.toml`(base_url=게이트웨이, wire_api=responses)을 자동 생성.

### 3-3. 디버그용 셸 (컨테이너 안 직접 진입)
```bash
./gw.sh shell claude     # claude-box 안 bash (ANTHROPIC_BASE_URL/VK 주입된 상태)
./gw.sh shell codex      # codex-box 안 sh (config.toml 생성된 상태)
```

---

## 4. 자주 겪는 문제

| 증상 | 원인 | 해결 |
|---|---|---|
| `VK 없음. 먼저 './gw.sh vk' 실행` | VK 미발급 | 2단계 수행 |
| 401 / `auth_failed` / "API key rejected" | VK 1시간 만료 | 2단계 재실행 |
| 403 `Model not allowed` | 해당 user/team 의 allowed_models 에 모델 없음 | Admin UI(사용자/팀 또는 모델 권한)에서 `codex-gpt`(codex) / 사용할 claude alias 추가 |
| `docker: ... not found` | 런타임 미기동 | `colima start` 또는 `finch vm start` |
| codex 가 응답 짧게 잘림 | max_output_tokens 가 reasoning 토큰에 먹힘 | 프롬프트에서 출력 길이 늘리기(기본은 충분) |

---

## 5. 한눈에 보는 전체 흐름 (복붙용)
```bash
# 최초 1회
colima start --cpu 2 --memory 4
cd <repo>/gateway-clients && ./gw.sh build

# 매 세션 (VK 1시간 만료마다 vk 만 다시)
./gw.sh vk                                  # 로그인(VK 발급)
cd ~/my-project
<repo>/gateway-clients/gw.sh claude "..."   # Claude Code
<repo>/gateway-clients/gw.sh codex  "..."   # Codex
```

> 편의: `alias gwclaude='<repo>/gateway-clients/gw.sh claude'`,
> `alias gwcodex='<repo>/gateway-clients/gw.sh codex'` 를 **호스트가 아닌** 임시 셸에만 두고 싶으면
> 그 터미널에서만 export. (호스트 영구 설정 변경 원치 않으면 ~/.zshrc 에 넣지 말 것)

## 검증 기록 (859 dev)
- claude-box → `/v1/messages` → Claude **HTTP 200** ("BOX-OK received")
- codex-box → `/v1/responses` → GPT-5.5 **HTTP 200** (reasoning_tokens 포함)
- VK 는 호스트 `~/.gateway-vk` 주입 → 호스트 환경 무변경 확인.
