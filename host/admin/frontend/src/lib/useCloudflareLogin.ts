import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";

/**
 * Drives the browser "Connect with Cloudflare" flow against the admin's
 * /api/tunnel/cloudflare/login/* endpoints (which run `cloudflared tunnel
 * login` and extract the token from the delivered cert.pem).
 *
 * phase: idle → starting → waiting (authorize URL shown) → connected | error.
 * On "connected" the caller's onConnected() fires (invalidate queries / close).
 */
export type CfLoginPhase = "idle" | "starting" | "waiting" | "connected" | "error";

interface StartResp { session_id: string; url: string }
interface PollResp { status: string; error?: string }

export function useCloudflareLogin(onConnected: () => void) {
  const [phase, setPhase] = useState<CfLoginPhase>("idle");
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  const sessionRef = useRef<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const onConnectedRef = useRef(onConnected);
  onConnectedRef.current = onConnected;

  const stopPolling = () => {
    if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null; }
  };

  const cancelSession = useCallback(() => {
    const sid = sessionRef.current;
    sessionRef.current = null;
    if (sid) api.post("/api/tunnel/cloudflare/login/cancel", { session_id: sid }).catch(() => {});
  }, []);

  const reset = useCallback(() => {
    stopPolling();
    cancelSession();
    setPhase("idle"); setUrl(""); setError("");
  }, [cancelSession]);

  const start = useCallback(async () => {
    setError(""); setPhase("starting");
    try {
      const r = await api.post<StartResp>("/api/tunnel/cloudflare/login/start");
      sessionRef.current = r.session_id;
      setUrl(r.url);
      setPhase("waiting");
      // Best-effort auto-open; the visible link is the reliable fallback.
      window.open(r.url, "_blank", "noopener");
      timerRef.current = setInterval(async () => {
        const sid = sessionRef.current;
        if (!sid) return;
        try {
          const p = await api.post<PollResp>("/api/tunnel/cloudflare/login/poll", { session_id: sid });
          if (p.status === "connected") {
            stopPolling(); sessionRef.current = null;
            setPhase("connected"); onConnectedRef.current();
          } else if (p.status === "failed" || p.status === "expired" || p.status === "unknown") {
            stopPolling(); sessionRef.current = null;
            setError(p.error || `Login ${p.status}.`); setPhase("error");
          }
          // "pending" → keep polling
        } catch (e) {
          stopPolling(); sessionRef.current = null;
          setError(String(e)); setPhase("error");
        }
      }, 2000);
    } catch (e) {
      setError(String(e)); setPhase("error");
    }
  }, []);

  // Free the cloudflared subprocess if the component unmounts mid-flow.
  useEffect(() => () => { stopPolling(); cancelSession(); }, [cancelSession]);

  return { phase, url, error, start, reset };
}
