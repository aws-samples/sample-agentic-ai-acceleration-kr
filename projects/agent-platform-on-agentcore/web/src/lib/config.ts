/** The registry agent a chat is bound to. */
export interface SelectedAgent {
  recordId: string;
  name: string;
  description?: string;
  agentRuntimeArn?: string;
  /** Set for managed-harness agents, which are invoked via InvokeHarness. */
  harnessArn?: string;
  qualifier?: string;
}

export interface StandaloneConfig {
  /**
   * Agent selected from the Registry; chat invokes its AgentCore runtime or
   * harness.
   *
   * There is deliberately no model here: the default model belongs to the
   * agent definition, and a per-thread override lives on the thread's metadata
   * (see lib/agent-config.ts), not in this browser-wide config, so it cannot
   * leak from one conversation to another.
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
