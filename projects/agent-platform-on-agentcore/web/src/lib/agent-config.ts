/** Identifies the AgentCore harness or runtime that serves this chat. */
export type AgentChatConfig = {
  agentRuntimeArn?: string;
  harnessArn?: string;
  qualifier?: string;
  registryRecordId?: string;
  /** Sent so the server can label the thread without a registry lookup. */
  registryAgentName?: string;
};

/** Maps the selected agent onto the stream request config. */
export function applyAgentConfig(
  config: Record<string, any>,
  agentConfig?: AgentChatConfig
) {
  if (agentConfig?.agentRuntimeArn) {
    config.agent_runtime_arn = agentConfig.agentRuntimeArn;
  }
  if (agentConfig?.harnessArn) {
    config.harness_arn = agentConfig.harnessArn;
  }
  if (agentConfig?.qualifier) {
    config.qualifier = agentConfig.qualifier;
  }
  // The server pins this onto the thread on the first turn and refuses later
  // turns from a different agent with a 409 — see Thread.agent_record_id.
  if (agentConfig?.registryRecordId) {
    config.registry_record_id = agentConfig.registryRecordId;
  }
  if (agentConfig?.registryAgentName) {
    config.registry_agent_name = agentConfig.registryAgentName;
  }
  return config;
}

/**
 * Per-thread overrides for an agent.
 *
 * The server forwards `model_id` / `system_prompt` per turn: InvokeHarness takes
 * them as `model` / `systemPrompt`, and the runtime reads them from the invoke
 * payload (agent-runtime/main.py), rebuilding its agent when the model changes.
 * The agent definition is untouched either way. The model must be on the
 * server's allow-list (GET /api/config → allowedModels) or the turn is refused.
 *
 * Kept per thread (Thread.metadata.harness_overrides) and resent on every turn,
 * so a reopened conversation keeps answering the way it was set up to. The key
 * name predates overrides on runtime agents and stays so existing threads keep
 * their settings.
 */
export type ThreadOverrides = {
  modelId?: string;
  systemPrompt?: string;
};

export const OVERRIDES_METADATA_KEY = "harness_overrides";

export function hasOverrides(overrides?: ThreadOverrides | null): boolean {
  return !!(overrides?.modelId?.trim() || overrides?.systemPrompt?.trim());
}

/** Maps the thread's overrides onto the stream request config. */
export function applyOverrides(
  config: Record<string, any>,
  overrides?: ThreadOverrides | null
) {
  const modelId = overrides?.modelId?.trim();
  const systemPrompt = overrides?.systemPrompt?.trim();
  if (modelId) config.model_id = modelId;
  if (systemPrompt) config.system_prompt = systemPrompt;
  return config;
}

/** Read overrides back out of thread metadata, ignoring anything malformed. */
export function overridesFromMetadata(
  metadata?: Record<string, unknown> | null
): ThreadOverrides {
  const raw = metadata?.[OVERRIDES_METADATA_KEY];
  if (!raw || typeof raw !== "object") return {};
  const { modelId, systemPrompt } = raw as Record<string, unknown>;
  return {
    modelId: typeof modelId === "string" ? modelId : undefined,
    systemPrompt: typeof systemPrompt === "string" ? systemPrompt : undefined,
  };
}
