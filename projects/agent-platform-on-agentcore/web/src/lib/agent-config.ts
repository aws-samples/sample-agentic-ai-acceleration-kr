/** Identifies the AgentCore harness or runtime that serves this chat. */
export type AgentChatConfig = {
  agentRuntimeArn?: string;
  harnessArn?: string;
  qualifier?: string;
  registryRecordId?: string;
  /** Sent so the server can label the thread without a registry lookup. */
  registryAgentName?: string;
  /**
   * Basic chat: the server binds the default runtime and the model itself and
   * refuses a model outside its allow-list, so no ARN or record id is sent.
   */
  basicChat?: boolean;
  basicChatModelId?: string;
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
  if (agentConfig?.basicChat) {
    config.basic_chat = true;
    config.basic_chat_model_id = agentConfig.basicChatModelId;
  }
  return config;
}

/**
 * Per-thread overrides for a harness agent.
 *
 * InvokeHarness accepts `model` and `systemPrompt` per request and applies them
 * to that turn only: the harness definition is untouched, no version is made.
 * That makes them the right tool for "try this agent on another model" without
 * recomposing it. Runtime agents have no such fields, so the control is only
 * offered when the chat is bound to a harness.
 *
 * Kept per thread (Thread.metadata.harness_overrides) and resent on every turn,
 * so a reopened conversation keeps answering the way it was set up to.
 */
export type HarnessOverrides = {
  modelId?: string;
  systemPrompt?: string;
};

export const OVERRIDES_METADATA_KEY = "harness_overrides";

export function hasOverrides(overrides?: HarnessOverrides | null): boolean {
  return !!(overrides?.modelId?.trim() || overrides?.systemPrompt?.trim());
}

/** Maps the thread's overrides onto the stream request config. */
export function applyOverrides(
  config: Record<string, any>,
  overrides?: HarnessOverrides | null
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
): HarnessOverrides {
  const raw = metadata?.[OVERRIDES_METADATA_KEY];
  if (!raw || typeof raw !== "object") return {};
  const { modelId, systemPrompt } = raw as Record<string, unknown>;
  return {
    modelId: typeof modelId === "string" ? modelId : undefined,
    systemPrompt: typeof systemPrompt === "string" ? systemPrompt : undefined,
  };
}
