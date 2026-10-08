/**
 * Platform settings: which sidebar menus a plain user sees.
 *
 * The read is open to every signed-in user (the sidebar needs it before it can
 * render); the write is admin-only. Unknown keys are dropped by the server, so
 * the list that comes back is the list that is in force.
 */
import { authedFetch } from "./http";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

/** The menus an admin may hide from plain users, in sidebar order. */
export type MenuKey = "knowledge" | "registry" | "harness";

export const MENU_LABELS: Record<MenuKey, string> = {
  knowledge: "Knowledge",
  registry: "Registry",
  harness: "Agent Harness",
};

export interface NavVisibility {
  hidden: MenuKey[];
  menus: MenuKey[];
  /** False when the preferences table is unconfigured or unreachable — nothing hidden, nothing saved. */
  persisted: boolean;
}

export class SettingsApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "SettingsApiError";
    this.status = status;
  }
}

async function request<T>(endpoint: string, init?: RequestInit): Promise<T> {
  const response = await authedFetch(`${API_BASE}${endpoint}`, init);
  if (!response.ok) {
    const err = await response.json().catch(() => ({ detail: response.statusText }));
    throw new SettingsApiError(response.status, err.detail || `HTTP ${response.status}`);
  }
  return response.json();
}

export function fetchNavVisibility(): Promise<NavVisibility> {
  return request<NavVisibility>("/api/settings/nav");
}

export function putNavVisibility(hidden: MenuKey[]): Promise<NavVisibility> {
  return request<NavVisibility>("/api/settings/nav", {
    method: "PUT",
    body: JSON.stringify({ hidden }),
  });
}
