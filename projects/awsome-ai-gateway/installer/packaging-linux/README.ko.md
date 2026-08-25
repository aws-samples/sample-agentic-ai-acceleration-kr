# Linux / WSL 설치 패키지

Python이 설치되지 않은 **폐쇄망(air-gapped) Linux/WSL** 사용자를 위한 **완전 자립형
설치 자산**을 빌드합니다. PyInstaller가 CPython 3.11+ 런타임과 모든 의존성(`click`,
`structlog`, `platformdirs`, `boto3`, `requests`, `PyYAML` 및 `socket`,
`subprocess`, `ssl` 등 표준 라이브러리)을 번들에 포함하므로, 대상 머신에는 **64비트
Linux 커널 외에 아무것도 필요 없습니다**(WSL 2도 Linux로 취급).

`../packaging/`(Windows)의 Linux/WSL 형제 패키지입니다. 두 패키지는 CLI 소스 트리
**하나**(`packaging/entrypoints/gateway-cli-v2`)를 공유하므로 플랫폼 간 애플리케이션
코드가 절대 달라지지 않으며, 빌드 글루와 패키징 형식(`.deb`/`.run` vs `setup.exe`)만
다릅니다.

---

## 배포 형식: `.deb` 권장

Windows와 동일한 사용자 경험(Python 사전 설치 불필요·사내 고정 설정 내장·수동 설정
최소화)을 Linux에서 가장 잘 재현하는 것은 **네이티브 `.deb` 패키지**입니다. 자세한
빌드/설치 절차는 [`deb/README.md`](deb/README.md)를 참조하세요.

| 항목 | `.run` 자가압축 설치 | `.deb` (권장) |
|---|---|---|
| Python 필요 | 불필요(내장) | 불필요(내장) |
| 사내 고정 설정 내장 | O(`site-config.json`) | O(동일 payload) |
| PATH 설정 | `~/.bashrc`/`profile.d` 편집 | `/usr/bin` 심볼릭 링크 — 이미 PATH에 존재 |
| 제거 | 별도 `uninstall.sh` | `sudo apt remove gateway-cli-suite` |
| 업그레이드 | `.run` 재실행 | dpkg 버전 관리 |
| 파일 추적 | 없음 | dpkg가 모든 파일 관리 |

`build.sh`가 **하나의** 배포용 payload(`dist/gateway-cli-suite/`)를 만들고, 각
패키징 형식(`deb/build-deb.sh` 등)이 그 payload를 감싸는 얇은 소비 계층입니다.
다른 배포판(rpm 등)도 동일한 payload를 재사용하는 형제 빌더로 추가할 수 있습니다.

---

## 빌드 결과물

```
dist/
├── gateway-cli-suite/                    # PyInstaller onedir 출력
│   ├── gateway-cli                       # cli.main:main
│   ├── api-key-helper                    # api_key_helper.main:main
│   ├── statusline                        # statusline.main:main
│   └── _internal/                        # 3개 CLI가 공유하는 단일 Python 런타임 + 의존성
└── installer/
    ├── gateway-cli-suite_<version>_<arch>.deb    # 네이티브 .deb (권장)
    └── gateway-cli-setup-<version>.run           # 단일 오프라인 .run (대체)
```

3개 CLI는 `_internal` 런타임 폴더 하나를 공유합니다(Python 공유 라이브러리, botocore
JSON 서비스 모델, certifi CA 번들의 단일 사본). onefile 바이너리 3개보다 설치물이 훨씬
작고, 매 실행마다 temp 디렉터리로 압축을 풀지 않아 시작이 빠릅니다.

---

## 파일 구성

| 파일 | 용도 | Windows 대응 |
|---|---|---|
| `entrypoints/*_entry.py` | `[tool.poetry.scripts]`를 대신하는 진입점 shim (PyInstaller는 `module:function`이 아닌 스크립트 파일이 필요) | 동일 |
| `gateway_cli.spec` | PyInstaller 스펙: 콘솔 바이너리 3개 + 공유 `COLLECT` | `../packaging/gateway_cli.spec` |
| `build.sh` | 원커맨드 빌드 파이프라인 (venv → pip → PyInstaller → smoke test → `.run`) | `build.ps1` |
| `deb/build-deb.sh` | payload를 `.deb`로 감싸는 얇은 패키징 계층 | `installer.iss` + ISCC |
| `make_installer.sh` | onedir 출력 + `install.sh`를 자가압축 `.run`으로 패킹 | `installer.iss` |
| `install.sh` | `.run`의 대상 머신 설치 스크립트(런타임 복사, PATH 연결, uninstaller 작성) | `installer.iss` `[Code]` |
| `download_wheels.sh` | (선택) *빌드 머신*도 오프라인일 때 wheel 사전 캐시 | `download_wheels.ps1` |
| `site-config.json` | 사내 고정값 입력 파일 (OIDC, 도메인, CA 경로) | 동일 |
| `site-extra.json.example` | 커스텀 키 주입 예시 | 동일 |

---

## 빌드 머신 요건

PyInstaller는 **크로스컴파일이 안 됩니다** — 대상 머신과 **동일한 아키텍처 및 호환
glibc**를 가진 **Linux**에서 빌드해야 합니다(컨테이너/CI 러너 가능). 필요:

1. **Python 3.11+** (`pyproject.toml`과 일치)
2. **bash**, **tar**, **gzip** — 모든 Linux/WSL 배포판에 기본 존재
3. `.deb` 빌드에는 **`dpkg-deb`** (`sudo apt-get install dpkg-dev`)
4. `packaging/entrypoints/gateway-cli-v2/` 프로젝트가 포함된 이 저장소

> CLI 프로젝트는 형제 폴더 `../packaging/`에서 단일 소싱됩니다. `packaging-linux/`만
> 단독 배포할 경우 `packaging-linux/entrypoints/gateway-cli-v2/`에 프로젝트를
> vendoring 하면 스펙/빌드가 이를 사용합니다.

---

## 빌드 방법

`.deb` 흐름(권장) — 공유 payload를 먼저 만들고 감쌉니다:

```bash
# 1. 배포판 무관 payload 빌드. 사내 고정값은 여기서 내장(.run과 동일).
./build.sh --skip-installer \
  --gateway-url   https://gateway.example.com \
  --admin-api-url https://api.gateway.example.com \
  --ca-bundle     /etc/ssl/certs/corp-proxy-ca.pem

# 2. .deb로 감싸기
./deb/build-deb.sh
```

`.run` 대체 흐름:

```bash
./build.sh   # dist/installer/gateway-cli-setup-<version>.run + .sha256
```

`build.sh`는 일회용 `.build-venv` 생성 → 프로젝트 pip 설치(pip가 poetry-core
백엔드를 직접 처리, Poetry 불필요) → PyInstaller → 각 바이너리 `--help` smoke test
→ 설치 파일 패킹 순으로 진행합니다.

> **⚠️ CA 번들은 Linux 경로로 반드시 내장하세요.** 사내 CA를 쓰는 환경에서 `--ca-bundle`
> 없이 빌드하면, 설정 파일에 잘못된 값이 기록될 수 있습니다. 프로덕션 Linux 빌드는
> `--ca-bundle /etc/ssl/certs/<corp-ca>.pem`처럼 **Linux 경로**를 명시하세요. (플랫폼
> 인지 폴백이 적용되어 미지정 시 Linux는 OS 신뢰 저장소를 사용합니다.)

### 빌드 머신도 오프라인인 경우

**동일한 Linux arch / glibc / Python minor 버전**의 연결된 머신에서 wheel 캐시를
만들어 함께 옮깁니다.

```bash
./download_wheels.sh --out-dir /path/to/wheels
./build.sh --wheel-dir /path/to/wheels
```

---

## 사내 고정값 내장 (`site-config.json`)

`site-config.json`(camelCase 키: `oidcIssuerUrl`, `oidcClientId`, `gatewayUrl`,
`adminApiUrl`, `caBundle`)을 편집하면 `build.sh`가 이를 읽어 생성된
`cli/_site_config.py`를 통해 바이너리에 값을 내장합니다. 우선순위:

```
build.sh --flag  >  GATEWAY_CLI_DEFAULT_*  >  site-config.json  >  site_defaults.py 리터럴
```

- **Gateway URL / Admin API URL**: 위 예시처럼 내장하면 사용자는 `--gateway-url`
  없이 `setup`만 실행하면 됩니다.
- **CA**: Linux 경로로 내장(위 경고 참조).
- **OIDC(issuer/client)**: 환경별 **비밀값**이므로 저장소에 포함되지 않습니다.
  빌드 시 `--oidc-issuer-url`/`--oidc-client-id`로 내장하거나(Windows와 동일한 방식),
  `setup` 시점에 플래그/카드로 공급합니다. 미내장 + 미공급 시 `setup`은 다음과 같이
  **명확한 오류로 즉시 중단**됩니다:

  ```
  Error: missing values needed for setup: oidcIssuerUrl, oidcClientId.
  ```

이 파일은 환경별 식별자를 담으므로 `.gitignore` 처리됩니다 — **커밋 금지**.

---

## 커스텀 키 주입 (`site-extra.json`)

`site-extra.json.example`을 `site-extra.json`으로 복사해 편집하면 `build.sh`가 `cli`
패키지 옆에 번들하고, `gateway-cli setup`이 각 섹션을 설정 파일에 deep-merge 합니다
(병합 규칙은 예시의 `__comment` 참조). 파일이 없으면 주입은 no-op. 역시 `.gitignore`.

---

## 폐쇄망 대상 설치

**`.deb` (권장):**

```bash
sudo apt install ./gateway-cli-suite_<version>_<arch>.deb
# Windows와 동일한 통일 흐름:
gateway-cli login
gateway-cli setup --model sonnet
claude
```

- 런타임은 `/opt/gateway-cli-suite`에 설치.
- `gateway-cli`, `api-key-helper`, `statusline`을 `/usr/bin`에 심볼릭 링크 →
  모든 셸에서 이미 PATH에 존재(**rc 편집·새 터미널 불필요**).
- 사내 OIDC/게이트웨이/CA 고정값은 이미 바이너리에 내장.
- 제거: `sudo apt remove gateway-cli-suite`. 업그레이드: 새 `.deb`를 그대로 `apt install`.

**`.run` (대체):** 단일 `gateway-cli-setup-<version>.run`을 전달합니다.

```bash
chmod +x gateway-cli-setup-<version>.run
./gateway-cli-setup-<version>.run          # 사용자 단위(root 불필요), ~/.local 이하
sudo ./gateway-cli-setup-<version>.run     # 시스템 전역, /opt 이하
```

- 무인 배포(Ansible/MDM)는 `--quiet`. 위치 변경 `--prefix DIR`/`--bindir DIR`,
  PATH 편집 생략 `--no-path`, 설치 없이 추출 `--extract-only DIR`.
- PATH는 **새** 터미널(또는 `source ~/.bashrc`)에서 적용. 제거는 생성된
  `uninstall.sh`를 설치 때와 동일 권한으로 실행.

---

## OS 간 사용 흐름 통일 (핵심 요건)

| 핵심 사용자 경험 | Windows | Linux(`.deb`) |
|---|---|---|
| Python 사전 설치 불필요 | O(내장 런타임) | O(내장 런타임) |
| 사내 고정 설정 내장 | O | O(Gateway/Admin/CA; OIDC 동일 방식) |
| 간소화된 설치·실행 절차 | 설치파일 실행 + PATH 자동 | `apt install` + `/usr/bin` 심볼릭 링크 |
| 통일 흐름 | 설치 → login → `setup` → claude | `apt install` → login → `setup --model …` → claude |

설치 *방식*(Inno Setup `.exe` vs `.deb`)은 다르지만, 세 가지 핵심 사용자 경험은
동일하게 제공됩니다.

---

## WSL 유의사항

WSL 2는 네이티브 Linux(`sys.platform == "linux"`)로 인식되어 이 번들이 그대로
실행됩니다. CLI는 런타임에서 WSL 동작을 이미 처리합니다(`cli/platform.py` 참조):
`wslview`/`powershell.exe`로 Windows 쪽 브라우저를 열고, `wslpath`로 Windows
Downloads 폴더의 온보딩 카드를 찾습니다. WSL과 네이티브 Linux 간 패키징 변경은
필요 없으며, Linux x86_64에서 한 번 빌드하면 둘 다 설치됩니다.

> **WSL + Windows Claude:** 이 패키지는 네이티브 Linux(및 네이티브 Linux Claude를
> 실행하는 WSL)용입니다. Claude가 **Windows 바이너리**인 WSL은 Windows 설치 파일이
> 담당합니다.

---

## 폐쇄망 유의사항

- **내부 CA TLS:** 번들은 certifi 공개 CA만 포함. 게이트웨이/OIDC가 사내 CA를 쓰면
  `site-config.json`의 `caBundle`(또는 `--ca-bundle`)로 **Linux PEM 경로**를 내장하거나
  `REQUESTS_CA_BUNDLE`/`AWS_CA_BUNDLE`를 지정하세요. 번들된 `truststore`는 앱이
  활성화하면 시스템 CA 저장소(`/etc/ssl/certs`)로도 검증을 라우팅합니다.
- **boto3 region/endpoint:** AWS 경로가 없으면 CLI가 boto3를 내부 엔드포인트
  (`endpoint_url`)로 향하도록 하세요.
- **실행 비트 / noexec 마운트(`.run` 한정):** `.run`은 실행 가능해야 하며(`chmod +x`),
  `/tmp`가 `noexec`면 stub 추출이 실패하므로 `TMPDIR=/var/tmp`로 지정하세요.
- **무결성:** `build.sh`/`build-deb.sh`는 항상 `.sha256`를 생성합니다. `.run`은
  `--sign-gpg-key <keyid>`로 detached `.asc` GPG 서명(Authenticode의 Linux 대응)도
  만들 수 있어 수신자가 `gpg --verify`로 검증할 수 있습니다.

---

## 유지보수 노트

- `pyproject.toml`에 의존성 추가 시 자동 인식됩니다. 단, 데이터 파일/플러그인을 동적
  로드하면 `gateway_cli.spec`에 `collect_data_files(...)`/`collect_submodules(...)`를
  추가하세요(Windows 스펙과 동기화 유지).
- `[tool.poetry.scripts]`에 콘솔 스크립트 추가 시: `entrypoints/`에 shim, 스펙에
  `Analysis`/`PYZ`/`EXE` trio, `COLLECT` 반영, `build.sh` smoke-test 목록,
  `install.sh`/`deb/build-deb.sh`의 `CLIS` 배열, 그리고 `../packaging/`의 미러 변경.
- 버전 변경은 `pyproject.toml`의 version 수정(또는 `--version`)으로 하며 `build.sh`가
  이를 읽습니다.
