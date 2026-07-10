// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import type { AdminSession } from '@/types/entities';
import type { UserRole } from '@/types/enums';
import { PAGE_PERMISSIONS } from './permissions';

/**
 * Decodes a JWT without verifying the signature.
 *
 * Middleware only checks cookie presence; signature verification is handled
 * server-side by the API. This utility is used client-side / in RSC to read
 * the payload for display and permission gating.
 *
 * @throws {Error} When the token is malformed or the payload cannot be parsed.
 */
export function parseJWT(token: string): AdminSession {
  const parts = token.split('.');

  if (parts.length !== 3) {
    throw new Error('Invalid JWT format: expected 3 dot-separated segments');
  }

  const payloadSegment = parts[1];

  // Base64URL → Base64 → JSON
  const base64 = payloadSegment.replace(/-/g, '+').replace(/_/g, '/');
  const padded = base64.padEnd(base64.length + ((4 - (base64.length % 4)) % 4), '=');

  let jsonString: string;
  try {
    jsonString = atob(padded);
  } catch {
    throw new Error('Failed to decode JWT payload: invalid base64');
  }

  let payload: Record<string, unknown>;
  try {
    payload = JSON.parse(jsonString) as Record<string, unknown>;
  } catch {
    throw new Error('Failed to parse JWT payload: invalid JSON');
  }

  return {
    user_id: String(payload['sub'] ?? payload['user_id'] ?? ''),
    email: String(payload['email'] ?? ''),
    display_name: String(payload['display_name'] ?? payload['name'] ?? ''),
    role: payload['role'] as UserRole,
    team_id: (payload['team_id'] as string | null) ?? null,
    department_id: (payload['department_id'] as string | null) ?? null,
    issued_at: String(payload['iat'] ?? ''),
    expires_at: String(payload['exp'] ?? ''),
  };
}

/**
 * Checks whether `role` is allowed to access the given `pathname`.
 *
 * Uses prefix matching: `/budgets/123` is covered by the `/budgets` entry.
 * Falls back to `false` (deny) for paths not listed in PAGE_PERMISSIONS.
 */
export function checkPagePermission(pathname: string, role: UserRole): boolean {
  // Find the most specific (longest) matching prefix
  const matchingEntry = Object.entries(PAGE_PERMISSIONS)
    .filter(([prefix]) => {
      if (prefix === '/') {
        // Root matches only the exact path '/' to avoid swallowing everything
        return pathname === '/';
      }
      return pathname === prefix || pathname.startsWith(`${prefix}/`);
    })
    .sort(([a], [b]) => b.length - a.length)[0];

  if (!matchingEntry) {
    return false; // No permission entry found — deny by default
  }

  const [, allowedRoles] = matchingEntry;
  return allowedRoles.includes(role);
}
