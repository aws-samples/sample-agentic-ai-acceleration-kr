"use client";

import {
  Badge,
  StatusDot,
  humanizeStatus,
  statusVariant,
} from "@/components/ui/badge";
import type { RegistryRecordDetail, StatusAction } from "@/lib/registry";
import type { HarnessSummary } from "@/lib/harness";
import { epochOf } from "@/app/insights/threadRows.mjs";

// Record names are constrained by the AWS API.
export const NAME_PATTERN = /^[a-zA-Z0-9][a-zA-Z0-9_\-./]*$/;

export const STATUS_ACTIONS: StatusAction[] = ["submit", "approve", "reject", "deprecate"];

/**
 * A record's lifecycle status, as a dot + label chip.
 *
 * The colour mapping lives in `statusVariant` (badge.tsx) and is shared with the
 * harness and knowledge pages, which each used to carry their own near-identical
 * copy returning hardcoded pastel classes.
 */
export function StatusBadge({
  status,
  className,
}: {
  status?: string | null;
  className?: string;
}) {
  if (!status) return null;
  const variant = statusVariant(status);
  return (
    <Badge
      shape="chip"
      variant={variant}
      // The raw SCREAMING_SNAKE value stays reachable on hover, since it is what
      // the API returns and what a bug report needs to quote.
      title={status}
      className={className}
    >
      <StatusDot variant={variant} />
      {humanizeStatus(status)}
    </Badge>
  );
}

export function formatTime(value?: string | null): string {
  if (!value) return "—";
  const ms = epochOf(value);
  if (ms === null) return value;
  const date = new Date(ms);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export function Field({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div>
      <p className="mb-0.5 caps-label-xs text-muted-foreground">{label}</p>
      <p className={"break-all text-xs" + (mono ? " font-mono" : "")}>{value}</p>
    </div>
  );
}

/** Shape of the parsed descriptor payloads the server returns per record type. */
type DescriptorContent = {
  server?: {
    name?: string;
    description?: string;
    version?: string;
    remotes?: Array<{ type?: string; url?: string }>;
    /** Present when the record was registered from an AgentCore gateway. */
    gatewayArn?: string;
  };
  tools?: unknown;
  skillMd?: string;
  skillDefinition?: {
    /**
     * Reverse-DNS extension metadata. The registry has nowhere to put a skill's
     * `references/` or `scripts/` — its descriptor is two inline strings — so the
     * bundle lives in S3 and this is where the record says where.
     */
    _meta?: Record<string, { type?: string; uri?: string; files?: string[] }>;
  };
};

/** The key the server writes a skill's bundle location under. */
const SKILL_SOURCE_META_KEY = "com.amazonaws.bap/skillSource";

export function toolList(tools: unknown): Array<{ name: string; description?: string }> {
  const raw = Array.isArray(tools)
    ? tools
    : Array.isArray((tools as { tools?: unknown })?.tools)
      ? (tools as { tools: unknown[] }).tools
      : [];
  return raw.flatMap((tool) => {
    if (typeof tool === "string") return [{ name: tool }];
    if (tool && typeof tool === "object") {
      const t = tool as { name?: string; description?: string };
      if (t.name) return [{ name: t.name, description: t.description }];
    }
    return [];
  });
}

/** Reads the MCP endpoint out of a descriptor the way the server's `_mcp_url` does. */
export function mcpEndpoint(record: RegistryRecordDetail): string | null {
  const content = (record.descriptor_content ?? null) as DescriptorContent | null;
  return content?.server?.remotes?.find((r) => r.url)?.url ?? null;
}

export function mcpGatewayArn(record: RegistryRecordDetail): string | null {
  const content = (record.descriptor_content ?? null) as DescriptorContent | null;
  return content?.server?.gatewayArn ?? null;
}

/**
 * The S3 prefix holding a skill record's bundle, or null.
 *
 * Null for a record whose SKILL.md was typed in rather than uploaded — every
 * skill registered before bundle uploads existed. Mirrors the server's
 * `skill_source_of`, including its "must be an s3:// URI" check.
 */
export function skillSourceUri(record: RegistryRecordDetail): string | null {
  const content = (record.descriptor_content ?? null) as DescriptorContent | null;
  const uri = content?.skillDefinition?._meta?.[SKILL_SOURCE_META_KEY]?.uri;
  return typeof uri === "string" && uri.startsWith("s3://") ? uri : null;
}

/**
 * Renders the descriptor in the shape that matters for the record's type.
 *
 * For MCP records this is deliberately only what identifies the *server* — its
 * name, what kind it is, and what it can do. The endpoint URL and gateway ARN
 * are deployment coordinates: they tell an operator where the thing lives and
 * mean nothing to someone picking a tool surface, so they sit in the admin-only
 * System info fold with the record ARNs (see `McpSystemInfo`). Agent records
 * already worked this way; this brings MCP in line.
 */
export function DescriptorDetail({ record }: { record: RegistryRecordDetail }) {
  const content = (record.descriptor_content ?? null) as DescriptorContent | null;
  if (!content || typeof content !== "object") return null;

  if (record.descriptor_type === "MCP") {
    const tools = toolList(content.tools);
    return (
      <div className="space-y-3">
        <p className="font-medium">
          {content.server?.gatewayArn ? "AgentCore Gateway" : "MCP server"}
        </p>
        {content.server?.name && (
          <Field label="Server name" value={content.server.name} mono />
        )}
        {tools.length > 0 && (
          <div>
            <p className="mb-1.5 caps-label-xs text-muted-foreground">
              Tools ({tools.length})
            </p>
            {/* A single bordered block with internal dividers, rather than one
                bordered box per tool — at a dozen tools the stack of separate
                outlines was busier than the content. */}
            <ul className="divide-y divide-border overflow-hidden rounded-md border border-border">
              {tools.map((tool) => (
                <li key={tool.name} className="px-2.5 py-2">
                  <span className="font-mono text-xs">{tool.name}</span>
                  {tool.description && (
                    <span className="mt-0.5 block text-xs leading-normal text-muted-foreground">
                      {tool.description}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    );
  }

  if (record.descriptor_type === "AGENT_SKILLS" && content.skillMd) {
    return (
      <div>
        <p className="mb-1.5 caps-label-xs text-muted-foreground">SKILL.md</p>
        <pre className="max-h-72 overflow-auto whitespace-pre-wrap text-xs">
          {content.skillMd}
        </pre>
      </div>
    );
  }

  return null;
}

/**
 * MCP deployment coordinates, for the admin-only System info fold.
 *
 * Split out of `DescriptorDetail` so the panel's top half stays about what the
 * server *is*. The gateway note lives here too: it explains why the endpoint
 * column reads the way it does, which only matters to whoever is reading ARNs.
 */
export function McpSystemInfo({ record }: { record: RegistryRecordDetail }) {
  if (record.descriptor_type !== "MCP") return null;

  const url = mcpEndpoint(record);
  const gatewayArn = mcpGatewayArn(record);

  return (
    <>
      {gatewayArn && <Field label="Gateway ARN" value={gatewayArn} mono />}
      <Field
        label="Endpoint"
        value={
          url ||
          (gatewayArn
            ? "ARN으로 조합됩니다"
            : "등록되지 않음 — Agent Harness에서 조합할 수 없습니다")
        }
        mono={Boolean(url)}
      />
      {gatewayArn && (
        <p className="text-xs leading-normal text-muted-foreground">
          Gateway는 SigV4 인증이 필요하므로 Agent Harness에서 전용 gateway 도구로
          붙습니다. 일반 MCP 엔드포인트로는 호출할 수 없습니다.
        </p>
      )}
    </>
  );
}

/** Live harness configuration, for records created by the Agent Harness composer. */
export function HarnessDetail({ harness }: { harness: HarnessSummary }) {
  const tools = (harness.tools ?? []) as Array<{ type?: string; name?: string }>;
  const skills = (harness.skills ?? []) as Array<Record<string, unknown>>;

  const describeSkill = (skill: Record<string, unknown>): string => {
    const s3 = skill.s3 as { uri?: string } | undefined;
    if (s3?.uri) return s3.uri;
    const git = skill.git as { url?: string; path?: string } | undefined;
    if (git?.url) return git.path ? `${git.url} (${git.path})` : git.url;
    const aws = skill.awsSkills as { paths?: string[] } | undefined;
    if (aws) return `AWS skills: ${(aws.paths ?? ["*"]).join(", ")}`;
    if (typeof skill.path === "string") return skill.path;
    return JSON.stringify(skill);
  };

  return (
    <div className="space-y-3 rounded-md border border-border bg-muted/40 p-3">
      <p className="text-sm font-semibold tracking-tight">
        Harness configuration
      </p>
      {harness.model_id && <Field label="Model" value={harness.model_id} mono />}
      <div>
        <p className="mb-1 caps-label-xs text-muted-foreground">
          Tools ({tools.length})
        </p>
        {tools.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            추가 도구 없음 (shell, file_operations는 기본 제공)
          </p>
        ) : (
          <ul className="flex flex-wrap gap-1">
            {tools.map((tool, i) => (
              <li key={`${tool.name}-${i}`}>
                <Badge shape="code" variant="secondary">
                  {tool.name || tool.type}
                </Badge>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div>
        <p className="mb-1 caps-label-xs text-muted-foreground">
          Skills ({skills.length})
        </p>
        {skills.length === 0 ? (
          <p className="text-xs text-muted-foreground">없음</p>
        ) : (
          <ul className="space-y-1">
            {skills.map((skill, i) => (
              <li key={i} className="break-all font-mono text-xs">
                {describeSkill(skill)}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
