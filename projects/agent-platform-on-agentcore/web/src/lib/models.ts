/**
 * Bedrock model catalog, used when composing or editing a harness and in the
 * per-thread override popover.
 *
 * The harness definition owns the default model. A chat bound to a harness may
 * still override it per thread: InvokeHarness takes a per-request `model`
 * (measured 2026-09-23 — an earlier note here said it did not). Runtime agents
 * have no such field, so the chat offers no picker for them.
 */

export interface BedrockModel {
  id: string;
  label: string;
  provider: string;
}

export const BEDROCK_MODELS: BedrockModel[] = [
  {
    id: "global.anthropic.claude-sonnet-5-5",
    label: "Claude Sonnet 5.5",
    provider: "Anthropic",
  },
  {
    id: "global.anthropic.claude-opus-5-5",
    label: "Claude Opus 5.5",
    provider: "Anthropic",
  },
  {
    id: "global.anthropic.claude-opus-5",
    label: "Claude Opus 5",
    provider: "Anthropic",
  },
  {
    id: "global.anthropic.claude-opus-4-8",
    label: "Claude Opus 4.8",
    provider: "Anthropic",
  },
  {
    id: "global.anthropic.claude-sonnet-5",
    label: "Claude Sonnet 5",
    provider: "Anthropic",
  },
  {
    id: "global.anthropic.claude-sonnet-4-6",
    label: "Claude Sonnet 4.6",
    provider: "Anthropic",
  },
  {
    id: "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    label: "Claude Haiku 4.5",
    provider: "Anthropic",
  },
  {
    id: "global.openai.gpt-5.6-sol",
    label: "GPT-5.6 Sol",
    provider: "OpenAI",
  },
  {
    id: "global.openai.gpt-5.6-terra",
    label: "GPT-5.6 Terra",
    provider: "OpenAI",
  },
  {
    id: "global.openai.gpt-5.6-luna",
    label: "GPT-5.6 Luna",
    provider: "OpenAI",
  },
  {
    id: "qwen.qwen3-next-80b-a3b",
    label: "Qwen3 Next 80B A3B",
    provider: "Qwen",
  },
  {
    id: "qwen.qwen3-vl-235b-a22b",
    label: "Qwen3 VL 235B A22B",
    provider: "Qwen",
  },
  {
    id: "qwen.qwen3-coder-next",
    label: "Qwen3 Coder Next",
    provider: "Qwen",
  },
  { id: "global.xai.grok-4.6", label: "Grok 4.6", provider: "xAI" },
  { id: "google.gemma-3-27b-it", label: "Gemma 3 27B", provider: "Google" },
  { id: "google.gemma-3-12b-it", label: "Gemma 3 12B", provider: "Google" },
  {
    id: "moonshotai.kimi-k2.5",
    label: "Kimi K2.5",
    provider: "Moonshot",
  },
];
