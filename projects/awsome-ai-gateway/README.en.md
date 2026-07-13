# AWSome AI Gateway

> ⚠️ **This is a sample/prototype for demonstration purposes only. Not production-ready; review and harden before any production use.**

[한국어 README](README.md)

A unified LLM Gateway for coding agents (Claude Code / Codex / Cowork). Authenticates via OIDC, auto-issues Virtual Keys, and routes each client to the appropriate backend (AWS Bedrock native / Bedrock Mantle). Provides per-team/user/app budget management, rate limiting, model access control, usage tracking (ROI), server-side web search, and a natural-language **AI BI Assistant** for querying operational data.

---

## Architecture

![AWSome AI Gateway Architecture](docs/img/architecture-overview.png)

Authentication, billing, and governance happen at a single gateway. Requests are then routed to different AWS accounts/backends based on the client type. Routing is **data-driven** — determined by `routing_profiles` table rows (Redis-cached, TTL 300s), not code.

```
[Claude Code]  [Codex]  [Cowork]
   │  api-key-helper (OIDC token → VK auto-issue; Cowork uses app config)
   ▼
[Admin API]  ── OIDC (Cognito) verification → VK issuance (POST /v1/auth/exchange)
   │
   ▼
Client ──Bearer VK──→ [Gateway Proxy] ── Auth · Client ID · Budget · RateLimit · Downgrade
                              │            → Routing profile → backend dispatch
                              │
          ┌───────────────────┼───────────────────────────────┐
          ▼                    ▼                               ▼
   claude-code               codex                          cowork
   Bedrock NATIVE            Bedrock Mantle                 Bedrock Mantle
   (boto3 invoke_model)      (OpenAI Responses API)         (Anthropic Messages)
   Cross-account STS         In-account IRSA                Cross-account STS
   Transparent fallback      httpx AsyncClient              httpx AsyncClient
          │
          ▼
   [Redis] + [Aurora PostgreSQL]   ← Budget / RateLimit / cost stream / aggregation

── Server-side Web Search (Architecture C, routing profile web_search_enabled=True) ──
Gateway injects web_search tool → intercepts model tool_use → AgentCore Gateway
managed WebSearch (MCP over httpx, SigV4/IRSA, us-east-1) → re-injects results.
Zero client-side configuration.

── Operator BI (admin-chat-agent) ──
[Admin UI /chat] ─SSE─→ [Admin API proxy] ─SigV4─→ [Bedrock AgentCore Runtime]
                                                      │ agents-as-tools (Strands)
                                                      │  Orchestrator (Opus)
                                                      │  ├ SQL Specialist → [query_db Lambda] → Aurora (read-only)
                                                      │  ├ Code Specialist → Code Interpreter
                                                      │  ├ SQL Validator
                                                      │  ├ Viz Specialist → chart spec
                                                      │  ├ Report Specialist
                                                      │  └ L3 self-consistency
                                                      ▼ Real-time streaming (SSE) + heartbeat
```

> Full component / account / data-plane details: [`ARCHITECTURE.md`](ARCHITECTURE.md) and [`devlog_websearch.md`](devlog_websearch.md).

---

## Demo

| Demo | Link |
|------|------|
| AWSome AI Gateway — Overview & Demo ① | [![Demo 1](https://img.youtube.com/vi/vp-Zsb5BocI/0.jpg)](https://youtu.be/vp-Zsb5BocI) |
| AWSome AI Gateway — Overview & Demo ② | [![Demo 2](https://img.youtube.com/vi/NDDsjHrkXMQ/0.jpg)](https://youtu.be/NDDsjHrkXMQ) |

---

## Key Features

- **Gateway Core** — OIDC → Virtual Key auto-issuance, 3-client × 2 API formats (Anthropic Messages / OpenAI Responses) routing, per-client backend dispatch (Bedrock native / Mantle). Per-team/user/app budgets (HARD_BLOCK / SOFT_WARNING / THROTTLE), rate limits (USER / TEAM / GLOBAL scopes), model ACL, auto-downgrade, usage/ROI aggregation.
- **Multi-app Governance (Phase 2)** — Per-user model allow-list (overrides team policy), per-user client ACL (restrict which apps a user can access), per-app budget/rate-limit separation. Data-driven routing profiles underpin all governance.
- **Service Tokens** — Long-lived bearer tokens for external (non-OIDC) systems. Issue / rotate / revoke via admin-api. Only the prefix is stored; full token returned once at issuance.
- **Resilience** — Readiness gate (`/health/ready`, 503 on degradation), invalid-VK fast rejection (no DB session opened), DB pool tuning (10s `pool_timeout` fast-fail), Redis circuit breaker with in-memory rate-limit fallback, response header leak prevention, security event detector with LRU OOM defense, cost stream spooling on Redis blip.
- **Server-side Web Search (Architecture C)** — Gateway injects `web_search` tool, intercepts model `tool_use`, calls AgentCore managed WebSearch (MCP/SigV4). Zero client configuration — just ask and get searched answers. Supports both Anthropic/Responses formats.
- **Admin UI (Next.js 14)** — Dashboard (KPI, cost trends, model/client share donuts, team/user ranking), user/team/model/budget/rate-limit management, real-time monitoring, ROI analysis, `/chat` BI assistant. Light/dark glass design.
- **AI BI Assistant (`/chat`)** — Natural language → validated SQL (text2SQL) → Aurora query → markdown table + recharts chart. Agents-as-tools orchestrator on AWS Strands + Bedrock AgentCore Runtime. Real-time token streaming + heartbeat.

---

## Security Design

Security is solved structurally, not by configuration:

| Layer | Mechanism | Why it's safe |
|-------|-----------|---------------|
| Request body | Whitelist reconstruction (13 allowed fields only) | Arbitrary client fields never reach upstream |
| Upstream auth | Gateway mints its own credentials (SigV4 / Mantle bearer) | Client key (VK) is never forwarded — auth boundary fully separated |
| Response headers | Gateway-only injection (budget/rate-limit/request-id) | Upstream headers are not relayed to client |
| Model/client ACL | Per-user allow-list enforced at middleware layer | Unauthorized model or app access returns 403 |
| Invalid key flood | Redis-first validation; DB session not opened for invalid keys | Connection pool exhaustion attack neutralized |

This is **re-origination**, not relay. There is no path for headers to leak.

---

## Guides

| Audience | Document | Description |
|----------|----------|-------------|
| Deployer (Infra Engineer) | [`guides/deployer-guide.md`](guides/deployer-guide.md) | EKS Fargate deployment, Terraform, Helm, secrets, monitoring |
| Admin (Operator) | [`guides/admin-guide.md`](guides/admin-guide.md) | User/team management, budgets, rate limits, models, incident response |
| End User (Developer) | [`guides/user-guide.md`](guides/user-guide.md) | CLI install, login, Claude Code integration, troubleshooting |
| 3-client Onboarding (all OS) | [`guides/QUICKSTART.md`](guides/QUICKSTART.md) | Claude Code / Codex / Cowork × macOS · Linux · Windows |

Step-by-step deployment: [`deployment/docs/eks-fargate/`](deployment/docs/eks-fargate/). Secrets contract: [`deployment/docs/secrets-contract.md`](deployment/docs/secrets-contract.md).

---

## Services

Helm deploys 6 Deployments + 1 Job. The admin-chat-agent is hosted separately on Bedrock AgentCore Runtime.

| Service | Role |
|---------|------|
| **gateway-proxy** | User API entry point. VK auth, client identification, backend routing, rate limiting, Bedrock/Mantle invocation |
| **admin-api** | Management REST API. OIDC→VK issuance, budget/rate-limit/model CRUD, `/chat` AgentCore proxy |
| **admin-ui** | Next.js 14 management dashboard |
| **scheduler** | ROI aggregation + VK expiration cleanup (singleton, reuses admin-api image) |
| **notification-worker** | Budget threshold alerts (default: mock provider) |
| **cost-recorder-worker** | Redis Stream → Aurora cost recording + daily aggregation |
| **migration** | Alembic DB migration (helm pre-install/pre-upgrade Job, head=`0022`) |
| **admin-chat-agent** | BI assistant on Bedrock AgentCore Runtime (not in EKS). Connected via `AGENTCORE_RUNTIME_ARN` env |

---

## Quick Start (User)

```bash
# 1. Set environment (provided by your admin)
export OIDC_ISSUER_URL="..."
export OIDC_CLIENT_ID="..."
export ADMIN_API_URL="..."
export ANTHROPIC_BASE_URL="..."

# 2. Onboard (macOS / Linux)
bash scripts/onboard-macos-linux.sh
# With Claude Code auto-setup:
bash scripts/onboard-macos-linux.sh --setup-claude-code

# 3. Start using
claude
```

- **Claude Code**: `gateway-cli setup` creates managed settings (`50-gateway.json`)
- **Codex**: `~/.codex/config.toml` → `model_provider=gateway`, `wire_api=responses`, `base_url=<BASE>/v1`
- **Cowork**: App config JSON with 5 keys (`inferenceProvider` / `inferenceGatewayBaseUrl` / `inferenceGatewayApiKey` / `inferenceGatewayAuthScheme` / `inferenceCredentialKind:"static"`)

> Detailed guide: [`guides/QUICKSTART.md`](guides/QUICKSTART.md). Container-isolated execution: [`gateway-clients/README.md`](gateway-clients/README.md).

---

## Quick Start (Operator — BI Assistant)

After logging into Admin UI, use the **Chat** menu (or bottom-right BI Chat button) to query operational data in natural language. Requires ADMIN or TEAM_LEADER role.

```
Examples:
  "Show total cost per user this month as a table and chart"
  "Model call volume trend for the last 30 days"
  "Teams that reached 80% of budget"
  "Top user getting 429s in the last 24h"
```

---

## Project Structure

| Path | Description |
|------|-------------|
| `gateway-proxy/` | Data plane (FastAPI, VK auth + client identification + Bedrock/Mantle proxy) |
| `admin-api/` | Control plane (model/team/budget/VK REST API + scheduler entrypoint) |
| `admin-ui/` | Next.js 14 management UI (dashboard/monitoring/analysis + `/chat` BI) |
| `admin-chat-agent/` | BI assistant — Strands agents-as-tools + AgentCore Runtime |
| `cost-recorder-worker/` | Redis Stream → Aurora cost recorder |
| `notification-worker/` | Budget threshold alert worker |
| `db/` | Alembic migration source (head=`0022`) |
| `gateway-cli/` | User CLI (`gateway-cli`, `api-key-helper`, `statusline`) |
| `gateway-clients/` | Claude-code/Codex container isolation utilities (`claude-box`/`codex-box` + `gw.sh`) |
| `scripts/` | Onboarding scripts (macOS/Linux/Windows) + IAM scripts |
| `deployment/charts/` | Helm chart + values (EKS Fargate dev/prod/loadtest + on-prem dev/prod) |
| `deployment/terraform/` | Terraform modules (VPC, EKS, Aurora, ElastiCache, Cognito, IRSA, ALB, ESO) |
| `guides/` | Final guides (deployer/admin/user/quickstart) |

---

## Original Builders

| Name | Role | Contact |
|------|------|---------|
| **Kyutae Park, Ph.D.** | AWS AI Specialist Solutions Architect | [Email](mailto:kyutae@amazon.com) |
| **Minjae An** | AWS Forward Deployed DL Architect | [Email](mailto:aminjae@amazon.com) |
| **Sue Cha** | AWS Deep Learning Architect | [Email](mailto:suecha@amazon.com) |
| **Gonsoo Moon** | AWS Sr. AI Specialist Solutions Architect | [Email](mailto:moongons@amazon.com) |

## Contributors

| Name | Role | Contact |
|------|------|---------|
| **Charlie Chang** | AWS Sr. Generative AI Strategist | [Email](mailto:subchang@amazon.com) |
| **Yash Shah** | AWS Data Science Manager | [Email](mailto:syash@amazon.com) |
| **Youngjoon Choi** | AWS Sr. AI Specialist Solutions Architect Manager | [Email](mailto:choijoon@amazon.com) |

---

**Built with ❤️ by AWS Specialist SA Team and GenAIIC**

---

## License

This sample code is made available under a modified MIT license (MIT-0). See the [LICENSE](LICENSE) file.
