/** The registry agent a chat is bound to. */
export interface SelectedAgent {
  recordId: string;
  name: string;
  description?: string;
  agentRuntimeArn?: string;
  /** Set for managed-harness agents, which are invoked via InvokeHarness. */
  harnessArn?: string;
  qualifier?: string;
  /**
   * Basic chat: no registry record, the default runtime answers with the model
   * below. The server binds the target and pins the model onto the thread, so
   * this is the only agent for which a model is remembered browser-wide — it is
   * the whole identity of the "agent", not an override of one.
   */
  basicChat?: boolean;
  basicChatModelId?: string;
}

export interface StandaloneConfig {
  /**
   * Agent selected from the Registry; chat invokes its AgentCore runtime.
   *
   * There is deliberately no model here for registry agents: the default model
   * belongs to the agent/harness definition. A per-thread override exists for
   * harness agents (InvokeHarness takes one) but it is kept on the thread's
   * metadata, not in this browser-wide config, so it cannot leak from one
   * conversation to another. Basic chat is the exception, see SelectedAgent.
   */
  selectedAgent?: SelectedAgent;
}

const CONFIG_KEY = "deep-agent-config";

export function getConfig(): StandaloneConfig | null {
  if (typeof window === "undefined") return null;

  const stored = localStorage.getItem(CONFIG_KEY);
  if (!stored) return null;

  try {
    return JSON.parse(stored);
  } catch {
    return null;
  }
}

export function saveConfig(config: StandaloneConfig): void {
  if (typeof window === "undefined") return;
  localStorage.setItem(CONFIG_KEY, JSON.stringify(config));
}
