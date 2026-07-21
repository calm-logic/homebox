/**
 * Display-only formatting helpers for the data viewer.
 *
 * Pure functions — no React, no I/O — so they can be unit-tested directly
 * once a test runner lands in this package (none is configured today).
 */

/** Word segments that render as fixed acronyms instead of Title Case. */
const ACRONYMS: Record<string, string> = {
  id: "ID",
  api: "API",
};

/**
 * Render a snake_case column name as a human-friendly Title Case label.
 *
 * Rules:
 *  - Split on underscores; empty segments (leading/trailing/double `_`) drop.
 *  - A segment that is an acronym (case-insensitive) renders in its canonical
 *    form: `id` → `ID`, `api` → `API` — anywhere it appears as a segment.
 *  - Any other segment gets its first letter upper-cased; the rest of the
 *    segment keeps its original casing (so `parentURL` stays `ParentURL`).
 *
 * Examples: `id` → `ID`, `some_id` → `Some ID`, `api_key` → `API Key`,
 * `created_at` → `Created At`.
 *
 * Display only — sorting, filtering, and queries must keep the raw name.
 */
export function formatColumnName(name: string): string {
  return name
    .split("_")
    .filter(Boolean)
    .map(seg => ACRONYMS[seg.toLowerCase()] ?? seg.charAt(0).toUpperCase() + seg.slice(1))
    .join(" ");
}
