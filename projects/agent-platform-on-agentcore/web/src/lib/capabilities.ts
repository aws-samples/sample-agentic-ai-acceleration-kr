/** Server capability flags (GET /api/config). Used to hide features the server can't serve. */
import { authedFetch } from "@/lib/http";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

export interface BasicChatCapability {
  configured: boolean;
  /** The server's allow-list; a turn naming any other model is refused. */
  models: string[];
}

export interface Capabilities {
  registryEnabled: boolean;
  harnessEnabled: boolean;
  /** Absent from servers older than basic chat; treat as not configured. */
  basicChat?: BasicChatCapability;
}

export async function getCapabilities(): Promise<Capabilities> {
  const res = await authedFetch(`${API_BASE}/api/config`);
  if (!res.ok) {
    // A missing/failed capability probe must not blank the UI — assume the
    // conservative "registry off" so fallbacks (which always work) are shown.
    return { registryEnabled: false, harnessEnabled: true };
  }
  return res.json();
}
