/**
 * API types matching the FastAPI server models
 * These replace LangGraph SDK types
 */

export type ThreadStatus = "idle" | "busy" | "interrupted" | "error";

export type MessageType = "human" | "ai" | "system" | "tool";

export interface MessageContent {
  text?: string;
  type?: string;
  [key: string]: any;
}

export interface Message {
  id: string;
  type: MessageType;
  content: string | (string | MessageContent)[];
  name?: string;
  tool_calls?: Array<{
    id?: string;
    name: string;
    args: Record<string, any>;
  }>;
  // Provider-specific extras (e.g. OpenAI-style tool_calls) carried alongside the message.
  additional_kwargs?: {
    tool_calls?: Array<Record<string, any>>;
    [key: string]: any;
  };
  tool_call_id?: string;
  delta?: boolean; // Flag indicating this is a delta update (content should be accumulated)
  reasoning?: string; // Reasoning content from the model
}

export interface Checkpoint {
  v?: number;
  id: string;
  ts: string;
  channel_values?: Record<string, any>;
  channel_versions?: Record<string, any>;
  versions_seen?: Record<string, any>;
}

export interface Thread<T extends Record<string, any> = Record<string, any>> {
  thread_id: string;
  created_at: string;
  updated_at: string;
  values?: T;
  status: ThreadStatus;
  metadata?: Record<string, any>;
  /**
   * Registry record of the agent bound to this thread. Empty on threads created
   * before pinning existed; those cannot be continued (the server answers 409).
   */
  agent_record_id?: string;
  /** The agent's name when the thread was pinned — a label, never a decision. */
  agent_name?: string;
  /** Model pinned onto a basic-chat thread; absent on older basic threads. */
  basic_chat_model_id?: string | null;
}

export interface Interrupt {
  id: string;
  value: any;
  [key: string]: any;
}

