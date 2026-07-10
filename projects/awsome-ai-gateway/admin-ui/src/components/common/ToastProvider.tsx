'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from 'react';
import type { ToastNotification } from '@/types/entities';
import { X } from 'lucide-react';

// ─── Context ──────────────────────────────────────────────────────────────────

interface ToastItem extends ToastNotification {
  id: string;
}

type ToastContextType = {
  toast: (notification: ToastNotification) => void;
};

export const ToastContext = createContext<ToastContextType>({
  toast: () => {},
});

// ─── Provider ─────────────────────────────────────────────────────────────────

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const counterRef = useRef(0);

  const dismiss = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const toast = useCallback(
    (notification: ToastNotification) => {
      const id = `toast-${Date.now()}-${++counterRef.current}`;
      const item: ToastItem = { ...notification, id };
      setToasts((prev) => [...prev, item]);
    },
    []
  );

  return (
    <ToastContext.Provider value={{ toast }}>
      {children}
      <ToastStack toasts={toasts} onDismiss={dismiss} />
    </ToastContext.Provider>
  );
}

// ─── Hook ─────────────────────────────────────────────────────────────────────

export function useToast(): ToastContextType {
  return useContext(ToastContext);
}

// ─── Toast Stack ──────────────────────────────────────────────────────────────

interface ToastStackProps {
  toasts: ToastItem[];
  onDismiss: (id: string) => void;
}

function ToastStack({ toasts, onDismiss }: ToastStackProps) {
  if (toasts.length === 0) return null;

  return (
    <div
      aria-live="polite"
      aria-atomic="false"
      className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 w-80"
    >
      {toasts.map((item) => (
        <ToastItem key={item.id} item={item} onDismiss={onDismiss} />
      ))}
    </div>
  );
}

// ─── Individual Toast ─────────────────────────────────────────────────────────

const TYPE_CLASSES: Record<ToastNotification['type'], string> = {
  success: 'bg-success text-success-foreground border-success/20',
  error: 'bg-destructive text-destructive-foreground border-destructive/20',
  warning: 'bg-warning text-warning-foreground border-warning/20',
  info: 'bg-primary text-primary-foreground border-primary/20',
};

const TYPE_ICONS: Record<ToastNotification['type'], React.ReactNode> = {
  success: (
    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M20 6 9 17l-5-5" />
    </svg>
  ),
  error: (
    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="10" />
      <line x1="15" y1="9" x2="9" y2="15" />
      <line x1="9" y1="9" x2="15" y2="15" />
    </svg>
  ),
  warning: (
    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z" />
      <line x1="12" y1="9" x2="12" y2="13" />
      <line x1="12" y1="17" x2="12.01" y2="17" />
    </svg>
  ),
  info: (
    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="10" />
      <line x1="12" y1="8" x2="12" y2="12" />
      <line x1="12" y1="16" x2="12.01" y2="16" />
    </svg>
  ),
};

interface ToastItemProps {
  item: ToastItem;
  onDismiss: (id: string) => void;
}

function ToastItem({ item, onDismiss }: ToastItemProps) {
  useEffect(() => {
    if (item.auto_dismiss_ms === null) return;
    const timer = setTimeout(() => onDismiss(item.id), item.auto_dismiss_ms);
    return () => clearTimeout(timer);
  }, [item.id, item.auto_dismiss_ms, onDismiss]);

  return (
    <div
      role="alert"
      className={[
        'flex items-start gap-3 rounded-lg border p-4 shadow-lg',
        TYPE_CLASSES[item.type],
      ].join(' ')}
    >
      <span className="flex-shrink-0 mt-0.5">{TYPE_ICONS[item.type]}</span>
      <p className="flex-1 text-sm font-medium leading-snug">{item.message}</p>
      <button
        onClick={() => onDismiss(item.id)}
        className="flex-shrink-0 rounded-sm opacity-70 hover:opacity-100 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring transition-opacity"
        aria-label="닫기"
      >
        <X size={14} aria-hidden="true" />
      </button>
    </div>
  );
}