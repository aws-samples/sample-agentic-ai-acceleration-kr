// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * Create-model dialog — the reason a validation failed must be visible on screen.
 *
 * Background (real customer outage): the schema required keys the form does not
 * have (max_tokens/context_window), so registration always failed, and because
 * those keys have no matching <input> their fieldErrors were never rendered.
 * Users only saw a bare "Validation failed" with no cause, leaving them unable
 * to diagnose it themselves.
 *
 * Here we stub the server action so it returns a field error for a key that is
 * not on the form, and assert that the key name shows up in the on-screen text.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { CreateModelDialog } from '@/components/models/CreateModelDialog';

const createModelAction = vi.fn();
const updateModelAction = vi.fn();

vi.mock('@/lib/actions/models', () => ({
  createModelAction: (...a: unknown[]) => createModelAction(...a),
  updateModelAction: (...a: unknown[]) => updateModelAction(...a),
}));

// next-intl throws without a message provider — stub it to return the key as-is.
vi.mock('next-intl', () => ({
  useTranslations: () => (key: string) => key,
}));

const toast = vi.fn();
vi.mock('@/components/common/ToastProvider', () => ({
  useToast: () => ({ toast }),
}));

/** Fill the required inputs so submit is not blocked by browser required validation. */
function fillRequiredFields() {
  fireEvent.change(screen.getByLabelText(/Alias/), {
    target: { name: 'alias', value: 'codex-gpt-5.6-sol' },
  });
  fireEvent.change(screen.getByLabelText(/Provider/), {
    target: { name: 'provider', value: 'BEDROCK_MANTLE_OPENAI' },
  });
  fireEvent.change(screen.getByLabelText(/Model ID/), {
    target: { name: 'model_id', value: 'openai.gpt-5.6-sol' },
  });
  fireEvent.change(screen.getByLabelText('priceInput'), {
    target: { name: 'input_price_per_1k', value: '0.011' },
  });
  fireEvent.change(screen.getByLabelText('priceOutput'), {
    target: { name: 'output_price_per_1k', value: '0.0495' },
  });
}

describe('CreateModelDialog — validation failure observability', () => {
  beforeEach(() => {
    createModelAction.mockReset();
    updateModelAction.mockReset();
    toast.mockReset();
  });

  it('surfaces field errors for keys with no input on the form (regression: Validation failed with no cause)', async () => {
    createModelAction.mockResolvedValue({
      success: false,
      error: 'Validation failed',
      // Keys with no matching <input> on the form — these used to be dropped silently.
      fieldErrors: { max_tokens: 'Required', context_window: 'Required' },
    });

    render(<CreateModelDialog isOpen onClose={() => {}} />);
    fillRequiredFields();
    fireEvent.submit(screen.getByRole('button', { name: 'register' }).closest('form')!);

    await waitFor(() => {
      const alerts = screen.getAllByRole('alert').map((n) => n.textContent ?? '');
      const joined = alerts.join(' | ');
      expect(joined).toContain('Validation failed');
      // The point: the offending key must be visible on screen.
      expect(joined).toContain('max_tokens');
      expect(joined).toContain('context_window');
    });
  });

  it('shows errors for keys that do have an input only under that field, without polluting the top-level message', async () => {
    createModelAction.mockResolvedValue({
      success: false,
      error: 'Validation failed',
      fieldErrors: { alias: 'Alias is required' },
    });

    render(<CreateModelDialog isOpen onClose={() => {}} />);
    fillRequiredFields();
    fireEvent.submit(screen.getByRole('button', { name: 'register' }).closest('form')!);

    await waitFor(() => {
      const joined = screen.getAllByRole('alert').map((n) => n.textContent ?? '').join(' | ');
      expect(joined).toContain('Alias is required');
      // alias has a matching input, so the key name is not appended to the top summary.
      expect(joined).not.toContain('alias (');
    });
  });

  it('shows a toast and closes on success', async () => {
    createModelAction.mockResolvedValue({ success: true, data: undefined });
    const onClose = vi.fn();

    render(<CreateModelDialog isOpen onClose={onClose} />);
    fillRequiredFields();
    fireEvent.submit(screen.getByRole('button', { name: 'register' }).closest('form')!);

    await waitFor(() => {
      expect(toast).toHaveBeenCalledWith(
        expect.objectContaining({ type: 'success' })
      );
      expect(onClose).toHaveBeenCalled();
    });
  });

  it('does not put fields absent from the form into the submitted payload', async () => {
    createModelAction.mockResolvedValue({ success: true, data: undefined });

    render(<CreateModelDialog isOpen onClose={() => {}} />);
    fillRequiredFields();
    fireEvent.submit(screen.getByRole('button', { name: 'register' }).closest('form')!);

    await waitFor(() => expect(createModelAction).toHaveBeenCalled());
    const payload = createModelAction.mock.calls[0][0] as Record<string, unknown>;
    expect(payload).not.toHaveProperty('max_tokens');
    expect(payload).not.toHaveProperty('context_window');
    expect(payload.alias).toBe('codex-gpt-5.6-sol');
    expect(payload.provider).toBe('BEDROCK_MANTLE_OPENAI');
  });
});
