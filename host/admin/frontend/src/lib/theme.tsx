import { createContext, useContext, useEffect, useState } from "react";

export type Theme = "dark" | "light";

interface ThemeApi { theme: Theme; toggle: () => void; set: (t: Theme) => void }

const ThemeCtx = createContext<ThemeApi | null>(null);
const STORAGE_KEY = "homebox-theme";

function readInitial(): Theme {
  if (typeof window === "undefined") return "dark";
  const stored = window.localStorage.getItem(STORAGE_KEY);
  if (stored === "dark" || stored === "light") return stored;
  return window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [theme, setTheme] = useState<Theme>(readInitial);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try { window.localStorage.setItem(STORAGE_KEY, theme); } catch { /* ignore */ }
  }, [theme]);

  const api: ThemeApi = {
    theme,
    toggle: () => setTheme(t => (t === "dark" ? "light" : "dark")),
    set: setTheme,
  };
  return <ThemeCtx.Provider value={api}>{children}</ThemeCtx.Provider>;
}

export function useTheme(): ThemeApi {
  const ctx = useContext(ThemeCtx);
  if (!ctx) throw new Error("useTheme outside ThemeProvider");
  return ctx;
}
