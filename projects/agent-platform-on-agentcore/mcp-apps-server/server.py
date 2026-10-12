#!/usr/bin/env python3
"""MCP Apps 서버 (SEP-1865) — 플랫폼 텔레메트리 대시보드.

이 서버가 존재하는 이유: MCP Apps 마이그레이션은 앱을 **연결된 MCP 서버**가 제공하도록
설계했다. 호스트·릴레이·샌드박스가 다 있어도 붙일 앱이 없으면 아무것도 렌더링되지
않는다. 이 서버가 그 앱을 제공한다.

앱은 **툴**에 붙는다 — 호스트는 `_meta.ui.resourceUri`를 선언한 툴의 호출에서만 앱을
띄운다. 참조 아키텍처는 ext-apps `examples/system-monitor-server`: 모델용 진입점 하나와
앱 전용 질의 툴 하나가 같은 뷰를 공유한다.

  - 모델용 `get_platform_telemetry(view, period)` — 모델이 부르면 앱이 뜬다.
  - 앱 전용 `query_platform_telemetry(view, period)` — `visibility=["app"]`이라 모델의
    툴 목록에 나타나지 않고(규격 MUST) 앱의 탭·기간·새로 고침만 부른다.

둘은 같은 `telemetry.fetch`를 부른다. 데이터는 CloudWatch(AWS/Bedrock 모델 지표,
AWS/Bedrock-AgentCore 런타임·게이트웨이 툴 지표)이며 실행 역할에
`cloudwatch:GetMetricData`가 필요하다 (README).

AgentCore Runtime 계약 (MCP protocol contract):
  - streamable-http, 경로 `/mcp`, `stateless_http=True`, `0.0.0.0:8000`, ARM64.
  - stateless 에서는 `client_supports_apps(ctx)`가 항상 False 다(README). 그래서 판정에
    의존하지 않고 결과에 항상 텍스트용 `note`를 싣는다 (SEP-2133).

로컬 실행:
    uv run --with 'mcp>=1.26.0' --with boto3 python server.py
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal

import boto3
from mcp.server.apps import Apps, ResourceCsp
from mcp.server.mcpserver import Context, MCPServer
from pydantic import Field

from telemetry import Telemetry, fetch

VIEW_URI = "ui://platform-status/app.html"

# 앱 HTML은 파일로 둔다 — `app/`에서 빌드된 생성물이다 (`npm run build`).
_HERE = Path(__file__).parent
_APP_HTML = (_HERE / "app.html").read_text(encoding="utf-8")

View = Annotated[
    Literal["models", "agents", "tools"],
    Field(
        description=(
            "models: Bedrock 모델별 호출·토큰(입력/출력/캐시)·지연. "
            "agents: AgentCore 런타임(에이전트·harness)별 호출·지연·오류. "
            "tools: 게이트웨이 툴별 호출·지연·오류."
        )
    ),
]
Period = Annotated[
    Literal["1h", "24h", "7d"],
    Field(description="조회 창. 1h(5분 버킷), 24h(1시간), 7d(1일)."),
]


def _cloudwatch():
    """Module-level factory so tests can swap the client without touching boto3."""
    return boto3.client("cloudwatch", region_name=os.environ.get("AWS_REGION", "ap-northeast-1"))


def _telemetry(view: str, period: str) -> Telemetry:
    return fetch(_cloudwatch(), view, period)


apps = Apps()

apps.add_html_resource(
    VIEW_URI,
    _APP_HTML,
    name="platform_telemetry_view",
    description="모델·에이전트·툴 사용량을 차트와 표로 보여주는 대시보드 앱",
    # 이 앱은 외부로 나가지 않는다 — 허용 도메인 없음을 명시한다.
    csp=ResourceCsp(),
    prefers_border=True,
)


@apps.tool(
    resource_uri=VIEW_URI,
    visibility=["model", "app"],
    description=(
        "플랫폼 사용량 텔레메트리를 대화 안의 대화형 대시보드로 보여준다. 사용자가 "
        "모델·에이전트·툴의 사용량, 토큰, 호출 수, 지연, 오류를 묻거나 '최근 사용량', "
        "'어떤 모델을 많이 썼어', '에이전트 호출 추이'처럼 물으면 이 툴을 쓴다. 결과는 "
        "CloudWatch 실측이며 앱이 차트로 그린다. 호출 뒤에는 한두 문장으로 눈에 띄는 점만 "
        "짚고 수치를 표로 반복하지 않는다."
    ),
)
def get_platform_telemetry(
    ctx: Context, view: View = "models", period: Period = "24h"
) -> Telemetry:
    """모델이 부르는 진입점. 호스트가 이 호출에 앱을 띄운다."""
    return _telemetry(view, period)


@apps.tool(
    resource_uri=VIEW_URI,
    visibility=["app"],
    description="앱 전용 재조회(탭·기간·새로 고침). 모델에게는 노출되지 않는다.",
)
def query_platform_telemetry(
    ctx: Context, view: View = "models", period: Period = "24h"
) -> Telemetry:
    """앱의 컨트롤만 부른다. 릴레이는 `is_app_callable`로 앱 발신 호출만 허용한다."""
    return _telemetry(view, period)


server = MCPServer(
    "platform-status",
    instructions=(
        "플랫폼 텔레메트리 서버입니다. get_platform_telemetry는 모델·에이전트·툴 사용량을 "
        "대화형 대시보드로 엽니다."
    ),
    extensions=[apps],
)


if __name__ == "__main__":
    # AgentCore Runtime 계약: 0.0.0.0:8000, /mcp, stateless.
    # stateless_http=True가 아니면 플랫폼이 붙이는 Mcp-Session-Id를 서버가 거부한다.
    server.run(
        transport="streamable-http",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        streamable_http_path="/mcp",
        stateless_http=True,
    )
