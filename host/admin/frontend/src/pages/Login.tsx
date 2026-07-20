import { FormEvent, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Github } from "lucide-react";
import { api, ApiError } from "../lib/api";
import { Logo } from "../components/Logo";
import type { LoginOptions } from "../lib/types";

function GoogleIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 18 18" aria-hidden focusable="false">
      <path fill="#4285F4" d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.92c1.7-1.57 2.68-3.88 2.68-6.62z" />
      <path fill="#34A853" d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.92-2.26c-.8.54-1.84.86-3.04.86-2.34 0-4.32-1.58-5.02-3.7H.96v2.34A9 9 0 0 0 9 18z" />
      <path fill="#FBBC05" d="M3.98 10.72a5.4 5.4 0 0 1 0-3.44V4.94H.96a9 9 0 0 0 0 8.12l3.02-2.34z" />
      <path fill="#EA4335" d="M9 3.58c1.32 0 2.5.46 3.44 1.35l2.58-2.58A9 9 0 0 0 .96 4.94l3.02 2.34C4.68 5.16 6.66 3.58 9 3.58z" />
    </svg>
  );
}

export function Login() {
  // Password-only (G3): there's exactly one local admin, so the form drops
  // the username field — the backend defaults it internally.
  const [password, setPassword] = useState("");
  const [params] = useSearchParams();
  const [error, setError] = useState<string | null>(params.get("error"));
  const [busy, setBusy] = useState(false);
  const nav = useNavigate();
  const qc = useQueryClient();

  const { data: options } = useQuery<LoginOptions>({
    queryKey: ["login-options"],
    queryFn: () => api.get<LoginOptions>("/api/auth/login-options"),
    staleTime: 30_000,
    retry: false,
  });

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.post("/api/auth/login", { password });
      qc.invalidateQueries({ queryKey: ["me"] });
      const next = params.get("next") || "/";
      nav(next.startsWith("/") ? next : "/", { replace: true });
    } catch (err) {
      setError(err instanceof ApiError && err.status === 401 ? "Invalid password." : String(err));
      setBusy(false);
    }
  }

  function loginWith(provider: "google" | "github") {
    window.location.href = `/api/oauth/login/${provider}/start`;
  }

  // OAuth sign-in only makes sense once an identity exists (post account
  // link) — before that every provider login would be rejected anyway.
  const oauthProviders = options?.has_identities ? (options.oauth_providers ?? []) : [];
  const showOAuth = oauthProviders.includes("github") || oauthProviders.includes("google");
  // Fresh install (no identity yet): the printed one-time password is the way in.
  const freshInstall = options ? !options.has_identities : false;

  return (
    <div className="login-body">
      <div className="login-card">
        <div className="login-brand">
          <Logo size={48} />
          <h1>Homebox</h1>
          <p>Self-hosted Internal PaaS</p>
        </div>
        {error && <div className="login-alert">{error}</div>}

        {showOAuth && (
          <>
            <div className="oauth-buttons">
              {oauthProviders.includes("google") && (
                <button type="button" className="btn oauth-btn" onClick={() => loginWith("google")}>
                  <GoogleIcon /> Continue with Google
                </button>
              )}
              {oauthProviders.includes("github") && (
                <button type="button" className="btn oauth-btn" onClick={() => loginWith("github")}>
                  <Github size={16} /> Continue with GitHub
                </button>
              )}
            </div>
            <div className="login-divider"><span>or</span></div>
          </>
        )}

        <form onSubmit={submit}>
          <div className="field">
            <label className="lbl">Password</label>
            <input
              type="password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              autoComplete="current-password"
              autoFocus
              required
            />
            {freshInstall && (
              <p className="dim" style={{ marginTop: "0.4rem", fontSize: "0.85rem" }}>
                Paste the password printed by the installer.
              </p>
            )}
          </div>
          <button type="submit" className="btn primary" style={{ width: "100%", justifyContent: "center", padding: "0.7rem" }} disabled={busy}>
            {busy ? <span className="spinner" /> : "Sign in"}
          </button>
        </form>
      </div>
    </div>
  );
}
