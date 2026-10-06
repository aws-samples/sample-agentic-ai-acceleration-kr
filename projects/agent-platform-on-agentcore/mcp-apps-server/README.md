# MCP Apps 예제 서버 (platform-status) — 플랫폼 텔레메트리

MCP Apps(SEP-1865) 앱을 제공하는 **예제** MCP 서버입니다. 플랫폼의 호스트·릴레이·샌드박스는
규격을 따르는 MCP 서버라면 무엇이든 받으므로 이 서버는 필수가 아닙니다. 앱은 연결된 MCP 서버가
제공하는 구조라, 붙일 서버가 하나도 없으면 렌더링할 앱도 없습니다 — 이 서버는 그 경로를 끝까지
확인해 볼 수 있게 하는 예제입니다.

앱은 **툴에 붙습니다.** 호스트는 `_meta.ui.resourceUri`를 선언한 툴의 호출에서만 앱을 띄웁니다.

## 무엇을 보여주는가

CloudWatch 실측 지표를 대화 안의 대시보드로 그립니다. 참조 아키텍처는 ext-apps
`examples/system-monitor-server`(모델용 진입점 + 앱 전용 질의 툴 + Chart.js)입니다. 공식 예제
26개는 모두 서버당 뷰 하나라, 다양성은 뷰가 아니라 앱(툴 + 뷰) 단위로 늘립니다.

| 툴 | visibility | 인자 | 용도 |
|----|-----------|------|------|
| `get_platform_telemetry` | `["model", "app"]` | `view`(models·agents·tools), `period`(1h·24h·7d) | 모델이 호출하는 진입점. 앱이 열린다 |
| `query_platform_telemetry` | `["app"]` | 같음 | 앱의 탭·기간·새로 고침. **모델의 툴 목록에 나타나지 않는다** (규격 MUST) |

| view | 데이터 (CloudWatch) |
|------|--------|
| `models` | `AWS/Bedrock` `ModelId`별 호출·입력/출력/캐시 토큰·지연·TTFT |
| `agents` | `AWS/Bedrock-AgentCore` 런타임(`Name`, harness 포함)별 호출·지연·오류·스로틀 |
| `tools` | `AWS/Bedrock-AgentCore` 게이트웨이 툴별(`Name`, `Method=tools/call`) 호출·지연·오류 |

앱이 쓰는 세 통로가 모두 실제 의미를 가집니다.

- 첫 렌더는 툴 **결과**(호스트 → 앱). Strands 스트림에는 툴 결과가 없어 호스트가 릴레이로 같은
  툴을 모델 인자 그대로 한 번 더 부릅니다 — CloudWatch 읽기라 무해합니다.
- 탭·기간·새로 고침은 앱 전용 툴 `tools/call`(앱 → 서버). **모델은 이 경로에 없습니다.**
- 행 선택 후 "모델 컨텍스트에 넣기"는 `ui/update-model-context`(앱 → 모델). 다음 턴에 모델이
  그 항목의 수치를 참고합니다.

지표 질의는 `telemetry.py`가 `GetMetricData` SEARCH 식으로 조립합니다(뷰당 API 1회). 스키마는
**정확한 차원 집합**으로 못 박습니다 — 같은 지표가 차원 조합별로 여럿 있어 느슨하게 잡으면 두
배로 집계됩니다. 결과 `Label`은 동적 라벨 `${PROP('Dim.X')}`로 차원값을 받아 항목 id로 쓰고,
같은 이름의 런타임이 인스턴스(Resource) 둘로 잡히면 한 항목으로 합칩니다. 모델은 `us.`/`global.`
추론 프로필이 따로 잡히므로 라벨을 `us/claude-opus-5`처럼 남깁니다.

## 앱 뷰 빌드 (`app/`)

소스는 `app/index.html` + `app/src/{main,chart,format}.ts`, 공식 SDK
`@modelcontextprotocol/ext-apps`의 `App`으로 호스트와 통신하고 Chart.js로 그립니다.

```bash
cd mcp-apps-server/app
npm install
npm test           # format.ts 순수 함수 (node --test, Node 22.18+ 타입 스트리핑)
npm run build      # → ../app.html (단일 파일, 약 610 KB)
```

- **`app.html`은 생성물이고 커밋합니다.** `server.py`가 그 파일을 읽어 `resources/read`로
  내보내므로 런타임에 Node가 필요 없고, 파이썬만으로 `agentcore launch`가 됩니다.
  `.gitattributes`가 diff를 숨기니 리뷰는 `app/` 소스로 합니다. **생성물을 직접 고치지 마세요.**
- 단일 파일이어야 하는 이유: 규격의 CSP 기본값은 `script-src 'self' 'unsafe-inline'`이라
  인라인 스크립트는 되지만 별도 청크 파일은 로드되지 않습니다.
- 스타일은 호스트가 `hostContext.styles.variables`로 내려주는 **규격 변수 이름**
  (`--color-background-primary`, `--color-text-primary`, `--font-sans`, …)을 씁니다. 차트 팔레트는
  그 글자색의 명도로 다크/라이트를 판정해 만듭니다.
- `app/`은 `.dockerignore`에 있어 컨테이너에 들어가지 않습니다.

## 로컬 실행·테스트

```bash
cd mcp-apps-server
PORT=3099 uv run --with 'mcp>=2' --with boto3 python server.py
uv run --with 'mcp>=2' --with boto3 --with pytest --with anyio python -m pytest tests -q
```

계약 테스트(`tests/test_server.py`)는 네트워크 없이 인프로세스로 붙고 CloudWatch는 스텁입니다.
변환 테스트(`tests/test_telemetry.py`)는 가짜 `GetMetricData` 응답으로 버킷 정렬·병합·라벨을 봅니다.

## AgentCore Runtime 배포

계약: streamable-http `/mcp`, **`stateless_http=True`**, `0.0.0.0:8000`, ARM64. `-p MCP`로 구성합니다.

```bash
agentcore configure --entrypoint server.py --name bap_platform_status \
  --requirements-file requirements.txt --region ap-northeast-1 --ecr auto -p MCP
agentcore launch
```

실행 역할에 CloudWatch 읽기 권한이 필요합니다. 역할은 agentcore CLI가 만들었으므로 인라인
정책으로 붙입니다(`GetMetricData`는 리소스 수준 제한이 없어 `*`):

```bash
aws iam put-role-policy --role-name AmazonBedrockAgentCoreSDKRuntime-ap-northeast-1-<suffix> \
  --policy-name PlatformTelemetryRead \
  --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["cloudwatch:GetMetricData"],"Resource":"*"}]}'
```

에이전트가 이 툴을 부르려면 `bap-gateway` 에 타깃으로 붙여야 합니다. Runtime ARN 을
`infra/envs/standalone/terraform.tfvars` 의 `runtime_mcp_servers` 에 넣고 apply 합니다
(절차: [`DEPLOYMENT.md`](../DEPLOYMENT.md#mcp-서버-연결-mcp-apps)).

```hcl
runtime_mcp_servers = { "platform-status" = "<bap_platform_status runtime ARN>" }
```

이 타깃(`platform-status`)은 `listingMode=DYNAMIC`이라 `tools/list`를 이 서버에 그때그때
넘깁니다 — 툴을 바꿔도 `SynchronizeGatewayTargets`가 필요 없고, 부르면 "not supported for dynamic
MCP targets"로 거절됩니다. 런타임이 READY가 되는 순간 모델이 새 툴 목록을 봅니다.

채팅 화면이 앱을 띄우려면 레지스트리에 MCP 레코드로 등록해야 합니다. Registry 화면에서 유형 MCP,
원격 URL 은 `terraform output runtime_mcp_endpoints` 의 값입니다. 서버는 그 `server.remotes[].url`
로 릴레이합니다(`services/mcp_apps_service.py`의 `resolve_endpoint`).

## 알려진 제약: stateless 모드에서 `client_supports_apps`

SEP-2133은 앱을 협상하지 않은 클라이언트에게도 의미 있는 텍스트를 돌려주라고 요구합니다.
그런데 `stateless_http=True`(AgentCore 필수)에서는 요청마다 세션이 새로 만들어져 `initialize`의
extensions가 `tools/call`까지 남지 않아 `client_supports_apps(ctx)`가 항상 `False`입니다.
그래서 판정에 의존하지 않고 결과에 항상 `note`(한 줄 요약)를 싣습니다. 앱은 `series`를, 텍스트
클라이언트는 `note`와 `series`의 `totals`를 읽습니다.
