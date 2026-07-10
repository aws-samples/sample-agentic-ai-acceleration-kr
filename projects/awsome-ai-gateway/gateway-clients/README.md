# gateway-clients — 로컬 격리 컨테이너로 LLM Gateway 사용

호스트 맥 환경(`~/.claude`, `~/.codex`, 셸 설정)을 **전혀 건드리지 않고**, 격리 컨테이너에서
Claude Code / Codex 를 LLM Gateway 로 붙여 쓴다. 사용량·비용은 게이트웨이가 집계(앱별 client 구분).

> **Cowork 는 제외** — Cowork 는 macOS 데스크톱 GUI 앱이라 컨테이너(헤드리스)에서 실행 불가.
> 컨테이너로 되는 건 CLI 인 **claude-code** 와 **codex** 둘.

## 구성
```
gateway-clients/
  claude-box/Dockerfile   # node20 + @anthropic-ai/claude-code
  codex-box/Dockerfile    # node20 + @openai/codex (+ entrypoint 가 config.toml 생성)
  codex-box/entrypoint.sh # GW_URL/GATEWAY_VK → ~/.codex/config.toml (wire_api=responses)
  gw.sh                   # 빌드·VK발급·실행 헬퍼
```

## 빠른 시작
```bash
cd gateway-clients

# 1) 이미지 빌드 (최초 1회)
./gw.sh build

# 2) VK 발급 (호스트 브라우저로 OIDC 로그인 → ~/.gateway-vk 저장, 1시간 유효)
./gw.sh vk
#   gateway-cli 가 호스트에 없으면, 발급받은 VK 문자열을 직접 저장도 가능:
#   printf '%s' '<VK>' > ~/.gateway-vk && chmod 600 ~/.gateway-vk

# 3) 사용 (현재 디렉터리가 /work 로 마운트됨)
./gw.sh claude "이 코드 리뷰해줘"
./gw.sh codex  "버그 고쳐줘"

# 디버그 셸
./gw.sh shell claude
./gw.sh shell codex
```

## 동작 원리
- **claude-box**: `ANTHROPIC_BASE_URL=<게이트웨이>` + `ANTHROPIC_AUTH_TOKEN=<VK>` 를 env 로 주입 →
  Claude Code 가 `/v1/messages` 를 게이트웨이로 호출. 게이트웨이가 `client=claude-code` 로 식별.
- **codex-box**: entrypoint 가 `~/.codex/config.toml` 을 생성(`base_url=<게이트웨이>/v1`,
  `wire_api="responses"`, `env_key=GATEWAY_VK`) → Codex 가 `/v1/responses` 로 GPT-5.5 호출.
  Codex 가 보내는 `originator: codex_cli_rs` 헤더로 게이트웨이가 `client=codex` 식별.
- **VK**: 호스트 `~/.gateway-vk`(plain 문자열, 600)에서 읽어 컨테이너에 env 주입. 호스트 환경 무변경.
  1시간 만료 → `./gw.sh vk` 재실행.

## 호스트 환경 보존 보장
- 이미지에 VK·자격증명을 굽지 않음(런타임 env 만).
- `~/.claude`, `~/.codex` 같은 호스트 설정 미접근(컨테이너 내부에만 생성).
- 컨테이너는 `--rm` 으로 매번 폐기, `$PWD` 만 `/work` 로 마운트.

## 주의
- 게이트웨이 dev ALB 는 HTTP(80) — 평문. dev 전용. (운영은 HTTPS 필요)
- codex 가 게이트웨이에서 거부(403 Model not allowed)되면, 해당 user/team 의 allowed_models 에
  `codex-gpt` 추가 필요(admin UI 사용자/팀 또는 모델 권한). claude-code 도 동일 원리.
- `GW_URL`/`ADMIN_URL` 은 `gw.sh` 상단에서 환경변수로 override 가능.
