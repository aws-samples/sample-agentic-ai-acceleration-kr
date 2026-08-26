# gateway-cli-v2 — Windows 설치 & 빌드

`gateway-cli-v2`를 **단일 자립형 Windows 설치 파일**로 만들어, Python이 설치되지
않은 **폐쇄망(air-gapped) Windows** 사용자에게 배포합니다. PyInstaller가 CPython
3.11+ 런타임과 모든 의존성(`click`, `boto3`, `requests`, `structlog`, `PyYAML` …)을
번들에 포함하므로, 대상 PC에는 **Windows x64 외에 아무것도 필요 없습니다.**

대상 독자 두 부류:
- **[사용자용](#사용자용)** — `.exe`를 설치하고 `gateway-cli setup` 실행.
- **[유지보수자용](#유지보수자용)** — 설치 파일을 빌드하고 사내 엔드포인트를 내장.

3개 CLI(`gateway-cli.exe`, `api-key-helper.exe`, `statusline.exe`)는 `_internal/`
Python 런타임 하나를 공유합니다 — 설치 파일이 작고, 실행이 빠르며, 실행 시마다 temp에
압축을 푸는 문제가 없어(엔드포인트 보안 SW와도 충돌이 적음) 유리합니다.

---

## 사용자용

단일 파일 하나를 전달받습니다: **`gateway-cli-setup-<version>.exe`** (예: 바탕화면).
Python 등 사전 요구사항은 없습니다.

### 1. 설치

```powershell
# 대화형: .exe 더블클릭
# 무인 설치 (SCCM / Intune / GPO):
gateway-cli-setup-0.1.0.exe /VERYSILENT /NORESTART
```

- 관리자는 `C:\Program Files\GatewayCLI`에, 비관리자는 사용자별 설치를 선택할 수 있습니다.
- "Add to PATH"(기본 on) → **새 터미널**에서 `gateway-cli` / `api-key-helper` /
  `statusline` 사용 가능.

### 2. Claude Code 설정

**관리자 권한 새 터미널**을 엽니다(`setup`은 시스템 전역 `managed-settings.json`을 씀):

```powershell
gateway-cli login                              # OIDC 브라우저 로그인
gateway-cli setup --model claude-sonnet-4-6    # 게이트웨이 설정 적용, 모델 지정
claude                                         # 바로 사용
```

보통 `--model`만 고르면 됩니다 — 사내 엔드포인트(게이트웨이 / 관리 API / OIDC)는 빌드에
내장되어 있기 때문입니다. 내장되지 **않은** 범용 빌드라면 `setup`이 값을 직접 넘기라고
요청합니다: `--gateway-url … --admin-api-url … --oidc-issuer-url … --oidc-client-id …`.

선택 사항:

```powershell
gateway-cli setup --available-models claude-sonnet-4-6,claude-haiku-4-5,claude-opus-4-6
gateway-cli verify        # 상태 / 설정 점검
gateway-cli env           # 실제 적용된 환경 표시
```

### 3. 제거 / 업그레이드

- **제거:** *앱 및 기능* → "LLM Gateway CLI". PATH도 자동 정리됩니다. Claude Code 설정까지
  먼저 되돌리려면 `gateway-cli clear`(비관리자, 사용자 범위) 후 `gateway-cli disable`(관리자).
- **업그레이드:** 새 `setup.exe`를 덮어 실행 — `AppId`가 동일해 같은 제품으로 취급됩니다.

---

## 유지보수자용

### 빌드 머신 요구사항

PyInstaller는 크로스컴파일이 안 되므로 **Windows x64**(VM/CI 가능)에서 빌드합니다.

1. **Python 3.11+** (`entrypoints/gateway-cli-v2/pyproject.toml`과 일치).
2. **Inno Setup 6** — https://jrsoftware.org/isdl.php
   (선택: `-SkipInstaller`로 `dist\gateway-cli-suite` 폴더/zip 배포).
3. `entrypoints/gateway-cli-v2/src`가 포함된 저장소 체크아웃.

### 1. 사내 엔드포인트 내장 (중요)

**기본(bare) 빌드는 모델 기본값(`claude-sonnet-4-6`) 외에 아무것도 내장하지 않습니다.**
사용자가 바로 쓸 수 있는 빌드를 만들려면 아래 중 하나로 사내 값을 공급하세요(우선순위 높은 순):

```
-Param  >  환경변수 GATEWAY_CLI_DEFAULT_*  >  packaging\site-config.json  >  (빈 값)
```

**가장 쉬운 방법 — `packaging\site-config.json` 편집** (백슬래시는 `\\`로 이스케이프):

```json
{
  "oidcIssuerUrl": "https://<issuer>/oauth2/default",
  "oidcClientId":  "<client-id>",
  "gatewayUrl":    "https://gateway.example.com",
  "adminApiUrl":   "https://api.gateway.example.com",
  "caBundle":      "C:\\corp-proxy-ca.pem"
}
```

**또는 빌드 파라미터로 전달**(JSON보다 우선):

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 `
    -OidcIssuerUrl https://<issuer>/oauth2/default -OidcClientId <client-id> `
    -GatewayUrl https://gateway.example.com -AdminApiUrl https://api.gateway.example.com `
    -CaBundle C:\corp-proxy-ca.pem
```

참고:
- `site-config.json`은 **5개 키만** 매핑합니다(`oidcIssuerUrl`, `oidcClientId`,
  `gatewayUrl`, `adminApiUrl`, `caBundle`). 3개 **프록시** 값
  (`-ExpectedProxyUrl`, `-NoProxyValue`, `-ForbiddenNoProxyToken`)은 `-Param` 또는
  `GATEWAY_CLI_DEFAULT_*` 환경변수로**만** 설정할 수 있습니다.
- 빈 값은 그대로 빈 값 — 해당 Claude Code 키는 아예 **기록되지 않습니다**
  (빈 값 ⇒ `KEY=""`가 아니라 키 자체가 없음).
- `site-config.json`은 `.gitignore` 처리됨 — 환경별 식별자를 **커밋 금지**.
- 모든 설정 항목과 출처·기본 빌드 동작 전체 카탈로그:
  **`entrypoints/gateway-cli-v2/docs/CONFIG_ITEMS_AND_DEFAULTS.md`**.

### 2. 빌드

저장소 루트에서 (PowerShell):

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

`.build-venv` 생성 → 프로젝트 pip 설치(poetry-core 백엔드를 pip가 직접 처리, Poetry
불필요) → PyInstaller → 각 exe `--help` smoke test → 설치 파일 컴파일 순으로 진행됩니다.
결과물:

```
dist\
├── gateway-cli-suite\                     # PyInstaller onedir (3개 exe + 공유 _internal\)
└── installer\
    └── gateway-cli-setup-<version>.exe    # 단일 오프라인 설치 파일
```

버전은 `pyproject.toml` 값을 사용하며 `-Version`으로 재정의할 수 있습니다.

### 3. 빌드 머신도 오프라인인 경우

동일한 Windows/Python 버전의 연결된 머신에서 wheel 캐시를 만들어 저장소와 함께 옮긴 뒤,
`build.ps1`에 캐시 경로를 지정합니다:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\download_wheels.ps1 -OutDir C:\wheels
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -WheelDir C:\wheels
```

### 4. 코드 서명 (프로덕션 필수)

미서명 PyInstaller exe는 AV/SmartScreen 오탐 대상입니다(다운로드/PATH 단계를 막을 수 있음).
자격증명을 제공하면 `build.ps1`이 3개 exe(ISCC 전)와 setup.exe(ISCC 후)를 Authenticode
(SHA-256 + RFC-3161 타임스탬프)로 서명하고 `signtool verify /pa`로 검증합니다:

```powershell
# 사내 표준: 인증서 저장소 / HSM / 토큰의 SHA-1 thumbprint
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -SignThumbprint <THUMBPRINT>

# 또는 PFX 파일 + 암호 (dev / 비-HSM 인증서)
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -SignPfxFile C:\certs\corp.pfx -SignPfxPassword <pw>
```

환경변수: `GATEWAY_CLI_SIGN_THUMBPRINT` / `GATEWAY_CLI_SIGN_PFX` /
`GATEWAY_CLI_SIGN_PFX_PASSWORD`. 타임스탬프 서버는 `-TimestampUrl`(기본
`http://timestamp.digicert.com`), signtool 경로는 `-SignToolPath`로 지정합니다.
자격증명 미제공 시 빌드는 성공하되 **미서명** 바이너리를 배포합니다(내부 테스트용으로만 허용).

### 5. 추가 사내 설정 주입 (선택)

`setup`은 선택적 운영자 JSON을 Claude Code 설정에 deep-merge합니다. 예시를 복사·편집 후
빌드하면 `build.ps1`이 번들에 포함합니다:

```powershell
Copy-Item packaging\site-extra.json.example packaging\site-extra.json
```

```jsonc
{
  "managed": {                 // managed-settings.json 에 병합 (최고 tier)
    "env": { "HTTPS_PROXY": "http://proxy.example.com:8080" },
    "permissions": { "allow": ["Bash(git*)"] }
  },
  "user": {                    // ~/.claude/settings.json 에 병합
    "env": { "MY_TEAM_FLAG": "1" }
  }
}
```

gateway-cli 자체 키(OTEL, `ANTHROPIC_BASE_URL`, `apiKeyHelper`, `availableModels` …)는
site-extra **이후에** 적용되므로 라우팅을 깨뜨릴 수 없습니다. 주입된 모든 키는
`_gatewayCli` 마커에 기록되어 `gateway-cli disable`로 깔끔히 제거됩니다. 파일 없으면
no-op. 이 파일도 `.gitignore` 처리됨.

### 6. 사용자 설정은 백업됩니다

`setup`은 파일을 수정하기 **전에** `%LOCALAPPDATA%\gateway-cli\backups\`(0700, 소유자
전용)에 타임스탬프 스냅샷을 남기므로 기존 사용자·조직 설정이 유실되지 않습니다. 복원은 원하는
`.bak`를 원본 위치로 복사하면 됩니다. 위치는 `GATEWAY_CLI_BACKUP_DIR`로 변경 가능합니다.

### 7. 설정 카탈로그 (`manifest.py`)

이 도구가 아는 모든 Claude Code 설정 키는 한곳에 선언됩니다:
`entrypoints/gateway-cli-v2/src/cli/manifest.py`. 이 모듈은 **선언적 카탈로그 +
resolver**로, 직접 파일을 쓰지 **않습니다**(쓰기는 `managed.py` / `site_extra.py`
담당). "무엇을 건드리고, 어디에 있고, 누가 이기는가"의 단일 출처입니다.

주요 구성:

| 클래스 / 데이터 | 설명 |
|---|---|
| `ConfigField` + `FIELDS` | 설정 키 하나와 도구의 취급 방식; `FIELDS`가 전체 카탈로그 |
| `Output` | 값이 기록되는 `(placement, tier)` 대상(한 필드가 여러 개 가질 수 있음) |
| `Category` / `Placement` / `Tier` / `Status` | 필드 라벨: 제어 영역, 저장 위치, 설정 tier, 관계(`OWNED` / `PASSTHROUGH` / `BYPASS` / `DOCUMENTED`) |
| `ValueKind` / `Compose` | 값의 타입 형태(str/url/path/list/json…)와 소스 결합 방식(replace / merge) |
| `Location` + `LOCATIONS` | OS별 파일 경로(data dir, managed root, 사용자 settings, 캐시) |
| `Tierlevel` + `SETTINGS_HIERARCHY` | Claude Code 설정 우선순위(높은 순) |
| `PrecedenceRule` + `PRECEDENCE` | 비자명한 "누가 이기나" 사례(OTEL, `NO_PROXY`, 프록시) |
| `ResolvedConfig` + `SETUP_REQUIRED_KEYS` | 명령 1회의 실제 게이트웨이/OIDC 값과 `setup`이 요구하는 키 |
| `by_key` / `by_category` / `owned` / `bypass_keys` / `sensitive_keys` / `os_persisted_keys` | 다른 모듈이 리터럴을 하드코딩하는 대신 호출하는 조회 헬퍼 |

**관리 방법:**
- **키 추가 / 변경** → `FIELDS` 튜플을 편집. `key`, `category`, `placement`,
  `status`와 (`OWNED`인 경우) `outputs`, 필요 시 `flag` / `baked_from` /
  `env_override`를 지정.
- **이름은 배선과 일치해야 함:** `baked_from`은 `DEFAULT_*` 상수 /
  `GATEWAY_CLI_DEFAULT_*` 빌드 변수와, `env_override`는 `GATEWAY_CLI_*` 런타임
  변수와, `flag`는 Click 옵션과 일치.
- **문서와 동기화 유지:** 런타임 문서(`docs/FILE_AND_ENV_OPERATIONS.md`,
  `PROXY_PRECEDENCE.md`, `OTEL_PRECEDENCE.md`)와 요약본
  `docs/CONFIG_ITEMS_AND_DEFAULTS.md` — 매니페스트는 표면을 문서화할 뿐 런타임
  동작을 재유도하지 않음.
- **편집 후 테스트 실행**(`entrypoints/gateway-cli-v2`에서 `pytest`): drift guard가
  카탈로그를 CLI 플래그·writer와 대조합니다.

### 8. 유지보수 메모

| 파일 | 용도 |
|---|---|
| `entrypoints/*_entry.py` | PyInstaller 진입점 shim (`[tool.poetry.scripts]` 대응) |
| `gateway_cli.spec` | PyInstaller 스펙: 3개 콘솔 exe + 공유 `COLLECT` |
| `installer.iss` | Inno Setup 6 스크립트 → 단일 `setup.exe` + PATH 처리 |
| `build.ps1` | 원커맨드 파이프라인 (venv → pip → PyInstaller → smoke test → ISCC) |
| `download_wheels.ps1` | (선택) 오프라인 빌드 머신용 wheel 사전 캐시 |
| `site-config.json` | 사내 엔드포인트 내장값 (1장) — `.gitignore` |
| `site-extra.json.example` | 추가 설정 주입 템플릿 (5장) |

- `pyproject.toml`에 의존성 추가? 자동 인식. 단, 데이터 파일/플러그인을 동적 로드하면
  `gateway_cli.spec`에 `collect_data_files(...)` / `collect_submodules(...)` 추가.
- `[tool.poetry.scripts]`에 콘솔 스크립트 추가? `entrypoints/`에 shim, 스펙에
  `Analysis`/`PYZ`/`EXE` trio 추가, `COLLECT`와 `build.ps1` smoke-test 목록에 반영.
- 제품 버전은 `pyproject.toml`에서 변경(또는 `-Version`).
