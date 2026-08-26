# gateway-cli-v2 Windows 설치 패키지

Python이 없는 **폐쇄망(air-gapped) Windows** 사용자를 위한 **자립형 오프라인
설치 파일**을 빌드합니다. PyInstaller가 CPython 3.11+ 런타임과 모든 의존성을
번들에 포함하므로 대상 PC에는 **Windows x64 외에 아무것도 필요 없습니다.**

## 1. 빌드 결과물

```
dist/
├── gateway-cli-cowork-suite/               # PyInstaller onedir
│   ├── gateway-cli-cowork.exe
│   ├── api-key-helper.exe
│   └── _internal/                          # 2개 exe가 공유하는 단일 런타임 + 의존성
└── installer/
    └── gateway-cli-cowork-setup-<version>.exe   # 단일 오프라인 설치 파일
```

`--onedir` 방식: 2개 exe가 `_internal/` 하나를 공유 → 설치 파일이 작고 실행이 빠르며
실행 시마다 temp에 압축을 푸는 `_MEI*` 누적이 없습니다(보안 SW 충돌도 적음).

| 파일 | 용도 |
|---|---|
| `entrypoints/*_entry.py` | PyInstaller 진입점 shim (poetry `[scripts]` 대체) |
| `gateway_cli.spec` | PyInstaller 스펙: 콘솔 exe 2개 + 공유 `COLLECT` |
| `installer.iss` | Inno Setup 6 → 단일 `setup.exe`(PATH 처리 포함) |
| `build.ps1` | 빌드 파이프라인 (venv → pip → PyInstaller → smoke test → ISCC) |
| `download_wheels.ps1` | (선택) 오프라인 빌드용 wheel 사전 캐시 |
| `site-config.json` | 사내 고정값 입력 파일 (3장) |
| `site-extra.json.example` | 커스텀 키 주입 예시 (4장) |

## 2. 빌드 방법

크로스컴파일 불가 → **Windows x64**(VM/CI 가능)에서 빌드. 필요: **Python 3.11+**,
**Inno Setup 6**(없으면 `-SkipInstaller`로 zip 배포).

```powershell
# 저장소 루트에서
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

`.build-venv` 생성 → pip 설치(poetry-core를 pip가 직접 처리, Poetry 불필요) →
PyInstaller → 각 exe `--help` smoke test → 설치 파일 컴파일 →
`dist\installer\gateway-cli-cowork-setup-<version>.exe`.

**빌드 머신도 오프라인이면** 동일 Windows/Python 버전 머신에서 wheel 캐시 생성 후 함께 이동:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\download_wheels.ps1 -OutDir C:\wheels
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -WheelDir C:\wheels
```

## 3. 사내 고정값 내장

OIDC·게이트웨이 도메인·CA 경로처럼 **환경 전체에서 고정인 값**을 빌드 시 exe에 구워
넣으면 사용자는 카드 없이 `setup`만 실행하면 됩니다.

```jsonc
// packaging\site-config.json (경로 백슬래시는 \\). build.ps1이 자동으로 읽어 내장.
{
  "oidcIssuerUrl": "https://.../oauth2/default",
  "oidcClientId":  "xxxxxxxx",
  "gatewayUrl":    "https://gateway.example.com",
  "adminApiUrl":   "https://api.gateway.example.com",
  "caBundle":      "C:\\corp-proxy-ca.pem"
}
```

빌드 파라미터로 직접 전달도 가능(우선순위 높음):
`build.ps1 -OidcIssuerUrl <issuer> -OidcClientId <id> -GatewayUrl <url> -AdminApiUrl <url> -CaBundle <pem>`.

**우선순위:** `-Param` > 환경변수 `GATEWAY_CLI_DEFAULT_*` > `site-config.json` >
`site_defaults.py` 리터럴. 빈 값은 다음 단계로 폴백. `site-config.json`은 환경별
식별자를 담으므로 **커밋 금지**(.gitignore).

## 4. 커스텀 값 주입

Cowork는 OS별 **단일 관리 설정 저장소**를 읽습니다(`setup`이 여기에 씀):

| OS | 저장소 |
|---|---|
| Windows | `HKCU\SOFTWARE\Policies\Claude` (값 전부 REG_SZ) |
| macOS | `Claude-3p/configLibrary/<uuid>.json` (`_meta.json`의 `appliedId`로 선택) |

추가 키는 **JSON 하나 편집**으로 주입 — `site-extra.json.example`을 복사·편집 후 빌드하면
build.ps1이 exe에 번들, `setup` 시 저장소에 병합됩니다.

```jsonc
// 최상위 키는 실제 Claude Desktop 3P 관리 설정 키여야 함
{
  "inferenceCustomHeaders": { "X-Tenant-Id": "acme" },
  "disableAutoUpdates": "true",
  "otlpEndpoint": "https://otel.example.com:4318",
  "otlpProtocol": "http/protobuf"
}
```

- **키는 allowlist 검증** — gateway-cli 소유 라우팅·모델·자격 키는 주입해도 **무시**되어
  게이트웨이 라우팅을 깨지 못함. 미인식 키는 경고와 함께 무시.
- 값은 저장소 형식에 맞게 자동 직렬화(Windows=문자열, macOS=네이티브).
- 주입 키는 마커에 기록되어 `disable`로 깔끔히 제거. 파일 없으면 no-op. **커밋 금지**.

## 5. 기존 설정 백업

`setup`은 관리 설정 수정 **전에** 항상 스냅샷을 남기므로 기존 사용자·조직 설정이
유실되지 않습니다(반복 실행해도 첫 스냅샷 유지). `disable`은 이 스냅샷으로 **이 도구가
추가/변경한 키만** 원복합니다.

| OS | 스냅샷 | 원복 |
|---|---|---|
| Windows | 이 도구가 쓰는 키의 사전 값 | 추가 키 삭제, 덮어쓴 키 이전 값 복원 |
| macOS | 대상 `<uuid>.json` 전체(소유권 보존) | 사전 파일로 복원 |

저장 위치(0700): `C:\Users\<user>\AppData\Local\gateway-cli\backups\`
(`GATEWAY_CLI_BACKUP_DIR`로 변경). 복원은 원하는 `.bak`를 원본 위치로 복사.

## 6. 설치 (폐쇄망 대상 PC)

`gateway-cli-cowork-setup-<version>.exe` 하나만 전달.

```powershell
# 대화형: 더블클릭 / 무인(SCCM·Intune·GPO):
gateway-cli-cowork-setup-<version>.exe /VERYSILENT /NORESTART
```

- **전체 사용자·관리자 전용** 설치, 고정 경로 `C:\Gateway-CLI-Cowork`(경로 선택 숨김).
  마법사 첫 페이지(Welcome)에 개요 + 이후 `setup` 안내.
- 설치 폴더 **PATH 자동 등록**(시스템+사용자) → 새 터미널에서 CLI 사용. 제거 시 PATH도 정리.
  업그레이드는 새 setup.exe 덮어 실행(`AppId` 동일).

### 설치 마법사 Welcome 페이지

별도 페이지 없이 `installer.iss` `[Messages]`의 내장 `WelcomeLabel2`를 개요로 재정의합니다.

> **주의:** Inno 6은 Welcome을 기본 숨김. `[Setup]`의 `DisableWelcomePage=no`로
> 되살립니다 — 이 줄을 지우면 개요가 사라지고 Tasks 페이지부터 시작합니다.

- `%n`=줄바꿈, `[name/ver]`=`AppName`+`AppVersion`.
- **ASCII만 사용**(인라인 메시지 — 한글·불릿·화살표 깨짐). 서식·현지화는 `InfoBeforeFile=overview.rtf`.
- **실제 동작과 일치**시킬 것(고정 경로·PATH·관리자 전용·HKLM/HKCU 스코프). `[Setup]`/`[Tasks]`
  변경 시 같은 커밋에서 갱신.

### 사용자 최종 시나리오

```powershell
gateway-cli-cowork login    # OIDC 브라우저 로그인
gateway-cli-cowork setup    # 사내 CA 설치 + 관리 설정 기록 + Claude Desktop 재시작
gateway-cli-cowork verify   # 설정 확인 + 상태 점검
```

> `setup`은 첫 단계로 사내 CA를 OS 신뢰 저장소에 설치(PEM 없으면 자동 건너뜀 — 사내 이그레스
> 프록시 밖에서는 정상; 건너뛰려면 `--skip-ca`). HKCU 정책 키를 쓰므로 **본인 세션의 관리자
> 권한 터미널**에서 실행(SSM/SYSTEM/다른 관리자 계정 금지 — 잘못된 하이브에 기록됨). CA만
> 되돌리려면 `ca restore`.

## 7. 코드 서명 (프로덕션 필수)

미서명 exe는 AV/SmartScreen 오탐 대상 — 잠긴 환경 배포용은 반드시 서명.

```powershell
powershell ... build.ps1 -SignThumbprint <THUMBPRINT>          # 인증서 저장소/HSM/토큰
powershell ... build.ps1 -SignPfxFile <path.pfx> -SignPfxPassword <pw>   # 또는 PFX
```

`build.ps1`이 exe(ISCC 전) + setup.exe(ISCC 후)를 SHA-256 + RFC 3161 타임스탬프로
서명·검증. 자격증명 미제공 시 빌드는 성공하되 **미서명** 경고(테스트용만 허용). 환경변수:
`GATEWAY_CLI_SIGN_THUMBPRINT` / `..._PFX` / `..._PFX_PASSWORD`.

## 8. 폐쇄망 유의사항 & 유지보수

- **내부 CA TLS:** 번들은 certifi 공개 CA만 포함. 사내 CA는 `REQUESTS_CA_BUNDLE`·
  `AWS_CA_BUNDLE`를 사내 PEM으로 지정하거나 3장 `caBundle`로 내장.
- **의존성 추가:** `pyproject.toml`에 추가 시 자동 인식. 동적 로드는 스펙에
  `collect_data_files(...)`/`collect_submodules(...)` 추가.
- **콘솔 스크립트 추가:** `entrypoints/` shim + 스펙 `Analysis/PYZ/EXE` + `COLLECT` +
  `build.ps1` smoke-test 반영.
- **버전 변경:** `pyproject.toml`의 version(또는 `-Version`).
