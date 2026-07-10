// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * RESILIENCE-02: Retry utility with configurable retry conditions.
 *
 * Wraps any async function and retries it on network failure or specified HTTP status codes.
 * The caller throws APIError; callers can distinguish transient vs permanent errors.
 */

export class APIError extends Error {
  constructor(
    public status: number,
    public error_code: string,
    message: string,
    public details: unknown = null
  ) {
    super(message);
    this.name = 'APIError';
  }
}

export interface RetryOptions {
  /** HTTP status codes that should trigger a retry. Defaults to [503]. */
  retryOn?: number[];
  /** Maximum number of retry attempts (not counting the initial attempt). Defaults to 1. */
  maxRetries?: number;
}

/**
 * Executes `fn` and retries up to `maxRetries` times when:
 *  - The promise rejects with a network/fetch error (TypeError)
 *  - The thrown error is an APIError whose status matches one of `retryOn`
 *
 * @example
 * const data = await withRetry(() => fetchSomeData(), { retryOn: [503], maxRetries: 2 });
 */
export async function withRetry<T>(fn: () => Promise<T>, options?: RetryOptions): Promise<T> {
  const retryOn = options?.retryOn ?? [503];
  const maxRetries = options?.maxRetries ?? 1;

  let lastError: unknown;

  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      return await fn();
    } catch (err) {
      lastError = err;

      const isNetworkError = err instanceof TypeError;
      const isRetryableStatus = err instanceof APIError && retryOn.includes(err.status);

      if (!isNetworkError && !isRetryableStatus) {
        // Non-retryable error — re-throw immediately
        throw err;
      }

      if (attempt >= maxRetries) {
        // Exhausted retries — propagate the last error
        break;
      }

      // Optional: exponential back-off could be added here in the future
    }
  }

  throw lastError;
}
