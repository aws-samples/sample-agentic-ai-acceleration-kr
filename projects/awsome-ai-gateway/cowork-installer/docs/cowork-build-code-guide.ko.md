# Cowork Installation Wizard — Build Code 사용 가이드 (End-to-End)

> **대상 독자:** Cowork 설치 파일(installation wizard)을 자사 환경에 맞게 빌드·배포하려는
> 고객사 담당자(빌드 엔지니어 / 배포 관리자).
> **목적:** 이 문서 하나로 (a) 설치 마법사를 만드는 **Build Code** 가 무엇이고, (b) 그것을
> 어떻게 **환경에 맞게 수정**하며, (c) 어떻게 **빌드**하고, (d) 최종 사용자가 **어떤 순서로
> 설치**하는지까지 처음부터 끝까지 따라 할 수 있도록 합니다.

---

## 0. 한눈에 보기

**"Build Code"** 란 Cowork 설치 마법사(`gateway-cli-cowork-setup-<version>.exe`)를 생성하는
스크립트·설정 파일 묶음입니다. 고객은 이 코드를 그대로 사용하거나, 몇 개의 값만 자사 환경에
맞게 수정하여 재빌드하면 됩니다. 소스 코드를 고칠 필요는 없습니다 — **환경별 값은 전부
설정 파일 한두 개로 주입**되도록 설계되어 있습니다.

Cowork 전체 도입 흐름은 세 단계입니다:

```
[빌드 담당자]                              [최종 사용자 PC]
Build Code 수정 → build.ps1 실행           (1) Cowork Readiness Check 실행 → PC 적합성 확인
      │                                    (2) Cowork installation wizard(setup.exe) 실행
      ▼                                    (3) Claude Desktop App 설치·실행 후 Cowork 사용
gateway-cli-cowork-setup-<version>.exe ───────────────►  (2)번 단계에 이 파일을 전달
```

- (1) **Readiness Check** — 사용자 PC가 Cowork 실행에 적합한지 사전 점검(§6.1). *현재
  별도 `Cowork Readiness Check.exe` 는 로드맵 항목이며, 이 가이드에서는 점검해야 할 항목만
  기술합니다.*
- (2) **Installation wizard** — 이 Build Code가 만들어 내는 산출물. CLI 도구(`gateway-cli-cowork.exe`,
  `api-key-helper.exe`)를 오프라인으로 설치(§6.2).
- (3) **Claude Desktop + 설정** — 사용자가 Claude Desktop을 설치·최초 로그인한 뒤,
  `gateway-cli-cowork setup` 으로 게이트웨이를 가리키게 함(§6.3).

> ⚠️ 이 설치 파일은 **폐쇄망(air-gapped) Windows x64** 를 전제로 합니다. PyInstaller 가
> CPython 3.11+ 런타임과 모든 의존성을 번들에 포함하므로 대상 PC에는 Python·인터넷이
> 필요 없습니다.

---

## 1. Build Code 구성 요소

Build Code는 `installer/packaging/` 아래에 있습니다. 고객이 **직접 수정하는** 파일과
**보통 건드리지 않는** 파일을 구분하세요.

| 파일 | 역할 | 고객 수정 |
|---|---|---|
| `packaging/site-config.json` | **사내 고정값 입력**(OIDC, 게이트웨이 URL, CA, 모델). 빌드 시 exe에 내장 | **✅ 자주 수정** |
| `packaging/site-extra.json` | (선택) 커스텀 설정 키 주입. 없으면 no-op | ⬜ 필요 시 |
| `packaging/build.ps1` | **원커맨드 빌드 파이프라인**(venv→pip→PyInstaller→smoke test→서명→Inno Setup) | 🔸 파라미터로 호출(코드 수정 불필요) |
| `packaging/installer.iss` | Inno Setup 6 마법사 정의(= 설치 마법사 그 자체). 브랜딩·PATH·사전점검 | 🔸 브랜딩만 선택 수정 |
| `packaging/gateway_cli.spec` | PyInstaller 스펙(2개 콘솔 exe + 공유 런타임) | ⬜ 의존성 추가 시만 |
| `packaging/download_wheels.ps1` | (선택) 빌드 머신도 오프라인일 때 wheel 사전 캐시 | ⬜ 폐쇄망 빌드 시 |
| `packaging/site-extra.json.example` | `site-extra.json` 작성 예시 | ⬜ 참고용 |

> 🔐 **커밋 금지 파일:** `site-config.json` 과 `site-extra.json` 은 환경별 식별자(OIDC id 등)를
> 담으므로 버전 관리에 커밋하지 마세요. `installer/.gitignore` 에 이미 제외되어 있습니다.
> 빌드가 생성하는 `_site_config.py`, `dist/`, `.build-venv/`, `wheels/` 도 마찬가지입니다.

---

## 2. 빌드 환경 준비 (빌드 담당자 PC, 1회)

PyInstaller 는 크로스컴파일이 불가하므로 **반드시 Windows x64** 에서 빌드합니다(VM/CI 가능).

| 필요 항목 | 비고 |
|---|---|
| **Windows x64** | `installer.iss` 의 `ArchitecturesAllowed=x64compatible` 와 일치 |
| **Python 3.11+** | `py -3.13/3.12/3.11` 또는 `python`. 빌드 머신에만 필요(최종 PC엔 불필요) |
| **Inno Setup 6** | 설치 마법사 컴파일용. 없으면 `-SkipInstaller` 로 zip 배포 (jrsoftware.org) |
| (선택) **Windows SDK** | 코드 서명 `signtool.exe` 용 |
| (선택) **wheel 캐시** | 빌드 머신도 폐쇄망일 때 (§5.2) |

---

## 3. Build Code 수정 — 환경 값 주입

### 3.1 `site-config.json` — 사내 고정값 (가장 중요)

이 파일 하나만 채우면 사용자는 카드 없이 `setup` 만 실행하면 됩니다. JSON 이므로 경로
백슬래시는 `\\` 로 이스케이프하고, 빈 문자열(`""`)은 코드 내 기본값으로 폴백됩니다.

```json
{
  "oidcIssuerUrl": "https://<issuer>/oauth2/default",
  "oidcClientId":  "xxxxxxxxxxxxxxxx",

  "gatewayUrl":    "https://gateway.example.com",
  "adminApiUrl":   "https://api.gateway.example.com",
  "caBundle":      "C:\\corp-proxy-ca.pem",

  "coworkGatewayHttpsUrl": "https://<cloudfront-id>.cloudfront.net",
  "coworkModels":          "global.anthropic.claude-opus-4-8",

  "orgUuid":               "<선택> uuidgen 으로 조직당 1회 생성",
  "expectedCaSha256":      "<선택> AB12CD...(대문자 hex, 구분자 없음)"
}
```

**Cowork 전용 값 4개 (일반 gateway-cli 값과 구분):**

| 키 | 필수 | 의미 | 주의 |
|---|---|---|---|
| `coworkGatewayHttpsUrl` | **필수** | 앱의 inference base(`inferenceGatewayBaseUrl`) | **반드시 공개 신뢰(CloudFront) HTTPS** — 앱이 HTTP를 거부(originPinned). `adminApiUrl` 과 **다름** |
| `coworkModels` | 권장 | `inferenceModels` 로스터(쉼표 구분, **첫 항목이 기본값**) | 각 alias 는 게이트웨이 `model.model_aliases` 에 **실제 등록된 값**이어야 함(미등록 alias → model-not-found). 여러 개면 쉼표로: `"global.anthropic.claude-opus-4-8,<another-registered-alias>"`. 빈 값이면 코드의 `FALLBACK_MODELS`(기본 `global.anthropic.claude-opus-4-8`) 로 폴백 |
| `orgUuid` | **선택** | `deploymentOrganizationUuid` **텔레메트리 태그** — 게이트웨이 사용량을 조직 단위로 귀속시키는 식별자 | 비우면 태그를 아예 기록하지 않음(설치·동작에 영향 없음). 조직 단위 사용량 집계가 필요할 때만 `uuidgen` 으로 조직당 한 번 생성 |
| `expectedCaSha256` | **선택** | `setup` 이 설치를 허용하는 사내 CA의 SHA-256 **핀(fingerprint pinning)** — 지정한 지문의 PEM만 신뢰 저장소에 설치 | 비우면 **핀 없음**으로 진행(PEM을 지문 검사 없이 설치, dev 빌드). 값이 있으면 **지문 불일치 PEM 설치를 거부**(CA 로테이션 시 `setup --force` 로 우회). 배포 대상 CA를 못 바꾸게 잠그는 **보안 강화용** |

> **값 우선순위(높을수록 우선):**
> `build.ps1 -Param` → 환경변수 `GATEWAY_CLI_DEFAULT_*` → `site-config.json` → 코드 기본값(`site_defaults.py`).
> 즉 `site-config.json` 을 비워 두고 빌드 시점에 파라미터로만 넘겨도 됩니다(§5.1 방법 B).

### 3.2 `site-extra.json` — (선택) 커스텀 관리 설정 키 주입

OTLP·커스텀 헤더·자동 업데이트 정책 등 **추가 Claude Desktop 관리 설정 키**를 `setup` 시점에
함께 기록하고 싶을 때만 사용합니다.

```powershell
Copy-Item packaging\site-extra.json.example packaging\site-extra.json
# 편집 후 빌드 → exe에 번들, setup 시 관리 설정 저장소에 병합
```

이 파일은 **플랫 맵**이며, 최상위 키는 반드시 **실제 Claude Desktop 3P 관리 설정 키**여야 합니다
(공식 키 목록: <https://claude.com/docs/third-party/claude-desktop/configuration>).

```jsonc
{
  "inferenceCustomHeaders": { "X-Tenant-Id": "acme" },  // 게이트웨이 요청에 붙일 헤더
  "disableAutoUpdates": "true",                         // 앱 자동 업데이트 비활성

  "otlpEndpoint": "https://otel.example.com:4318",      // OTLP 텔레메트리 수집기
  "otlpProtocol": "http/protobuf"
}
```

- **키는 allowlist 로 검증** — gateway-cli 가 소유한 라우팅·모델·자격 키
  (`inferenceProvider`, `inferenceGatewayBaseUrl`, `inferenceModels`, 자격 헬퍼 키,
  `deploymentOrganizationUuid`)는 **주입해도 무시**되어 게이트웨이 동작을 깨뜨릴 수 없습니다.
- 인식되지 않는 키(예: 구 Claude Code 의 `env`/`permissions`/`managed`/`user` 섹션)는 **경고와
  함께 무시**됩니다 — 앱이 어차피 조용히 버리기 때문입니다.
- 주입한 키는 마커에 기록되어 `gateway-cli-cowork disable` 로 깔끔히 제거됩니다.
- 파일이 없으면 주입은 no-op.

#### 값이 기록되는 위치 (location)

`site-extra.json` 은 별도 파일로 남지 않습니다. 빌드 시 exe 안에 번들되고, `setup` 실행 시
allowlist 를 통과한 키가 **게이트웨이 라우팅 키와 똑같은 관리 설정 저장소·똑같은 쓰기 동작으로
함께 기록**됩니다. 즉 주입 키와 core 키는 항상 같은 곳에 나란히 들어갑니다.

| OS | 저장소 위치 |
|---|---|
| Windows | 레지스트리 `HKCU\SOFTWARE\Policies\Claude` (값은 전부 `REG_SZ` 문자열) |
| macOS | `~/Library/Application Support/Claude-3p/configLibrary/<uuid>.json` (`_meta.json` 의 `appliedId` 가 활성 파일 지목) |

> ⚠️ Windows 에서 `HKLM\SOFTWARE\Policies\Claude` 에 값이 있으면 앱이 HKCU 를 **무시**합니다
> (§6.1·§8). 이 도구는 HKLM 을 쓰지 않으므로, 그런 값이 있으면 주입 키도 함께 적용되지 않습니다.

#### key–value–location 정렬은 어떻게 맞추나

주입 키가 "이름·값 형식·기록 위치" 세 축 모두에서 앱이 실제 읽는 형태와 어긋나지 않도록
`setup` 이 자동 정렬합니다:

- **KEY(이름):** allowlist 대조로 **실제 3P 키만** 통과 — 오타·구 Claude Code 키·core 소유 키는
  기록 전에 걸러집니다. 그래서 저장소에는 앱이 인식하는 이름만 들어갑니다.
- **VALUE(형식):** 같은 값이라도 저장소 규칙에 맞게 자동 직렬화합니다.

  | 값 타입 | Windows (REG_SZ) | macOS (JSON) |
  |---|---|---|
  | 불리언 | `"true"` / `"false"` | 네이티브 `true` |
  | 정수(예: TTL) | `"3600"` (십진 문자열) | `3600` |
  | 객체·배열 | 압축 JSON 문자열 `{"a":1}` | 네이티브 객체/배열 |

  Windows 는 3P 규격상 **모든 값이 문자열**이어야 하므로(레퍼런스 드라이버 `cowork-test.ps1`
  과 동일), JSON 에 `true`/`3600` 처럼 native 로 적어도 자동으로 문자열화됩니다.
- **LOCATION(위치)·우선순위:** core 게이트웨이 키가 **항상 우선**합니다. 주입 키를 먼저 깔고
  core 키로 덮어쓰므로, 설령 core 키를 주입하려 해도(→ allowlist 에서 이미 차단) 게이트웨이
  라우팅을 이길 수 없습니다.

### 3.3 `installer.iss` — (선택) 마법사 브랜딩

보통 그대로 두면 되지만, 회사명·표시명은 바꿀 수 있습니다.

| 항목 | 위치 | 수정 가능 여부 |
|---|---|---|
| `AppPublisher` | `#define AppPublisher "Your Organization"` | ✅ 회사명으로 변경 |
| `AppName` / `UninstallDisplayName` | `#define AppName …` | 🔸 변경 시 `cli/cowork_uninstall.py` 의 `_EXPECTED_DISPLAY_NAME` 과 **반드시 일치**시켜야 함 |
| `DefaultDirName` | `{autopf}\GatewayCLI-Cowork` | ✅ 설치 경로 변경 |
| **`AppId`** | `{{806B6437-…}}` | 🔸 **최초 배포 시 1회 지정, 이후 절대 변경 금지** — 이 GUID가 업그레이드 인식 기준. 최초 릴리스 전에는 조직 고유 GUID로 바꿔도 되지만, **한 번 배포된 뒤에는 바꾸지 말 것**. 바꾸면 기존 설치가 고아가 됨(업그레이드로 인식 못 함) |

> `installer.iss` 는 **의도적으로 Cowork 정책 키를 쓰지 않습니다.** 설치 마법사는 관리자
> 권한으로 실행되어 HKCU 가 관리자 하이브를 가리키므로, 정책 키는 사용자가 §6.3 에서
> `setup` 을 **본인 세션**으로 실행할 때 올바른 하이브에 기록됩니다.

---

## 4. 빌드 실행

### 4.1 기본 빌드 (온라인 빌드 머신)

```powershell
# 저장소 루트에서 (PowerShell)
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

진행 순서: `.build-venv` 생성 → 프로젝트 pip 설치 → `site-config.json` 로드 →
사내 값 내장(`_site_config.py` 생성) → PyInstaller → 각 exe `--help` smoke test →
(자격증명 있으면) 코드 서명 → Inno Setup 컴파일.

**방법 B — 파일 대신 파라미터로 직접 전달** (`site-config.json` 보다 우선):

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 `
    -OidcIssuerUrl https://<issuer> -OidcClientId <client-id> `
    -GatewayUrl https://gateway.example.com `
    -AdminApiUrl https://api.gateway.example.com `
    -CaBundle C:\corp-proxy-ca.pem `
    -CoworkGatewayUrl https://<cloudfront-id>.cloudfront.net `
    -OrgUuid <uuid> -CoworkModels "global.anthropic.claude-opus-4-8"
```

유용한 파라미터: `-Version 1.2.3`(파일명 버전), `-SkipInstaller`(zip 배포용, Inno Setup 생략).

### 4.2 빌드 머신도 오프라인인 경우

동일한 Windows/Python 버전의 **인터넷 연결 머신**에서 wheel 캐시를 만들어 함께 옮깁니다.

```powershell
# (연결된 머신에서)
powershell -ExecutionPolicy Bypass -File packaging\download_wheels.ps1 -OutDir C:\wheels
# (오프라인 빌드 머신에서)
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -WheelDir C:\wheels
```

### 4.3 코드 서명 (선택 — 현재 단계에서는 생략 가능)

> ℹ️ **현재 단계에서는 선택 사항입니다.** 자격증명을 넘기지 않으면 빌드는 그대로 성공하며,
> 서명 단계만 건너뜁니다(내부 테스트·PoC 용도로 충분). 다만 미서명 PyInstaller exe 는
> AV/SmartScreen 오탐 대상이므로, 잠긴 환경에 **정식 배포**할 때는 서명을 권장합니다.

서명하려면 아래처럼 자격증명을 넘깁니다:

```powershell
# 사내 표준: 인증서 저장소/HSM/토큰의 SHA-1 thumbprint
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -SignThumbprint <THUMBPRINT>
# 또는 PFX 파일 + 암호
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -SignPfxFile C:\certs\corp.pfx -SignPfxPassword <pw>
```

2개 exe(Inno Setup 전)와 setup.exe(Inno Setup 후)를 SHA-256 + RFC 3161 타임스탬프로
서명·검증합니다. 자격증명 미제공 시 빌드는 성공하되 **미서명** 경고를 출력합니다.
환경변수: `GATEWAY_CLI_SIGN_THUMBPRINT` / `..._PFX` / `..._PFX_PASSWORD`.

### 4.4 빌드 산출물

```
dist/
├── gateway-cli-cowork-suite/                   # PyInstaller onedir 출력(zip 배포 가능)
│   ├── gateway-cli-cowork.exe
│   ├── api-key-helper.exe
│   └── _internal/                              # 2개 exe가 공유하는 Python 런타임 + 의존성
└── installer/
    └── gateway-cli-cowork-setup-<version>.exe  # ★ 사용자에게 전달할 단일 설치 마법사
```

`gateway-cli-cowork-setup-<version>.exe` 하나만 전달하면 됩니다.

---

## 5. 빌드 검증 (권장)

전달 전에 빌드 담당자가 최소한 확인할 것:

1. **smoke test 통과** — `build.ps1` 이 각 exe `--help` 를 자동 실행합니다(실패 시 빌드 중단).
2. **내장 값 확인** — 대상과 유사한 테스트 PC에서 `gateway-cli-cowork verify` 로 baseUrl·모델이
   의도대로 내장됐는지 확인.
3. (서명한 경우) 설치 마법사 우클릭 → 속성 → *디지털 서명* 탭에 서명이 보이는지 확인.

---

## 6. 최종 사용자 설치 순서 (End-to-End)

빌드된 설치 마법사를 받은 사용자가 밟는 3단계입니다.

### 6.1 단계 (1) — Cowork Readiness Check (PC 적합성 확인)

사용자 PC가 Cowork 실행에 적합한지 **사전 점검**하는 단계입니다. 점검 항목:

| 점검 항목 | 적합 기준 | 부적합 시 |
|---|---|---|
| **OS/아키텍처** | Windows 10/11 **x64** | 32-bit·ARM 미지원 |
| **Claude Desktop 설치 여부** | MSIX 패키지 `*Claude*` 존재(또는 설치 예정) | §6.3 에서 먼저 설치 |
| **HKLM 정책 충돌** | `HKLM\SOFTWARE\Policies\Claude` 에 값이 **없어야** 함 | 값이 있으면 앱이 HKCU를 무시 → 관리자에게 제거 요청 |
| **egress 도달성** | `downloads.claude.ai:443` 도달 가능(세션 시작마다 필수) | 프록시/방화벽 allowlist 필요 |
| **사내 프록시 TLS** | 사내 프록시 CA가 **OS 신뢰 저장소**에 있어야 함 | `setup` 이 자동 설치(§6.3), 또는 관리자 배포 |

> ℹ️ **참고:** 별도 `Cowork Readiness Check.exe` 는 로드맵 항목입니다. 현재는 위 항목을
> 수동 확인하거나, 저장소의 참조 드라이버 `cowork-test.ps1` 의 `check` / `netcheck` /
> `regcheck` 모드로 점검할 수 있습니다. (이 드라이버는 배포 산출물이 아닌 검증용입니다.)

### 6.2 단계 (2) — Installation wizard 실행

빌드 산출물 `gateway-cli-cowork-setup-<version>.exe` 를 실행합니다.

```powershell
# 대화형: 더블클릭 → 마법사 진행("Add to PATH" 기본 on 권장)
# 무인 설치 (SCCM/Intune/GPO):
gateway-cli-cowork-setup-<version>.exe /VERYSILENT /NORESTART
```

- **Add to PATH**(기본 on) → **새 터미널**에서 `gateway-cli-cowork` / `api-key-helper` 사용 가능.
- 이 마법사는 CLI 도구만 설치하며 **정책 키는 쓰지 않습니다**(§3.3 참조).
- 대화형 설치 시 Claude Desktop 미설치가 감지되면 안내 메시지가 표시됩니다(비차단).

### 6.3 단계 (3) — Claude Desktop 설치 후 Cowork 활성화

1. **Claude Desktop App 설치** 후 실행하여 **최초 로그인**을 완료합니다.
2. 설치된 CLI로 게이트웨이를 가리키게 설정합니다 — **본인 Windows 세션의 관리자 권한 터미널**에서:

```powershell
gateway-cli-cowork login    # OIDC 브라우저 로그인
gateway-cli-cowork setup    # 사내 CA 설치 + Cowork 관리 설정 기록 + Claude Desktop 재시작
gateway-cli-cowork verify   # 설정 확인 + 상태 점검
```

> ⚠️ **`setup` 은 반드시 본인 세션의 관리자 권한 터미널에서 실행하세요.**
> `HKCU\SOFTWARE\Policies\Claude` 는 **호출자 SID의 하이브**에 기록됩니다. SSM/SYSTEM/다른
> 관리자 계정으로 실행하면 잘못된 하이브에 기록되어 앱이 게이트웨이를 인식하지 못합니다.
> 다중 사용자 PC는 각 사용자가 `setup` 을 1회씩 실행합니다.

**`setup` 동작 요약**
- 첫 단계로 사내 CA를 OS 신뢰 저장소에 설치(PEM 없으면 자동 건너뜀 — 프록시 밖에선 정상).
  건너뛰려면 `--skip-ca`, CA 로테이션 지문 불일치를 강제 허용하려면 `--force`.
- 관리 설정 기록 후 Claude Desktop 을 자동 재시작(설정은 **실행 시점에만** 읽힘).
  재시작을 원치 않으면 `--no-relaunch`.
- 자격 방식은 기본 `helper-script`(앱이 VK를 자동 갱신). 정적 VK는 `--credential-kind static --api-key vk-…`.
- **자동 롤백:** `setup` 중 CA 설치는 됐지만 설정 기록이 실패하면, 이번 실행이 만든 변경만
  자동 원복하고 원래 오류를 그대로 보고합니다. 이전 성공 설정은 보존됩니다.

3. Claude Desktop 의 **Cowork 탭**에서 모델을 선택하고 대화를 시작합니다.

### 6.4 되돌리기 / 제거

되돌리기는 **범위가 작은 것부터 큰 것 순**으로 골라 쓰세요: 개별 되돌리기(설정만/CA만) →
전체 소프트웨어 정리(`clear`) → 바이너리 제거(`uninstall`).

| 작업 | 명령 | 되돌리는 범위 | 권한 / 주의 |
|---|---|---|---|
| **사용자 설정 전체 정리** | `gateway-cli-cowork clear` | 관리 설정 + CA + 토큰/VK 캐시 + 이 도구의 마커·백업을 **안전한 순서로 한 번에** 원복(= `disable` + `ca restore` + `logout` + 백업 정리의 상위집합). **바이너리는 남김** | 본인 세션(관리자 권한 불필요). 확인 프롬프트 표시(`-y` 로 생략, `--dry-run` 으로 미리보기, `--keep-ca`/`--keep-tokens` 로 일부 보존) |
| **CLI 도구(바이너리) 제거** | *앱 및 기능* 에서 제거, 또는 `gateway-cli-cowork uninstall` | Inno 언인스톨러(`unins000.exe`)에 위임하여 2개 exe·공유 `_internal\` 런타임·PATH 항목·ARP 등록 제거. **설정/CA/토큰은 원복하지 않음** | 실행 중인 exe 는 자기 자신을 지울 수 없으므로 위임 후 종료(자기삭제 안 함). **설정도 함께 지우려면 `uninstall --clear-first`**(먼저 `clear` → 그다음 바이너리 제거) |
| **업그레이드** | 새 `gateway-cli-cowork-setup-<version>.exe` 를 덮어 실행 | 바이너리를 새 버전으로 교체. 사용자 설정·정책 키는 유지 | `AppId` 가 동일해야 업그레이드로 인식(§3.3) |

> 📌 **개별 되돌리기(부분 원복):** 전체 정리(`clear`)까지 필요 없고 한 가지만 되돌릴 때 사용합니다.
> 두 명령 모두 이 도구가 **추가/변경한 값만** 되돌리고, 기존 조직 설정은 건드리지 않습니다.
>
> | 작업 | 명령 | 설명 |
> |---|---|---|
> | 게이트웨이 설정만 해제(설정 원복) | `gateway-cli-cowork disable` | 관리 설정을 `setup` 이전 상태로 정확히 원복(추가한 값만 제거, 덮어쓴 값은 복원). **CA 는 그대로 둠.** 설정이 실제로 바뀐 경우에만 Claude Desktop 재시작(`--no-relaunch` 로 생략) |
> | CA 신뢰만 원복 | `gateway-cli-cowork ca restore` | `setup` 이 설치한 CA **만** 기록된 백업에 따라 제거(직접 신뢰하던 CA 는 건드리지 않음). 설정은 그대로 둠 |
>
> CA 신뢰 여부만 확인하려면(읽기 전용): `gateway-cli-cowork ca check`.

---

## 7. 기존 설정 백업 (자동)

`setup` 은 파일을 수정하기 **전에** 항상 타임스탬프 스냅샷을 남기므로 기존 사용자·조직
설정이 유실되지 않습니다(반복 실행해도 첫 스냅샷을 덮어쓰지 않음).

- 저장 위치(소유자 전용): `C:\Users\<user>\AppData\Local\gateway-cli\backups\`
  (`GATEWAY_CLI_BACKUP_DIR` 로 변경 가능)
- 복원: 원하는 `.bak` 파일을 원본 위치로 복사.

---

## 8. 문제 해결 (Troubleshooting)

| 증상 | 원인 / 해결 |
|---|---|
| 빌드가 "No Python >= 3.11" 로 중단 | 빌드 머신에 Python 3.11+ 설치(python.org). 최종 PC엔 불필요 |
| "Inno Setup 6 (ISCC.exe) not found" | Inno Setup 6 설치, 또는 `-SkipInstaller` 로 zip 배포 |
| 설치 후 앱이 게이트웨이를 인식 못 함 | `setup` 을 SSM/SYSTEM/다른 관리자로 실행했을 가능성 → **본인 세션**에서 재실행 |
| 앱이 정책을 무시 | `HKLM\SOFTWARE\Policies\Claude` 값 존재 → 제거(HKLM 이 HKCU 보다 우선) |
| Cowork 세션이 시작되지 않음 | `downloads.claude.ai` 가 프록시/방화벽에서 차단 → allowlist |
| TLS 오류(사내망) | 사내 프록시 CA 가 OS 신뢰 저장소에 없음 → `setup`(CA 설치) 또는 관리자 배포. **Chromium 은 `NODE_EXTRA_CA_CERTS` 를 무시** — OS 저장소에 넣어야 함 |
| exe 가 AV/SmartScreen 에 걸림 | 미서명 빌드 → §4.3 으로 서명 |
| `setup` 이 CA 설치를 건너뜀 | PEM 이 없음(프록시 밖에선 정상). 필요 시 `caBundle` 경로 확인 |

---

## 9. 참고 문서

- `installer/packaging/README.ko.md` — 빌드 패키지 기술 요약(파일 구성·의존성 추가·스펙 수정)

---

### 용어

| 용어 | 설명 |
|---|---|
| **Build Code** | 설치 마법사를 생성하는 스크립트·설정 묶음(`installer/packaging/`) |
| **Installation wizard** | 빌드 산출물 `gateway-cli-cowork-setup-<version>.exe`(Inno Setup 기반) |
| **VK (Virtual Key)** | 게이트웨이 인증 키. `helper-script` 방식은 앱이 자동 갱신 |
| **managed config / 정책 키** | Cowork 를 게이트웨이 모드로 전환하는 `HKCU\SOFTWARE\Policies\Claude` 값 |
| **egress CA** | 사내 TLS-인터셉트 프록시의 CA. **OS 신뢰 저장소**에 있어야 앱이 `downloads.claude.ai` 를 신뢰 |
