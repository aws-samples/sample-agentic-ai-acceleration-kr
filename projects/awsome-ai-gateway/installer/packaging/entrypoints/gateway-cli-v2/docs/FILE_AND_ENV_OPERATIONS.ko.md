# 파일 및 환경 변수 작업 (File & environment-variable operations)

> 🇺🇸 English: [`FILE_AND_ENV_OPERATIONS.md`](./FILE_AND_ENV_OPERATIONS.md)

`gateway-cli` 가 전체 수명 주기 동안 명령별로 수행하는 **모든** 파일 및 OS
환경 변수 변경 작업입니다. 쓰기 경로를 변경하기 전에 이 문서를 먼저 읽으세요 —
이 문서는 도구가 사용자 머신에서 무엇을 건드리는지, 무엇이 복구 가능한지에 대한
단일 기준(single reference)입니다.

**설계 원칙 (Design rule):** `gateway-cli` 는 **자신의** 설정 파일(Claude Code
설정, 자체 토큰 캐시)을 관리하고, 자신의 운영자용 환경 변수 4개를 *User* 범위에
**추가**합니다. 사용자의 시스템/OS 환경 변수를 제거하지 **않습니다**. 게이트웨이를
우회하는 OS 환경 변수(`CLAUDE_CODE_USE_BEDROCK`, `ANTHROPIC_API_KEY`,
`ANTHROPIC_BEDROCK_BASE_URL`)는 `verify` 가 **탐지하고 보고**하며, User 및
System(관리자) 범위 모두에 대한 단계별 제거 안내를 출력합니다 — 사용자가 직접
제거합니다.

> **이 원칙과 현재 코드 사이의 알려진 차이 (Known gaps)** (각 항목은 아래 해당
> 섹션에 상세히 기술되어 있으며, 이 문서에 대한 적대적 검토(adversarial review)로
> 드러났습니다):
> 1. ~~`verify` 의 POSIX 프로파일 스캔이 Linux/WSL 시스템 파일을 다루지 않음~~ —
>    **수정됨**: 스캔이 이제 Linux/WSL 의 `/etc/*`, `/etc/environment`,
>    `/etc/profile.d/*.sh` 와 fish `set -x` 구문을 다룹니다 (Step 3).
> 2. `disable` 은 `managed-settings.json` 의 **백업을 만들지 않으며**, 우리 키
>    중 하나를 차지했던 이전 조직(org) 값을 복원할 수 없습니다 (기타 명령).
> 3. Windows 환경 변수 영속화는 기존 `HKCU` 값을 **덮어씁니다**(스냅샷 없음);
>    POSIX 셸 rc 쓰기만 진정으로 추가(additive) 방식입니다 (Step 2C).

범례 (Legend):

| 기호 | 의미 |
|---|---|
| ✏️ | 쓰기 / 생성 (write / create) |
| 🔁 | 덮어쓰기 또는 병합 (overwrite or merge) |
| 🗑️ | 제거 (remove) |
| 💾 | 먼저 백업 (타임스탬프 기록, 덮어쓰지 않음) |
| 🌐 | 프로세스 내 환경 변수만 (이 프로세스; **영속화 안 됨**) |

---

## 주요 위치 (Key locations)

| 항목 | 경로 |
|---|---|
| **데이터 디렉터리** (`<data_dir>`) | macOS `~/Library/Application Support/gateway-cli/` · Linux `~/.local/share/gateway-cli/` · Windows `%LOCALAPPDATA%\gateway-cli\`. 재정의: `GATEWAY_CLI_DATA_DIR`. |
| **백업 디렉터리** | `<data_dir>/backups/` (모드 `0700`). 재정의: `GATEWAY_CLI_BACKUP_DIR`. |
| **관리형 설정 루트 (Managed settings root)** | Windows `C:\Program Files\ClaudeCode\` · macOS `/Library/Application Support/ClaudeCode/` · Linux/WSL-native `/etc/claude-code/` · WSL→Windows Claude `/mnt/c/Program Files/ClaudeCode/`. |
| **사용자 설정 (User settings)** | `~/.claude/settings.json`. 재정의: `GATEWAY_CLI_SETTINGS_PATH`. |
| **토큰 / VK 캐시** | `<data_dir>/oidc-tokens.json`, `<data_dir>/vk-cache.json`. 재정의: `GATEWAY_CLI_OIDC_CACHE`, `GATEWAY_CLI_VK_CACHE`. |

---

## 공통 사항 — 모든 명령, 시작 시 (Cross-cutting — every command, at startup)

`cli/main.py` + `gateway_cli_oidc/tls.py`, 모든 하위 명령 실행 전.

| 대상 | 작업 | 상세 |
|---|---|---|
| `REQUESTS_CA_BUNDLE`, `SSL_CERT_FILE`, `AWS_CA_BUNDLE`, `CURL_CA_BUNDLE` | 🌐 ✏️ (`setdefault`) | `apply_ca_bundle()` — **로컬에 회사 PEM 이 존재할 때만** 설정됨; 사용자가 이미 설정한 값을 절대 덮어쓰지 않음. 프로세스 내에서만, 영속화 안 됨. |
| `PYTHONUTF8=1` (Windows 전용) | 🌐 ✏️ (`setdefault`) | em-대시/화살표/체크마크가 올바르게 표시되도록 UTF-8 I/O 를 강제함. 프로세스 내에서만. |
| `ssl.SSLContext` (런타임 몽키패치) | — | `enable_os_trust_store()` 가 `truststore` 를 프로세스 전역에 주입함. 파일이나 환경 변수 쓰기 없음. |

---

## Step 1 — `login` (OIDC PKCE, 또는 헤드리스 이메일+비밀번호)

`cli/login.py`

| 대상 | 작업 | 상세 |
|---|---|---|
| `<data_dir>/oidc-tokens.json` | 🔁 | `save_tokens()` — access / refresh / id 토큰. `chmod 0600`. 로그인마다 덮어씀. |
| `<data_dir>/vk-cache.json` | 🔁 | `save_vk_cache()` — `/v1/auth/exchange` 로부터 받은 Virtual Key. `chmod 0600`. |

- **백업 없음** (캐시는 폐기 가능/재생성 가능).
- 환경 변수를 건드리지 않음.

---

## Step 2 — `setup` (쓰기 집약적)

### A. 관리형 설정 (최상위 계층) — `cli/managed.py`

| 대상 | 작업 | 상세 |
|---|---|---|
| `…/ClaudeCode/managed-settings.json` | 💾 → 🔁 | 이미 존재했으면 백업(`claude-code-managed`) 후 **딥 머지(deep-merge)**: 우리 키가 우선, 조직 키는 보존, 우리가 소유한 것을 기록하는 비공개 `_gatewayCli` 마커 추가. 관리자/sudo 필요. |
| `…/managed-settings.d/50-gateway.json` | 💾 → 🔁 | 우리 드롭인 조각(기본 파일보다 우선). 존재하면 백업(`claude-code-managed-dropin`) 후 덮어씀. |
| `…/managed-settings.d/99-gateway.json` (레거시) | 💾 → 🗑️ | 이전 빌드의 오래된 조각 — 백업 후, **우리 마커가 있을 때만** 제거. |

POSIX 쓰기는 `sudo tee` + `chmod 644` + `chown root` 를 거칩니다.

관리형 `env` 블록에 기록되는 키 (`build_gateway_env` 경유):
`ANTHROPIC_BASE_URL`, `GATEWAY_CLI_GATEWAY_URL`, `NO_PROXY`, 모든 `OTEL_*`
텔레메트리 키, 그리고 — CA 번들이 구성된 경우 — `NODE_EXTRA_CA_CERTS`,
`REQUESTS_CA_BUNDLE`, `AWS_CA_BUNDLE`, `SSL_CERT_FILE`. 더하여 최상위
`apiKeyHelper` 및 `statusLine`. (OTEL 엔드포인트 우선순위:
[`OTEL_PRECEDENCE.md`](../OTEL_PRECEDENCE.md) 참조.)

### B. 사용자 설정 — `cli/setup.py`

| 대상 | 작업 | 상세 |
|---|---|---|
| `~/.claude/settings.json` | 💾 → 🔁 | 존재하면 백업(`claude-code`) 후 병합. |

해당 파일 내부:

- ✏️/🔁 **기록됨:** 최상위 `apiKeyHelper`, `model`, `availableModels`(제공 시);
  `env.ANTHROPIC_BASE_URL`, `env.ADMIN_API_URL`,
  `env.GATEWAY_CLI_GATEWAY_URL`, `env.OIDC_ISSUER_URL`, `env.OIDC_CLIENT_ID`;
  선택적으로 `env.ANTHROPIC_DEFAULT_{SONNET,OPUS,HAIKU}_MODEL`;
  `env.NODE_EXTRA_CA_CERTS`(setdefault); 로그인한 신원에 맞춰 조정되는
  `env.OTEL_RESOURCE_ATTRIBUTES` 의 `user.id` 세그먼트.
- 🗑️ **이 파일에서 제거됨** (`reconcile_settings` 경유 — `cli/reconcile.py` 의
  단일 기준): `env.CLAUDE_CODE_USE_BEDROCK`,
  `env.ANTHROPIC_BEDROCK_BASE_URL`, `env.ANTHROPIC_API_KEY`, 최상위
  `statusLine`, 최상위 `modelOverrides`. 위의 타임스탬프 백업이 이를 되돌릴 수
  있게 합니다. **settings.json 사본만 제거되며 — 동일한 이름의 OS/시스템 환경
  변수는 절대 제거되지 않습니다.**

### C. 영속화되는 OS 환경 변수 — `cli/env.py`

기본으로 켜져 있음; `--no-persist-env` 로 비활성화.

| 대상 | 작업 | 상세 |
|---|---|---|
| Windows `HKCU\Environment` (User 범위) | 🔁 | `persist_env_vars()` → `_persist_windows()` 가 `OIDC_ISSUER_URL`, `OIDC_CLIENT_ID`, `ADMIN_API_URL`, `ANTHROPIC_BASE_URL` 을 `winreg.SetValueEx` 로 `REG_SZ` 로 기록. ⚠️ 이는 **무조건적 교체**입니다: 쓰기 전 읽기(read-before-write)가 없으므로, 이 네 이름 중 하나에 대한 기존 User 값은 **덮어써져 소실**됩니다(복구할 스냅샷 없음). |
| `~/.zshrc` **또는** `~/.bashrc` (POSIX) | ✏️ | 멱등적(idempotent) `export` 블록 추가; 이미 존재하는 변수는 건너뜀. 기존 줄은 절대 덮어쓰지 않음. |

- **User 범위만.** setup 은 이 네 변수를 **User** 범위에 기록하며,
  System/Machine 범위에는 아무것도 기록하지 않습니다.
- **POSIX 에서는 추가, Windows 에서는 교체.** 셸 rc 쓰기는 이미 존재하는 변수를
  건너뛰지만; Windows 레지스트리 쓰기는 그렇지 않습니다 — 네 이름에 대한 이전
  값을 덮어씁니다. 이는 no-overwrite 설계 원칙에 대한 알려진 차이입니다
  (Windows 영속화는 기존 값을 백업하거나 건너뛰어야 하지만 그렇지 않습니다).
- 레지스트리 / 셸 rc 의 **스냅샷을 만들지 않습니다.** POSIX 에서는 안전하며(쓰기가
  추가 방식); Windows 에서는 덮어쓴 이전 값을 복구할 수 없습니다.
- setup 은 게이트웨이 우회 OS 환경 변수를 **제거하지 않습니다** — 그것은
  `verify`(Step 3)가 수동 안내로 드러냅니다.

### D. 이후 단계를 위한 상태 영속화 — `cli/main.py`

| 대상 | 작업 | 상세 |
|---|---|---|
| _(없음)_ | — | **없음.** setup 은 이후 단계를 위한 설정을 영속화하지 않습니다. 엔드포인트는 빌드 시 내장된(build-time-baked) 기본값(`cli/site_defaults.py`)에서 단일 소싱(single-sourced)되므로, `login`/`verify` 는 저장된 상태 없이 `setup` 이 사용한 동일한 값을 해석합니다. |

- **setup → login/verify 를 잇는 설정 파일 없음.** 프로덕션 빌드는 엔드포인트를
  내장하며; 스테이징/개발 테스트는 `GATEWAY_CLI_*` 환경 변수로 재정의하고, 이는
  파일 없이 셸의 명령 간에 유지됩니다.

---

## Step 3 — `verify`

`cli/verify.py`

**읽기 전용.** 프로세스 환경, `settings.json` env 블록, 셸 프로파일(POSIX),
Windows 레지스트리(User + Machine 범위)를 스캔합니다. **OS/시스템 환경**에서
발견된 게이트웨이 우회 변수에 대해 다중 계층 수동 제거 안내를 출력합니다:

- **Windows** — `[Environment]::SetEnvironmentVariable(..., $null, 'User')` 및
  `...'Machine'`(관리자) 명령, `sysdm.cpl` GUI 경로, 탐지된 범위, 그리고 새
  PowerShell 을 열라는 알림.
- **POSIX/WSL** — export 를 담고 있는 정확한 프로파일 파일 + 줄(또는 이를 찾는
  `grep`), `/etc/*` 파일에 대한 `sudo` 참고, 재시작/`source` 알림.

> **프로파일 스캔의 범위** (`_shell_profile_paths()` /
> `_scan_shell_profiles()`):
> - **스캔되는 사용자 파일:** `~/.bashrc`, `~/.bash_profile`, `~/.profile`,
>   `~/.zshrc`, `~/.zprofile`, `~/.config/fish/config.fish`.
> - **macOS *및* Linux/WSL 에서 스캔되는 시스템 파일:** `/etc/zshrc`,
>   `/etc/zshenv`, `/etc/profile`, `/etc/bashrc`, `/etc/bash.bashrc`,
>   `/etc/environment`(PAM `KEY=VALUE` 형식), 그리고 모든 `/etc/profile.d/*.sh`
>   드롭인. (이전에는 macOS 에서만 스캔되어 — Linux/WSL 의 시스템 파일에서
>   export 된 우회 변수가 `verify` 를 무사히 통과했습니다.)
> - **매칭되는 구문:** `export VAR=` / 단순 `VAR=`(`/etc/environment` 포함), 그리고
>   `config.fish` 의 fish `set -x` / `--export` / `-gx` 형식.
>
> 프로세스-환경 검사는 `verify` 를 실행하는 셸에 export 된 변수라면 여전히
> 잡아냅니다; 파일 스캔은 추가로, 새 Claude 프로세스가 상속받겠지만 현재 `verify`
> 셸에는 없는 영속적 정의를 잡아냅니다.

**아무것도 수정하지 않습니다.**

---

## 기타 명령 (Other commands)

| 명령 | 대상 | 작업 | 상세 |
|---|---|---|---|
| `disable` | `managed-settings.json` | 🔁 / 🗑️ | 우리 마커가 붙은 키만 언머지(`_gatewayCli` 마커 기반)한 뒤 축소된 파일을 다시 씀; gateway-cli 가 생성한 경우에만(`fileExisted=false`) **파일을 삭제**. 제거 전에 파일의 타임스탬프 백업을 먼저 스냅샷하므로(`_backup_existing`) 파일-삭제 분기에서도 되돌릴 수 있습니다. 마커는 소유한 키 *이름*만 기록하고 이전 값은 기록하지 않으므로, 조직 값이 우리 키 중 하나를 차지했었다면 복원되지 않고 제거(pop)됩니다(스냅샷에서 복구 가능). |
| `disable` | `50-gateway.json`, 레거시 `99-gateway.json` | 🗑️ | 우리 드롭인의 마커 기반 제거(먼저 백업, 라벨 `claude-code-managed-dropin`). |
| `logout` | `oidc-tokens.json`, `vk-cache.json` | 🗑️ | `clear_tokens()` 가 둘 다 unlink. |
| `env --persist` | `HKCU\Environment` / 셸 rc | ✏️/🔁 | Step 2C 와 동일한 추가 방식 쓰기. Windows 에서는 덮어쓰기 직전의 기존 HKCU 값을 먼저 스냅샷합니다(`gateway-cli-hkcu-env.Environment.<ts>.json.bak`). |
| `env` (플래그 없음) | — | — | 값 + 셸 스니펫 출력. 읽기 전용. |
| `status` | — | — | 읽기 전용. |
| `clear` | 위 전부 + 백업 | 🔁 / 🗑️ | 소프트웨어 수준 teardown, 순서대로: managed settings(= `disable`) → `~/.claude/settings.json` 의 소유 키(**가장 이른** `claude-code` 스냅샷에서 사전 setup 값 복원) → 영속화된 OS 환경 변수(HKCU 는 가장 이른 `gateway-cli-hkcu-env` 스냅샷 기준 복원-또는-삭제; POSIX 는 마커 블록 라인 제거) → 토큰/VK(= `logout`) → 이 도구의 백업 스냅샷 sweep(엄격히 마지막). 비승격 실행; managed-settings 권한 오류 시 사용자 범위 단계는 완료하고 관리자 셸용 `gateway-cli disable` 한 줄을 출력합니다. `--keep-tokens` / `--keep-os-env` / `--dry-run` / `--yes`. |
| `uninstall` | 바이너리, PATH, ARP 키 | 🗑️ (위임) | Windows 전용. Add/Remove Programs 에서 `unins000.exe` 를 해석(GUID 부분 문자열 + `_is1` — Inno 의 `}}_is1` 이중 중괄호 특성 허용)하고 검증(`GatewayCLI\` 내부의 순수 `unins###.exe` 경로, DisplayName 일치)한 뒤 승격 + 분리 실행. 자신의 이미지는 절대 삭제하지 않습니다. `--clear-first` 는 사전에 `clear` 를 인프로세스로 실행. |
| `verify --post-teardown` | — | — | 읽기 전용 역방향 게이트: managed settings, settings.json 소유 키, 영속화된 OS 환경 변수, 토큰 캐시, 우리 백업이 모두 제거/복원되었는지 검증; 잔여물이 있으면 1 로 종료. |

---

## 백업 — 복구 스냅샷이 저장되는 곳 (Backups)

모든 **설정(config)** 백업은 `<data_dir>/backups/`(모드 `0700`)에 타임스탬프와
함께 저장되며 **절대 덮어쓰지 않습니다**(같은 초 충돌 시 숫자 접미사 추가):

- `claude-code.settings.json.<ts>.bak`
- `claude-code-managed.managed-settings.json.<ts>.bak`
- `claude-code-managed-dropin.50-gateway.json.<ts>.bak`
- `gateway-cli-hkcu-env.Environment.<ts>.json.bak` — Windows 환경 변수 영속화가
  덮어쓰기 직전에 기록한 기존 HKCU 값(T6.2 read-before-write). `clear` 는 이 중
  **가장 이른** 스냅샷(진짜 사전 setup 상태)에서 복원합니다.

**백업되지 않는 것:**

- 토큰 / VK 캐시 (폐기 가능 — `login` 이 재생성).
- Step 2C 의 POSIX OS 환경 변수 영속화 쓰기 — 추가 방식(존재하면 건너뜀)이라
  스냅샷이 불필요; `clear` 는 대신 마커 블록 라인을 제거합니다.

`gateway-cli clear` 는 마지막 단계로 위 스냅샷들을 모두 삭제합니다(소유 접두사
allowlist + 디렉터리 이탈 가드 — 공유된 `GATEWAY_CLI_BACKUP_DIR` 에서 다른 도구의
파일은 보존).

> 이력 참고: 이전 리비전에서는 `setup` 이 게이트웨이 우회 OS 환경 변수를 삭제하고
> `registry-env.<ts>.json` 으로 스냅샷했습니다. 그 방향은 되돌려졌습니다 —
> `setup` 은 더 이상 OS 환경 변수를 건드리지 않으므로, 해당 스냅샷 파일은 더 이상
> 생성되지 않습니다.
