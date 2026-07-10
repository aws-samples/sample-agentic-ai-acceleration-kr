// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * AdminAPIClient — server-side only.
 *
 * Wraps the admin-api backend with automatic cookie forwarding and
 * structured error handling (PERF-01: cache: 'no-store').
 */

import { cookies } from 'next/headers';
import { APIError } from '@/lib/utils/retry';

const ADMIN_API_URL = process.env.ADMIN_API_URL || 'http://admin-api:8080';

class AdminAPIClient {
  private async fetch<T>(path: string, options?: RequestInit): Promise<T> {
    const cookieStore = cookies();
    const url = `${ADMIN_API_URL}${path}`;

    const response = await fetch(url, {
      ...options,
      cache: 'no-store',
      headers: {
        'Content-Type': 'application/json',
        Cookie: cookieStore.toString(),
        ...options?.headers,
      },
    });

    if (!response.ok) {
      let errorBody: { error_code?: string; message?: string; detail?: string; error?: { code?: string; message?: string } } = {};
      try {
        errorBody = (await response.json()) as typeof errorBody;
      } catch {
        // Response body may not be JSON — use empty defaults
      }

      throw new APIError(
        response.status,
        errorBody.error_code ?? errorBody.error?.code ?? 'UNKNOWN_ERROR',
        errorBody.message ?? errorBody.error?.message ?? errorBody.detail ?? `Request failed with status ${response.status}`,
        errorBody.detail ?? undefined
      );
    }

    // 204 No Content — return undefined cast as T
    if (response.status === 204) {
      return undefined as unknown as T;
    }

    return response.json() as Promise<T>;
  }

  async get<T>(
    path: string,
    params?: Record<string, string | number | undefined>
  ): Promise<T> {
    let url = path;

    if (params) {
      const search = new URLSearchParams();
      for (const [key, value] of Object.entries(params)) {
        if (value !== undefined) {
          search.set(key, String(value));
        }
      }
      const queryString = search.toString();
      if (queryString) {
        url = `${path}?${queryString}`;
      }
    }

    return this.fetch<T>(url, { method: 'GET' });
  }

  async post<T>(path: string, body: unknown): Promise<T> {
    return this.fetch<T>(path, {
      method: 'POST',
      body: JSON.stringify(body),
    });
  }

  async put<T>(path: string, body: unknown): Promise<T> {
    return this.fetch<T>(path, {
      method: 'PUT',
      body: JSON.stringify(body),
    });
  }

  async patch<T>(path: string, body: unknown): Promise<T> {
    return this.fetch<T>(path, {
      method: 'PATCH',
      body: JSON.stringify(body),
    });
  }

  async delete<T>(path: string): Promise<T> {
    return this.fetch<T>(path, { method: 'DELETE' });
  }
}

export const adminAPI = new AdminAPIClient();
