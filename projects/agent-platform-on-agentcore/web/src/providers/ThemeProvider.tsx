"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";

/** What the user chose. `system` follows the OS setting. */
export type ThemePreference = "light" | "dark" | "system";
/** What that choice resolves to right now. */
export type ResolvedTheme = "light" | "dark";

export const THEME_STORAGE_KEY = "app-theme";

interface ThemeContextValue {
  /** The stored preference, including `system`. */
  preference: ThemePreference;
  /** The theme actually applied. `system` is resolved against the OS. */
  resolved: ResolvedTheme;
  setPreference: (preference: ThemePreference) => void;
  /** Cycles light → dark → system, for the single-button control. */
  cycle: () => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error("useTheme must be used within a ThemeProvider");
  return ctx;
}

/**
 * The script that runs before first paint.
 *
 * This has to be inlined and synchronous in <head>: React only applies the class
 * after hydration, by which point a dark-mode user has already been shown a full
 * white page. Kept deliberately tiny and dependency-free, and it must resolve
 * the theme exactly the way `resolve` below does or the two will disagree on the
 * first frame.
 */
export const THEME_INIT_SCRIPT = `
(function(){try{
var p=localStorage.getItem(${JSON.stringify(THEME_STORAGE_KEY)});
if(p!=="light"&&p!=="dark"&&p!=="system")p="system";
var d=p==="dark"||(p==="system"&&window.matchMedia("(prefers-color-scheme: dark)").matches);
var r=document.documentElement;
r.classList.toggle("dark",d);
r.style.colorScheme=d?"dark":"light";
}catch(e){}})();
`;

function systemTheme(): ResolvedTheme {
  if (typeof window === "undefined") return "light";
  return window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

function resolve(preference: ThemePreference): ResolvedTheme {
  return preference === "system" ? systemTheme() : preference;
}

/**
 * Applies the theme to <html>.
 *
 * `color-scheme` is set alongside the class so that UA-rendered surfaces — form
 * controls, the scrollbar gutter, spellcheck underlines — follow too; the class
 * alone only reaches things our own CSS paints.
 */
function apply(resolved: ResolvedTheme) {
  const root = document.documentElement;
  root.classList.toggle("dark", resolved === "dark");
  root.style.colorScheme = resolved;
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  // Starts at the SSR-safe default and is corrected in the mount effect below.
  // The real first-paint value is set by THEME_INIT_SCRIPT, so this initial
  // state never reaches the screen as a flash.
  const [preference, setPreferenceState] = useState<ThemePreference>("system");
  const [resolved, setResolved] = useState<ResolvedTheme>("light");

  // Adopt whatever the inline script already decided.
  useEffect(() => {
    let stored: string | null = null;
    try {
      stored = localStorage.getItem(THEME_STORAGE_KEY);
    } catch {
      // Private-mode / storage-blocked: fall through to "system".
    }
    const initial: ThemePreference =
      stored === "light" || stored === "dark" || stored === "system"
        ? stored
        : "system";
    setPreferenceState(initial);
    setResolved(resolve(initial));
  }, []);

  // Track the OS setting, but only while it is what we are following.
  useEffect(() => {
    if (preference !== "system") return;
    const query = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => {
      const next = query.matches ? "dark" : "light";
      setResolved(next);
      apply(next);
    };
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, [preference]);

  const setPreference = useCallback((next: ThemePreference) => {
    setPreferenceState(next);
    const nextResolved = resolve(next);
    setResolved(nextResolved);
    apply(nextResolved);
    try {
      localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch {
      // Not persisting is survivable; the session still switches.
    }
  }, []);

  const cycle = useCallback(() => {
    setPreference(
      preference === "light"
        ? "dark"
        : preference === "dark"
          ? "system"
          : "light"
    );
  }, [preference, setPreference]);

  return (
    <ThemeContext.Provider
      value={{ preference, resolved, setPreference, cycle }}
    >
      {children}
    </ThemeContext.Provider>
  );
}
