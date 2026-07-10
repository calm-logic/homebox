import { RefObject, useLayoutEffect } from "react";

/**
 * Measures the active tab inside a `.tabs`/`.vtabs` container and writes its
 * position as CSS vars (--ind-x/--ind-w for horizontal, --ind-y/--ind-h for
 * vertical) so a `.tab-indicator` element can animate between tabs with a
 * plain CSS transition — no per-orientation JS branching, the stylesheet
 * picks which pair of vars to use (see index.css, including the mobile
 * breakpoint where .vtabs itself switches to a horizontal row).
 */
export function useTabIndicator(
  containerRef: RefObject<HTMLElement | null>,
  activeSelector: string,
  deps: unknown[],
) {
  useLayoutEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const update = () => {
      const active = container.querySelector<HTMLElement>(activeSelector);
      if (!active) {
        container.style.setProperty("--ind-w", "0px");
        container.style.setProperty("--ind-h", "0px");
        return;
      }
      container.style.setProperty("--ind-x", `${active.offsetLeft}px`);
      container.style.setProperty("--ind-w", `${active.offsetWidth}px`);
      container.style.setProperty("--ind-y", `${active.offsetTop}px`);
      container.style.setProperty("--ind-h", `${active.offsetHeight}px`);
    };

    update();
    const ro = new ResizeObserver(update);
    ro.observe(container);
    return () => ro.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}
