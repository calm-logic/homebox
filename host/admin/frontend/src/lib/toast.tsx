import { createContext, useCallback, useContext, useState } from "react";

interface ToastMessage { id: number; text: string; kind: "ok" | "fail" | "info" }
interface ToastApi { show: (text: string, kind?: ToastMessage["kind"]) => void }

const ToastCtx = createContext<ToastApi | null>(null);

// Errors stay until the user dismisses them — they often contain detail you
// need to read carefully (HTTP status, Cloudflare error codes, stack hints).
// Successes auto-dismiss; the diff is in the diff, no need to dwell.
const AUTO_DISMISS_MS: Record<ToastMessage["kind"], number | null> = {
  ok: 2400,
  info: 4000,
  fail: null,
};

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = useState<ToastMessage[]>([]);
  const dismiss = useCallback((id: number) => {
    setItems(prev => prev.filter(i => i.id !== id));
  }, []);
  const show = useCallback((text: string, kind: ToastMessage["kind"] = "info") => {
    const id = Date.now() + Math.random();
    setItems(prev => [...prev, { id, text, kind }]);
    const ttl = AUTO_DISMISS_MS[kind];
    if (ttl !== null) {
      setTimeout(() => setItems(prev => prev.filter(i => i.id !== id)), ttl);
    }
  }, []);
  return (
    <ToastCtx.Provider value={{ show }}>
      {children}
      <div style={{ position: "fixed", right: "1rem", bottom: "1rem", display: "flex", flexDirection: "column", gap: "0.5rem", zIndex: 1000, maxWidth: "min(480px, calc(100vw - 2rem))" }}>
        {items.map(t => (
          <div
            key={t.id}
            className={`toast ${t.kind}`}
            role={t.kind === "fail" ? "alert" : "status"}
            onClick={() => dismiss(t.id)}
            title="Click to dismiss"
            style={{ cursor: "pointer" }}
          >
            {t.text}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

export function useToast(): ToastApi {
  const ctx = useContext(ToastCtx);
  if (!ctx) throw new Error("useToast outside ToastProvider");
  return ctx;
}
