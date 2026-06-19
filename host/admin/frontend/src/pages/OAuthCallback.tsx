import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { useToast } from "../lib/toast";

interface FinishResult {
  purpose: "login" | "connect";
  redirect: string;
}

export function OAuthCallback() {
  const [params] = useSearchParams();
  const nav = useNavigate();
  const toast = useToast();
  const qc = useQueryClient();
  const [status, setStatus] = useState<"working" | "error">("working");
  const ran = useRef(false);

  useEffect(() => {
    if (ran.current) return;
    ran.current = true;
    const code = params.get("code");
    const state = params.get("state");
    if (!code || !state) {
      setStatus("error");
      return;
    }
    api.post<FinishResult>("/api/oauth/finish", { code, state }).then((res) => {
      if (res.purpose === "login") {
        // New session was issued — refresh auth state and land on the app.
        qc.invalidateQueries({ queryKey: ["me"] });
        nav(res.redirect || "/", { replace: true });
      } else {
        toast.show("GitHub organization connected", "ok");
        nav(res.redirect || "/projects", { replace: true });
      }
    }).catch((e) => {
      // Surface authorization failures on the login screen.
      const msg = String(e?.message ?? e);
      nav(`/login?error=${encodeURIComponent(msg)}`, { replace: true });
    });
  }, [params, nav, toast, qc]);

  return (
    <div style={{ padding: "3rem 1.5rem", textAlign: "center" }}>
      {status === "working" ? <><span className="spinner" /> <p>Signing you in…</p></>
        : <p style={{ color: "var(--danger)" }}>OAuth callback failed. <a href="/login">Back to sign in</a>.</p>}
    </div>
  );
}
