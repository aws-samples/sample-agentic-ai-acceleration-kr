# Sample Agentic AI Acceleration KR

A curated archive of agentic AI code assets, sample implementations, and artifacts developed through the **Agentic AI Acceleration** program in Korea.

This repository serves as a reference collection for builders looking to explore agentic AI patterns and accelerate their own development — covering everything from production-grade agent architectures to reusable Claude Code tooling.

## Repository Structure

```
.
├── projects/    # Self-contained sample projects
├── plugin/      # 3a-plugin — Claude Code plugin reusable across projects
└── ...
```

## Projects

Each project lives in its own folder under `projects/` with a dedicated README and setup guide.

| Project | Description |
| --- | --- |
| [agentops-kit](./projects/agentops-kit) | An e-commerce data-analytics agent that demonstrates a full AgentOps pipeline: Gateway → Observability → Evaluation → Improvement. AWS Seoul Summit 2026 demo. |
| [claude-code-to-agentcore](./projects/claude-code-to-agentcore) | A step-by-step guide to promoting a Claude Code data-analytics agent (Skill, MCP, web search) to production on Amazon Bedrock AgentCore Runtime, Gateway, and Web Search with minimal code changes. |
| [agentic-text-to-sql-on-aws](./projects/agentic-text-to-sql-on-aws) | A production-oriented agentic text-to-SQL solution on Amazon Bedrock AgentCore, where a Strands Graph orchestrator turns natural language into safe, validated SQL using Runtime-hosted MCP tools and streams results to a CopilotKit UI over the AG-UI protocol. |

> This table is updated whenever a new project is added.

## Tooling

| Tool | Description |
| --- | --- |
| [3a-plugin](./plugin) | A Claude Code plugin that helps you plan, track, and verify AWS-based AI agent projects. Use it as a reference companion while building. |

## Contributing

To add a new sample, see [CONTRIBUTING.md](CONTRIBUTING.md). Each project should be a self-contained folder with its own README and run instructions.

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
