/**
 * Custom HSV color picker. Three controls:
 *   - Saturation/Value 2D pad (the colorful square)
 *   - Hue strip (rainbow vertical bar)
 *   - Hex input with a reset-to-default button + preset swatches
 *
 * Drag handling uses pointer events with capture so the thumb tracks
 * even when the cursor leaves the element. No third-party deps.
 */

import { useEffect, useRef, useState, useCallback } from "react";
import { Check, RotateCcw } from "lucide-react";
import { hexToHsv, hsvToHex } from "../lib/accent";

const PRESETS = [
  "#4dd6a4", // homebox green (default-dark)
  "#1f9d6f", // homebox green (default-light)
  "#6fb1f3", // info blue
  "#7c5cff", // violet
  "#f08aff", // pink
  "#f3c969", // amber
  "#ef6f6c", // coral
  "#ff8a3d", // orange
];

interface Props {
  /** Current hex (or null = no override). */
  value: string | null;
  /** Called as the user picks (live preview). null = reset to default. */
  onChange: (hex: string | null) => void;
  /** Closes the popover. */
  onClose: () => void;
}

export function ColorPicker({ value, onChange, onClose }: Props) {
  const [h, setH] = useState(0);
  const [s, setS] = useState(1);
  const [v, setV] = useState(1);
  const [hexInput, setHexInput] = useState(value ?? "#4dd6a4");
  const ref = useRef<HTMLDivElement>(null);

  // Initialize from `value` once.
  useEffect(() => {
    const start = value ?? "#4dd6a4";
    const [hh, ss, vv] = hexToHsv(start);
    setH(hh); setS(ss); setV(vv);
    setHexInput(start);
  }, []); // intentionally only on mount; user dragging shouldn't snap back

  // Push every change up.
  const currentHex = hsvToHex(h, s, v);
  useEffect(() => {
    onChange(currentHex);
    setHexInput(currentHex);
  }, [h, s, v]);

  // Click-outside to close.
  useEffect(() => {
    function onDocDown(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    }
    function onKey(e: KeyboardEvent) { if (e.key === "Escape") onClose(); }
    document.addEventListener("mousedown", onDocDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  function commitHex(raw: string) {
    const trimmed = raw.trim();
    const m = trimmed.match(/^#?([0-9a-fA-F]{6})$/);
    if (!m) return;
    const hex = "#" + m[1].toLowerCase();
    const [hh, ss, vv] = hexToHsv(hex);
    setH(hh); setS(ss); setV(vv);
  }

  return (
    <div ref={ref} className="color-picker-popover" role="dialog" aria-label="Pick accent color">
      <div className="color-picker-row">
        <SVPad h={h} s={s} v={v} onChange={(ns, nv) => { setS(ns); setV(nv); }} />
        <HueStrip h={h} onChange={setH} />
      </div>

      <div className="color-picker-presets">
        {PRESETS.map(p => (
          <button
            key={p}
            type="button"
            className="color-preset"
            style={{ background: p }}
            aria-label={`Preset ${p}`}
            title={p}
            onClick={() => { commitHex(p); }}
          />
        ))}
      </div>

      <div className="color-picker-bottom">
        <div className="color-swatch-current" style={{ background: currentHex }} aria-hidden />
        <input
          className="color-hex-input"
          type="text"
          value={hexInput}
          onChange={e => setHexInput(e.target.value)}
          onBlur={() => commitHex(hexInput)}
          onKeyDown={e => {
            if (e.key === "Enter") { e.preventDefault(); commitHex(hexInput); }
          }}
          spellCheck={false}
          aria-label="Hex value"
        />
        <button
          type="button"
          className="btn small ghost"
          title="Reset to default"
          onClick={() => onChange(null)}
        >
          <RotateCcw size={12} /> Reset
        </button>
        <button
          type="button"
          className="btn small primary"
          onClick={onClose}
        >
          <Check size={12} /> Done
        </button>
      </div>
    </div>
  );
}

// ─── Saturation / Value 2D pad ──────────────────────────────────────────────

function SVPad({
  h, s, v, onChange,
}: { h: number; s: number; v: number; onChange: (s: number, v: number) => void }) {
  const padRef = useRef<HTMLDivElement>(null);

  const update = useCallback((clientX: number, clientY: number) => {
    const el = padRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const ns = clamp01((clientX - r.left) / r.width);
    const nv = clamp01(1 - (clientY - r.top) / r.height);
    onChange(ns, nv);
  }, [onChange]);

  function onPointerDown(e: React.PointerEvent) {
    (e.target as Element).setPointerCapture(e.pointerId);
    update(e.clientX, e.clientY);
  }
  function onPointerMove(e: React.PointerEvent) {
    if (e.buttons !== 1) return;
    update(e.clientX, e.clientY);
  }

  return (
    <div
      ref={padRef}
      className="sv-pad"
      role="slider"
      aria-label="Saturation and brightness"
      aria-valuetext={`saturation ${Math.round(s * 100)}%, brightness ${Math.round(v * 100)}%`}
      style={{
        background: `
          linear-gradient(to top, #000, transparent),
          linear-gradient(to right, #fff, hsl(${h}, 100%, 50%))
        `,
      }}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
    >
      <div
        className="sv-thumb"
        style={{
          left: `${s * 100}%`,
          top: `${(1 - v) * 100}%`,
          background: hsvToHex(h, s, v),
        }}
      />
    </div>
  );
}

// ─── Hue strip ──────────────────────────────────────────────────────────────

function HueStrip({ h, onChange }: { h: number; onChange: (h: number) => void }) {
  const stripRef = useRef<HTMLDivElement>(null);

  const update = useCallback((clientY: number) => {
    const el = stripRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    onChange(clamp01((clientY - r.top) / r.height) * 360);
  }, [onChange]);

  function onPointerDown(e: React.PointerEvent) {
    (e.target as Element).setPointerCapture(e.pointerId);
    update(e.clientY);
  }
  function onPointerMove(e: React.PointerEvent) {
    if (e.buttons !== 1) return;
    update(e.clientY);
  }

  return (
    <div
      ref={stripRef}
      className="hue-strip"
      role="slider"
      aria-label="Hue"
      aria-valuemin={0} aria-valuemax={360}
      aria-valuenow={Math.round(h)}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
    >
      <div className="hue-thumb" style={{ top: `${(h / 360) * 100}%` }} />
    </div>
  );
}

function clamp01(n: number) { return Math.max(0, Math.min(1, n)); }
