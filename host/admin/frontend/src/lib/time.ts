// Tiny relative-time formatter — "just now", "5m ago", "3h ago", "2d ago".
// Accepts ISO strings or epoch timestamps (seconds or milliseconds); returns
// null for missing/unparseable input so callers can fall back to "pending".
export function timeAgo(input: string | number | null | undefined): string | null {
  if (input == null || input === "") return null;
  let ms: number;
  if (typeof input === "number") {
    ms = input < 1e12 ? input * 1000 : input; // epoch seconds vs milliseconds
  } else {
    // The admin backend emits naive-UTC ISO strings (no zone suffix), which
    // JS would parse as LOCAL time — treat zone-less timestamps as UTC.
    const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(input);
    const d = new Date(hasZone ? input : input + "Z");
    if (isNaN(d.getTime())) return null;
    ms = d.getTime();
  }
  const s = Math.floor((Date.now() - ms) / 1000);
  if (s < 45) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 60) return `${d}d ago`;
  return `${Math.floor(d / 30)}mo ago`;
}
