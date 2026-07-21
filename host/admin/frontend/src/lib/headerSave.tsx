import { useEffect, useRef } from "react";
import { RefreshCw } from "lucide-react";

/**
 * Conditional page-header Save.
 *
 * A view (a ServiceDetail tab, the project Settings panel) reports whether it
 * holds real unsaved changes; the page renders one Save slot at the right end
 * of its title/back-arrow header row. The button only exists while the active
 * view is dirty — reverting an edit back to the saved value makes it
 * disappear again.
 */
export interface HeaderSave {
  dirty: boolean;
  saving: boolean;
  save: () => void;
}

/**
 * Report a view's unsaved-changes status up to the page header.
 *
 * `onStatus` must be referentially stable (pass a useState setter). The save
 * callback is kept in a ref so the parent only re-renders when dirty/saving
 * actually flip; unmounting (tab switch, section change) clears the slot.
 */
export function useHeaderSave(
  onStatus: (s: HeaderSave | null) => void,
  dirty: boolean,
  saving: boolean,
  save: () => void,
) {
  const saveRef = useRef(save);
  saveRef.current = save;
  useEffect(() => {
    onStatus({ dirty, saving, save: () => saveRef.current() });
    return () => onStatus(null);
  }, [onStatus, dirty, saving]);
}

/** The header-row Save button itself — not rendered at all unless dirty. */
export function HeaderSaveButton({ state }: { state: HeaderSave | null }) {
  if (!state?.dirty) return null;
  return (
    <button className="btn primary" disabled={state.saving} onClick={state.save}>
      {state.saving ? <span className="spinner" /> : <><RefreshCw size={14} /> Save</>}
    </button>
  );
}
