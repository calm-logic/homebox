import { FormEvent, useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Github } from "lucide-react";
import { api } from "../lib/api";
import { Modal } from "./Modal";
import type { AccountStatus } from "../lib/types";

/** Small inline Google "G" mark (duplicated from the login screen). */
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

interface Props {
  open: boolean;
  onClose: () => void;
  /** Called once the account is linked (via either path). The parent should
   *  close the modal and refresh whatever it renders from the account. */
  onLinked: () => void;
}

/**
 * Inline account-auth fallback (G4b): when the silent link
 * (POST /api/cluster/account/link-silent) 412s, this modal offers
 * (1) provider sign-in via the existing oauth-url + popup + postMessage
 * machinery, and (2) an account-token paste posting to the existing
 * POST /api/cluster/account/link. Self-contained: it owns its queries,
 * mutations and popup lifecycle, so Onboarding/System can drop it in as-is.
 */
export function AccountAuthModal({ open, onClose, onLinked }: Props) {
  const qc = useQueryClient();
  const [popup, setPopup] = useState<Window | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showToken, setShowToken] = useState(false);
  const [accountToken, setAccountToken] = useState("");
  const [nodeName, setNodeName] = useState("");
  const [peerUrl, setPeerUrl] = useState("");
  const [linkedFired, setLinkedFired] = useState(false);

  const { data: providers } = useQuery<{ github: boolean; google: boolean }>({
    queryKey: ["cluster-account-providers"],
    queryFn: () => api.get("/api/cluster/account/providers"),
    enabled: open,
    staleTime: 60000,
    retry: true,
  });
  // Poll the account status while a popup is open — the fallback path when
  // the popup's postMessage is missed (e.g. it was closed mid-redirect).
  const { data: account } = useQuery<AccountStatus>({
    queryKey: ["cluster-account"],
    queryFn: () => api.get<AccountStatus>("/api/cluster/account"),
    enabled: open,
    refetchInterval: popup ? 2000 : false,
  });

  function finishLinked() {
    if (linkedFired) return;
    setLinkedFired(true);
    qc.invalidateQueries({ queryKey: ["cluster"] });
    qc.invalidateQueries({ queryKey: ["cluster-account"] });
    qc.invalidateQueries({ queryKey: ["account-topology"] });
    onLinked();
  }

  const oauthStart = useMutation({
    mutationFn: (provider: "github" | "google") =>
      api.get<{ url: string }>(`/api/cluster/account/oauth-url?provider=${provider}`),
    onSuccess: (d) => {
      const w = window.open(d.url, "homebox-account", "width=560,height=720");
      if (!w) {
        setError("Popup blocked. Allow popups for this site and try again");
        return;
      }
      w.focus();
      setPopup(w);
      setError(null);
    },
    onError: (e) => setError(String(e)),
  });

  const linkToken = useMutation({
    mutationFn: () =>
      api.post("/api/cluster/account/link", {
        account_token: accountToken.trim(),
        node_name: nodeName.trim(),
        peer_url: peerUrl.trim(),
      }),
    onSuccess: () => {
      setAccountToken("");
      finishLinked();
    },
    onError: (e) => setError(String(e)),
  });

  // Reset one-shot state each time the modal opens.
  useEffect(() => {
    if (open) {
      setLinkedFired(false);
      setError(null);
    }
  }, [open]);

  // Notice when the user closes the popup themselves, so buttons re-enable.
  useEffect(() => {
    if (!popup) return;
    const t = setInterval(() => {
      if (popup.closed) setPopup(null);
    }, 500);
    return () => clearInterval(t);
  }, [popup]);

  // The popup lands on /system?account=linked|account_error and posts a
  // message to its opener right before closing itself.
  useEffect(() => {
    if (!open) return;
    function onMessage(e: MessageEvent) {
      if (e.origin !== window.location.origin) return;
      if (!e.data || e.data.type !== "homebox-account") return;
      setPopup(null);
      if (e.data.error) {
        setError(String(e.data.error));
        return;
      }
      if (e.data.linked) finishLinked();
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, linkedFired]);

  // Poll path: the modal only ever opens on an unlinked node, so linked=true
  // while it's open means the flow finished (whatever the channel).
  useEffect(() => {
    if (open && account?.linked) {
      try { popup?.close(); } catch { /* already closed */ }
      setPopup(null);
      finishLinked();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, account?.linked]);

  return (
    <Modal open={open} title="Link your homebox.sh account" onClose={onClose}>
      <p className="dim">
        Sign in with the provider your homebox.sh account uses. The link happens right here,
        and your encrypted cloud vault syncs this box automatically afterwards.
      </p>

      {error && (
        <div className="banner danger" style={{ marginTop: "0.75rem" }}>
          <span>{error}</span>
          <button className="btn ghost small" onClick={() => setError(null)}>Dismiss</button>
        </div>
      )}

      {popup ? (
        <div className="row" style={{ marginTop: "1rem" }}>
          <span className="spinner" /> Waiting for you to finish in the popup…
        </div>
      ) : (
        <div className="oauth-buttons" style={{ marginTop: "1rem" }}>
          <button
            type="button"
            className="btn oauth-btn"
            onClick={() => oauthStart.mutate("github")}
            disabled={oauthStart.isPending}
          >
            {oauthStart.isPending && oauthStart.variables === "github"
              ? <span className="spinner" />
              : <><Github size={16} /> Continue with GitHub</>}
          </button>
          {providers?.google && (
            <button
              type="button"
              className="btn oauth-btn"
              onClick={() => oauthStart.mutate("google")}
              disabled={oauthStart.isPending}
            >
              {oauthStart.isPending && oauthStart.variables === "google"
                ? <span className="spinner" />
                : <><GoogleIcon /> Continue with Google</>}
            </button>
          )}
        </div>
      )}

      <div style={{ marginTop: "1.25rem" }}>
        <button type="button" className="btn ghost small" onClick={() => setShowToken(s => !s)}>
          {showToken ? "Hide" : "Have an account token? Paste it"}
        </button>
        {showToken && (
          <form
            onSubmit={(e: FormEvent) => { e.preventDefault(); linkToken.mutate(); }}
            style={{ display: "grid", gap: "0.7rem", marginTop: "0.7rem" }}
          >
            <label>Account token
              <input
                value={accountToken}
                onChange={e => setAccountToken(e.target.value)}
                placeholder="hba.…"
              />
            </label>
            <label>Node name
              <input
                value={nodeName}
                onChange={e => setNodeName(e.target.value)}
                placeholder="living-room-mini"
              />
            </label>
            <label>This node's LAN address (peers connect here, port 80)
              <input
                value={peerUrl}
                onChange={e => setPeerUrl(e.target.value)}
                placeholder="http://192.168.1.10"
              />
            </label>
            <div>
              <button
                className="btn primary"
                type="submit"
                disabled={linkToken.isPending || !accountToken.trim() || !peerUrl.trim()}
              >
                {linkToken.isPending ? <span className="spinner" /> : <>Link account</>}
              </button>
            </div>
          </form>
        )}
      </div>
    </Modal>
  );
}
