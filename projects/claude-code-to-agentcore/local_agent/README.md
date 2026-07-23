# 로컬 이커머스 분석 에이전트 (Claude Code 환경)

Claude Code에서 Skill, MCP, 웹 검색을 조합해 이커머스 데이터를 분석하던 환경을 Claude Agent SDK로 코드화한 출발점입니다.

```
local_agent/
├── agent.py                             # 진입점: Claude Agent SDK로 Claude Code 세션을 래핑
├── .claude/settings.json                # 모델/리전/Bedrock 설정 (Claude Code와 동일 포맷)
├── .claude/skills/sales-report/SKILL.md # 매출 리포트 작성 스킬
├── .mcp.json                            # MCP 서버 2개 등록 (Claude Code와 동일 포맷)
├── mcp_server/ecommerce_mcp.py          # 사내 이커머스 DB 조회 MCP (stdio)
└── data/generate_data.py                # mock 데이터 생성기 (seed 고정)
```

## 구성 요소

| 요소 | 역할 | 파일 |
|---|---|---|
| Skill | 매출 리포트 표준 형식 | `.claude/skills/sales-report/SKILL.md` |
| MCP (ecommerce) | 사내 DB 조회 (query_sales / top_products / run_sql) | `mcp_server/ecommerce_mcp.py` |
| MCP (web-search) | AgentCore Web Search Gateway로 외부 트렌드 조회 | `.mcp.json` |

## 실행

```bash
# 1) mock 데이터 생성 (최초 1회)
python data/generate_data.py

# 2) .mcp.json의 web-search Gateway URL을 본인 값으로 교체

# 3) 에이전트 실행 (Bedrock)
export CLAUDE_CODE_USE_BEDROCK=1 AWS_REGION=us-east-1
python agent.py "sales-report 스킬로 2026년 상반기 매출 리포트를 작성해줘"
```

`setting_sources=["project"]` 옵션 덕분에 SDK가 `.claude/settings.json`, `.mcp.json`, `.claude/skills/`를 Claude Code와 동일하게 읽어들입니다. Claude Code에서 사용하던 세션을 비대화형으로 실행하는 셈입니다.

다음 단계는 이 에이전트를 그대로 AgentCore로 이전하는 것입니다. 로컬 MCP 도구는 [gateway/](../gateway)로, 에이전트 실행은 [runtime/](../runtime)으로 전환합니다.
