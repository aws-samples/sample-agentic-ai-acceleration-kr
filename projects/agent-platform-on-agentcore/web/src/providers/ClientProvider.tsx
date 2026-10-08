"use client";

import { createContext, useContext, useMemo, ReactNode } from "react";
import { ApiClient } from "@/lib/api-client";

interface ClientContextValue {
  apiClient: ApiClient;
}

const ClientContext = createContext<ClientContextValue | null>(null);

interface ClientProviderProps {
  children: ReactNode;
}

export function ClientProvider({
  children,
}: ClientProviderProps) {
  const apiClient = useMemo(() => {
    // Same-origin by default (goes through the Next.js rewrite proxy);
    // override with NEXT_PUBLIC_API_URL when the backend is elsewhere.
    const apiUrl = process.env.NEXT_PUBLIC_API_URL || "";
    return new ApiClient({
      apiUrl,
    });
  }, []);

  const value = useMemo(() => ({ apiClient }), [apiClient]);

  return (
    <ClientContext.Provider value={value}>{children}</ClientContext.Provider>
  );
}

export function useClient(): ApiClient {
  const context = useContext(ClientContext);

  if (!context) {
    throw new Error("useClient must be used within a ClientProvider");
  }
  return context.apiClient;
}
