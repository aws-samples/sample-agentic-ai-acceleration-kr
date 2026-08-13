// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * Regression tests for the consistency between the model add/edit form, the zod schema,
 * and the backend request schema.
 *
 * Background (real customer outage): ModelCreateSchema required `max_tokens`/`context_window`,
 * but the CreateModelDialog form has no inputs for either field and never puts them in the
 * payload. The backend ModelCreateRequest and the model.model_aliases table have no
 * corresponding fields either.
 * Result: every model add/edit always failed safeParse → the screen showed only
 * "Validation failed". Worse, neither key has a matching <input>, so their fieldErrors were
 * never rendered and the cause stayed invisible.
 *
 * These tests validate against "the payload the form actually builds", so putting a field
 * the form does not have back into the schema fails immediately.
 */

import { describe, it, expect } from 'vitest';
import { ModelCreateSchema } from '@/types/api';

/**
 * Reproduces the payload construction in CreateModelDialog.handleSubmit exactly.
 * (The form FormState is all strings; numbers go through parseFloat.)
 */
function buildPayloadLikeDialog(form: {
  alias: string;
  provider: string;
  model_id: string;
  endpoint_url: string;
  input_price_per_1k: string;
  output_price_per_1k: string;
  cache_creation_5m_price_per_1k: string;
  cache_creation_1h_price_per_1k: string;
  cache_read_price_per_1k: string;
  description: string;
  display_name: string;
}) {
  return {
    alias: form.alias,
    provider: form.provider,
    model_id: form.model_id,
    endpoint_url: form.endpoint_url,
    input_price_per_1k: parseFloat(form.input_price_per_1k),
    output_price_per_1k: parseFloat(form.output_price_per_1k),
    cache_creation_5m_price_per_1k: parseFloat(form.cache_creation_5m_price_per_1k || '0'),
    cache_creation_1h_price_per_1k: parseFloat(form.cache_creation_1h_price_per_1k || '0'),
    cache_read_price_per_1k: parseFloat(form.cache_read_price_per_1k || '0'),
    description: form.description || undefined,
    display_name: form.display_name || undefined,
  };
}

/** The GPT-5.6 Sol registration values the customer actually entered (from the screenshot). */
const GPT56_SOL_FORM = {
  alias: 'codex-gpt-5.6-sol',
  provider: 'BEDROCK_MANTLE_OPENAI',
  model_id: 'openai.gpt-5.6-sol',
  endpoint_url: 'https://bedrock-mantle.us-east-2.api.aws/openai',
  input_price_per_1k: '0.011000',
  output_price_per_1k: '0.049500',
  cache_creation_5m_price_per_1k: '0.013750',
  cache_creation_1h_price_per_1k: '0',
  cache_read_price_per_1k: '0.001100',
  description: '',
  display_name: 'Codex · GPT-5.6 Sol',
};

describe('ModelCreateSchema — form payload consistency', () => {
  it('accepts the GPT-5.6 Sol registration payload the customer entered (regression: Validation failed)', () => {
    const parsed = ModelCreateSchema.safeParse(buildPayloadLikeDialog(GPT56_SOL_FORM));

    // Surface the issues so a failure immediately shows which field blocked it.
    const issues = parsed.success
      ? []
      : parsed.error.issues.map((i) => `${i.path.join('.')}: ${i.message}`);
    expect(issues).toEqual([]);
    expect(parsed.success).toBe(true);
  });

  it('allows registering every Mantle/Bedrock/OPENMODEL provider', () => {
    for (const provider of ['BEDROCK', 'OPENMODEL', 'BEDROCK_MANTLE', 'BEDROCK_MANTLE_OPENAI']) {
      const parsed = ModelCreateSchema.safeParse(
        buildPayloadLikeDialog({ ...GPT56_SOL_FORM, provider })
      );
      expect(parsed.success, `provider=${provider}`).toBe(true);
    }
  });

  it('keeps the schema required keys inside the key set the form sends (drift guard)', () => {
    // The keys the form can actually produce. If the schema requires a key that is not
    // listed here, the user can never fill in a value for it.
    const KEYS_THE_FORM_CAN_SEND = new Set([
      'alias',
      'provider',
      'model_id',
      'endpoint_url',
      'input_price_per_1k',
      'output_price_per_1k',
      'cache_creation_5m_price_per_1k',
      'cache_creation_1h_price_per_1k',
      'cache_read_price_per_1k',
      'description',
      'display_name',
    ]);

    // Parse an empty object to extract the keys that fail when no value is given
    // (i.e. effectively required). Keys with optional / .default() do not show up here.
    const empty = ModelCreateSchema.safeParse({});
    const requiredKeys = empty.success
      ? []
      : [...new Set(empty.error.issues.map((i) => i.path.join('.')))];

    const unreachable = requiredKeys.filter((k) => !KEYS_THE_FORM_CAN_SEND.has(k));
    expect(unreachable).toEqual([]);
  });

  it('passes when description and display name are empty (optional fields)', () => {
    const parsed = ModelCreateSchema.safeParse(
      buildPayloadLikeDialog({ ...GPT56_SOL_FORM, description: '', display_name: '' })
    );
    expect(parsed.success).toBe(true);
  });

  it('attaches the error to the field itself when a required field is empty (the hint must be visible on screen)', () => {
    const parsed = ModelCreateSchema.safeParse(
      buildPayloadLikeDialog({ ...GPT56_SOL_FORM, alias: '', model_id: '' })
    );
    expect(parsed.success).toBe(false);
    if (!parsed.success) {
      const keys = parsed.error.issues.map((i) => i.path.join('.'));
      expect(keys).toContain('alias');
      expect(keys).toContain('model_id');
    }
  });

  it('rejects negative unit prices', () => {
    const parsed = ModelCreateSchema.safeParse(
      buildPayloadLikeDialog({ ...GPT56_SOL_FORM, input_price_per_1k: '-0.01' })
    );
    expect(parsed.success).toBe(false);
  });

  it('rejects a missing unit price (NaN) — it must not silently bill at 0', () => {
    // Leaving the input empty makes parseFloat('') === NaN.
    const parsed = ModelCreateSchema.safeParse(
      buildPayloadLikeDialog({ ...GPT56_SOL_FORM, input_price_per_1k: '' })
    );
    expect(parsed.success).toBe(false);
  });
});
