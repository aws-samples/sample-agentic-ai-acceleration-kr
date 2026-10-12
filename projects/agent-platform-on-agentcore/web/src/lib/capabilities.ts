/** Server capability flags (GET /api/config). Used to hide features the server can't serve. */
import { authedFetch } from "@/lib/http";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

export interface Capabilities {
  registryEnabled: boolean;
  harnessEnabled: boolean;
  /**
   * Models a per-thread override may pick, for every agent, in the operator's
   * order. The server refuses any other, so the override popover offers exactly
   * this. Absent (older server) or empty = no model override is offered.
   */
  allowedModels?: string[];
}

export async function getCapabilities(): Promise<Capabilities> {
  const res = await authedFetch(`${API_BASE}/api/config`);
  if (!res.ok) {
    // A missing/failed capability probe must not blank the UI — assume the
    // conservative "registry off" so fallbacks (which always work) are shown.
    return { registryEnabled: false, harnessEnabled: true, allowedModels: [] };
  }
  return res.json();
}
