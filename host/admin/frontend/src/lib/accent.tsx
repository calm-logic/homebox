/**
 * Accent color provider. Fetches the persisted accent from /api/theme on
 * mount (public endpoint, so it works on the login screen pre-auth) and
 * applies it as CSS variables on :root. `set` writes back via authed POST.
 *
 * The CSS we override:
 *   --accent          the user-chosen color
 *   --accent-2        ~15% darker (hover/pressed state)
 *   --accent-glow     same color, low alpha (focus rings, soft fills)
 *
 * If the user hasn't set anything (first run) or has explicitly cleared
 * their choice, we leave the inline overrides off and the stylesheet
 * defaults take over.
 */

import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./api";
import { useTheme } from "./theme";

interface AccentApi {
  /** null = no override; defaults from CSS take over. */
  accent: string | null;
  set: (hex: string | null) => Promise<void>;
}

const AccentCtx = createContext<AccentApi | null>(null);

interface ThemeResponse { accent_color: string | null }

const ACCENT_VARS = ["--accent", "--accent-2", "--accent-glow"] as const;

function applyAccent(hex: string | null, theme: "dark" | "light") {
  const root = document.documentElement;
  if (!hex) {
    for (const v of ACCENT_VARS) root.style.removeProperty(v);
    return;
  }
  const [h, s, v] = hexToHsv(hex);
  // accent-2: a touch darker — drop value, slightly bump saturation. Mirrors
  // the relationship between #4dd6a4 and #3aaf85 in the default palette.
  const accent2 = hsvToHex(h, Math.min(1, s * 1.05), Math.max(0, v - 0.15));
  // accent-glow: same hue, low alpha. Dark mode wants a stronger glow than
  // light mode (the original palette uses 0.28 vs 0.18).
  const alpha = theme === "dark" ? 0.28 : 0.18;
  const [r, g, b] = hsvToRgb(h, s, v).map(n => Math.round(n));
  root.style.setProperty("--accent", hex);
  root.style.setProperty("--accent-2", accent2);
  root.style.setProperty("--accent-glow", `rgba(${r}, ${g}, ${b}, ${alpha})`);
}

export function AccentProvider({ children }: { children: React.ReactNode }) {
  const qc = useQueryClient();
  const { theme } = useTheme();
  // Local mirror so applyAccent can run synchronously in the click handler
  // without waiting for the round-trip — feels instant.
  const [accent, setAccent] = useState<string | null>(null);

  const { data } = useQuery<ThemeResponse>({
    queryKey: ["theme"],
    queryFn: () => api.get<ThemeResponse>("/api/theme"),
    staleTime: 30_000,
  });

  useEffect(() => {
    if (data) setAccent(data.accent_color);
  }, [data]);

  // Re-apply when accent OR theme changes (theme switch needs to re-derive
  // accent-glow alpha against the new background).
  useEffect(() => { applyAccent(accent, theme); }, [accent, theme]);

  const set = useCallback(async (hex: string | null) => {
    setAccent(hex);
    applyAccent(hex, theme);
    await api.post<ThemeResponse>("/api/theme", { accent_color: hex });
    qc.invalidateQueries({ queryKey: ["theme"] });
  }, [theme, qc]);

  return <AccentCtx.Provider value={{ accent, set }}>{children}</AccentCtx.Provider>;
}

export function useAccent(): AccentApi {
  const ctx = useContext(AccentCtx);
  if (!ctx) throw new Error("useAccent outside AccentProvider");
  return ctx;
}

// ─── Color math ──────────────────────────────────────────────────────────────
// HSV is what the picker UI is built on; hex is the API + DB format.

export function hexToHsv(hex: string): [number, number, number] {
  const m = hex.match(/^#?([0-9a-f]{6})$/i);
  if (!m) return [0, 0, 0];
  const n = parseInt(m[1], 16);
  const r = ((n >> 16) & 0xff) / 255;
  const g = ((n >> 8) & 0xff) / 255;
  const b = (n & 0xff) / 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  const d = max - min;
  let h = 0;
  if (d !== 0) {
    if (max === r)      h = ((g - b) / d) % 6;
    else if (max === g) h = (b - r) / d + 2;
    else                h = (r - g) / d + 4;
    h *= 60;
    if (h < 0) h += 360;
  }
  const s = max === 0 ? 0 : d / max;
  return [h, s, max];
}

export function hsvToRgb(h: number, s: number, v: number): [number, number, number] {
  const c = v * s;
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
  const m = v - c;
  let r = 0, g = 0, b = 0;
  if (h < 60)       [r, g, b] = [c, x, 0];
  else if (h < 120) [r, g, b] = [x, c, 0];
  else if (h < 180) [r, g, b] = [0, c, x];
  else if (h < 240) [r, g, b] = [0, x, c];
  else if (h < 300) [r, g, b] = [x, 0, c];
  else              [r, g, b] = [c, 0, x];
  return [(r + m) * 255, (g + m) * 255, (b + m) * 255];
}

export function hsvToHex(h: number, s: number, v: number): string {
  const [r, g, b] = hsvToRgb(h, s, v);
  const to = (n: number) => Math.round(Math.max(0, Math.min(255, n))).toString(16).padStart(2, "0");
  return `#${to(r)}${to(g)}${to(b)}`;
}
