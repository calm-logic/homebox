/**
 * First-run wizard. The fast path plus three steps wired to existing endpoints:
 *   1. Log in with Homebox (opt) → POST /api/cluster/account/link-silent
 *      (falls back to the AccountAuthModal on 412 — no stored token). The
 *      post-link vault restore imports the Cloudflare integration, which
 *      auto-completes step 2 and lets us auto-run step 3 via
 *      POST /api/onboarding/auto-tunnel. Zero pastes on camera.
 *   2. Connect Cloudflare       → POST /api/tunnel/token (token + account pick)
 *   3. Create Homebox tunnel    → POST /api/tunnel/connect
 *   4. Pick admin domain (opt)  → POST /api/onboarding/admin-domain
 *
 * The wizard is gated in App.tsx — when /api/onboarding/state.complete is
 * false, every other route redirects here. The manual path (token paste)
 * stays fully available for accountless installs.
 */

import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Cloud, ExternalLink, CheckCircle2, ArrowRight, AlertTriangle, LogIn } from "lucide-react";
import { api, ApiError } from "../lib/api";
import { useToast } from "../lib/toast";
import { Logo } from "../components/Logo";
import { AccountAuthModal } from "../components/AccountAuthModal";
import type {
  CloudflareAccount,
  CloudflareZone,
  OnboardingState,
  SetTokenResponse,
} from "../lib/types";

// Local extensions of OnboardingState (types.ts is owned elsewhere): the
// backend now reports the linked-account fast path.
interface OnboardingAccount {
  linked: boolean;
  /** True while the post-link vault restore is importing. */
  restoring?: boolean;
}
type WizardState = OnboardingState & {
  account?: OnboardingAccount;
  steps: OnboardingState["steps"] & {
    cloudflare_token: OnboardingState["steps"]["cloudflare_token"] & {
      /** The integration arrived via the account-vault restore, not a paste. */
      synced?: boolean;
    };
  };
};

interface TunnelConflict {
  kind: "name_collision";
  tunnel: {
    id: string;
    name: string;
    created_at: string | null;
    config_src: string | null;
    connector_count: number;
    is_ours: boolean;
  };
  message: string;
}

const CF_TOKEN_TEMPLATE_URL =
  // Same scope template the Tunnel page uses — keep them in sync.
  // Tunnel group key is 'argo_tunnel' (Cloudflare's legacy name for the
  // Cloudflare Tunnel permission group); 'cfd_tunnel' was silently dropped.
  "https://dash.cloudflare.com/profile/api-tokens?permissionGroupKeys=%5B%7B%22key%22%3A%22argo_tunnel%22%2C%22type%22%3A%22edit%22%7D%2C%7B%22key%22%3A%22account_settings%22%2C%22type%22%3A%22read%22%7D%2C%7B%22key%22%3A%22dns%22%2C%22type%22%3A%22edit%22%7D%2C%7B%22key%22%3A%22zone%22%2C%22type%22%3A%22edit%22%7D%5D&name=Homebox+Admin&accountId=*&zoneId=all";

export function Onboarding() {
  const qc = useQueryClient();
  const nav = useNavigate();
  const toast = useToast();
  const { data: state } = useQuery<WizardState>({
    queryKey: ["onboarding"],
    queryFn: () => api.get<WizardState>("/api/onboarding/state"),
    refetchInterval: 4000,
  });

  // Decide which manual step is "active" — the first not-done step.
  const activeStep = useMemo<1 | 2 | 3>(() => {
    if (!state) return 1;
    if (!state.steps.cloudflare_token.done) return 1;
    if (!state.steps.tunnel.done) return 2;
    return 3;
  }, [state]);

  function finish() {
    qc.invalidateQueries({ queryKey: ["onboarding"] });
    nav("/", { replace: true });
  }

  // ── "Log in with Homebox" fast path ──
  const linked = !!state?.account?.linked;
  const cfSynced = !!state?.steps.cloudflare_token.synced;
  const [authModalOpen, setAuthModalOpen] = useState(false);

  function onLinked() {
    setAuthModalOpen(false);
    qc.invalidateQueries({ queryKey: ["onboarding"] });
    toast.show("Account linked, pulling your saved setup…", "ok");
  }

  const linkSilent = useMutation({
    mutationFn: () => api.post<{ linked: boolean }>("/api/cluster/account/link-silent", {}),
    onSuccess: onLinked,
    onError: (e) => {
      // 412 = no stored provider/account token on this box — fall back to the
      // inline auth modal (OAuth buttons + account-token paste).
      if (e instanceof ApiError && e.status === 412) { setAuthModalOpen(true); return; }
      toast.show(String(e), "fail");
    },
  });

  // Once the Cloudflare step auto-completes from the synced integration,
  // advance step 3 with one call. Fire-once; a failure falls back to the
  // manual tunnel form below.
  const autoTunnelFired = useRef(false);
  const autoTunnel = useMutation({
    mutationFn: () => api.post<{ ok: boolean; already?: boolean }>("/api/onboarding/auto-tunnel", {}),
    onSuccess: (resp) => {
      qc.invalidateQueries({ queryKey: ["onboarding"] });
      qc.invalidateQueries({ queryKey: ["tunnel"] });
      if (!resp.already) toast.show("Tunnel created with your synced Cloudflare credentials", "ok");
    },
    onError: (e) => toast.show(`Automatic tunnel setup failed. Create it below. ${String(e)}`, "fail"),
  });
  useEffect(() => {
    if (!state || autoTunnelFired.current) return;
    if (linked && cfSynced && state.steps.cloudflare_token.done && !state.steps.tunnel.done) {
      autoTunnelFired.current = true;
      autoTunnel.mutate();
    }
  }, [state, linked, cfSynced]); // eslint-disable-line react-hooks/exhaustive-deps

  // Fast path only: once everything required is done, collapse the wizard
  // into the app. Manual installs keep the explicit finish/skip on step 4.
  useEffect(() => {
    if (state?.complete && autoTunnelFired.current) finish();
  }, [state?.complete]); // eslint-disable-line react-hooks/exhaustive-deps

  const syncingAccount = linked && !state?.steps.cloudflare_token.done;

  return (
    <div className="onboarding-shell">
      <header className="onboarding-header">
        <div className="brand"><Logo size={48} /><span style={{ fontSize: "1.4rem", fontWeight: 600 }}>Homebox</span></div>
        <p className="dim" style={{ marginTop: "0.5rem", marginBottom: 0 }}>
          Welcome. Connect this host to Cloudflare to reach it from the internet.
        </p>
      </header>

      <ol className="onboarding-steps">
        <Step
          number={1}
          title="Log in with Homebox (optional)"
          done={linked}
          active={!linked && !state?.complete}
          subtitle={linked
            ? (syncingAccount
              ? <><span className="spinner" /> Linked, syncing your saved setup from your account…</>
              : <>Linked. Your saved setup synced from your account.</>)
            : <>Link your homebox.sh account to pull your saved integrations, domains and projects.</>}
        >
          {!linked && !state?.complete && (
            <div className="btn-row">
              <button
                className="btn primary" type="button"
                onClick={() => linkSilent.mutate()}
                disabled={linkSilent.isPending}
              >
                {linkSilent.isPending ? <span className="spinner" /> : <><LogIn size={14} /> Log in with Homebox</>}
              </button>
              <span className="dim">No account? Continue below with a Cloudflare API token instead.</span>
            </div>
          )}
        </Step>

        <Step
          number={2}
          title="Connect Cloudflare"
          done={!!state?.steps.cloudflare_token.done}
          active={activeStep === 1}
          subtitle={state?.steps.cloudflare_token.done && cfSynced
            ? <>Synced from your account{state.steps.cloudflare_token.account_name
              ? <>, connected to <strong>{state.steps.cloudflare_token.account_name}</strong></> : null}</>
            : state?.steps.cloudflare_token.account_name
              ? <>Connected to <strong>{state.steps.cloudflare_token.account_name}</strong></>
              : <>Connect your Cloudflare account so this host can route traffic.</>}
        >
          {activeStep === 1 && (syncingAccount
            ? <p className="dim" style={{ margin: 0 }}><span className="spinner" /> Waiting for your synced Cloudflare integration…</p>
            : <Step1Connect />)}
        </Step>

        <Step
          number={3}
          title="Create the Homebox tunnel"
          done={!!state?.steps.tunnel.done}
          active={activeStep === 2}
          subtitle={state?.steps.tunnel.tunnel_name
            ? <>Running as <strong>{state.steps.tunnel.tunnel_name}</strong></>
            : <>A single tunnel routes every project on this host. Other Homebox installs make their own.</>}
        >
          {activeStep === 2 && (autoTunnel.isPending
            ? <p className="dim" style={{ margin: 0 }}><span className="spinner" /> Creating your tunnel with the synced credentials…</p>
            : <Step2Tunnel />)}
        </Step>

        <Step
          number={4}
          title="Set a public admin URL (optional)"
          done={!!state?.steps.admin_domain.done}
          active={activeStep === 3}
          subtitle={state?.steps.admin_domain.hostname
            ? <>Reachable at <a href={`https://${state.steps.admin_domain.hostname}`} target="_blank" rel="noopener"><strong>{state.steps.admin_domain.hostname}</strong></a></>
            : <>Or skip to keep the admin on <code>http://localhost:7765</code>.</>}
        >
          {activeStep === 3 && <Step3AdminDomain onDone={finish} onSkip={finish} />}
        </Step>
      </ol>

      <AccountAuthModal
        open={authModalOpen}
        onClose={() => setAuthModalOpen(false)}
        onLinked={onLinked}
      />
    </div>
  );
}

// ─── Step shell ──────────────────────────────────────────────────────────────

function Step({
  number, title, subtitle, done, active, children,
}: {
  number: number;
  title: string;
  subtitle: React.ReactNode;
  done: boolean;
  active: boolean;
  children?: React.ReactNode;
}) {
  return (
    <li className={`onboarding-step ${done ? "done" : active ? "active" : "pending"}`}>
      <div className="step-head">
        <div className="step-marker">
          {done ? <CheckCircle2 size={20} /> : <span>{number}</span>}
        </div>
        <div className="grow">
          <div className="step-title">{title}</div>
          <div className="step-subtitle dim">{subtitle}</div>
        </div>
      </div>
      {children && <div className="step-body">{children}</div>}
    </li>
  );
}

// ─── Step 1: Cloudflare token ────────────────────────────────────────────────

function Step1Connect() {
  const qc = useQueryClient();
  const toast = useToast();
  const [token, setToken] = useState("");
  const [accounts, setAccounts] = useState<CloudflareAccount[]>([]);
  const [accountId, setAccountId] = useState("");

  const onConnected = () => {
    qc.invalidateQueries({ queryKey: ["onboarding"] });
    qc.invalidateQueries({ queryKey: ["tunnel"] });
    toast.show("Cloudflare connected", "ok");
  };

  const submit = useMutation({
    mutationFn: (body: { token: string; account_id?: string }) =>
      api.post<SetTokenResponse>("/api/tunnel/token", body),
    onSuccess: (resp) => {
      if (resp.account_id) onConnected();
      else { setAccounts(resp.accounts); toast.show("Pick which Cloudflare account to use", "ok"); }
    },
    onError: (e) => toast.show(String(e), "fail"),
  });

  function connectWith(raw: string) {
    const t = raw.trim();
    if (!t || submit.isPending) return;
    setToken(t);
    submit.mutate({ token: t });
  }
  function onPaste(e: React.ClipboardEvent<HTMLInputElement>) {
    const pasted = e.clipboardData.getData("text");
    if (pasted.trim()) { e.preventDefault(); connectWith(pasted); }
  }

  // ── Account picker (token sees >1 account) ──
  if (accounts.length > 0) {
    return (
      <form onSubmit={e => { e.preventDefault(); submit.mutate({ token, account_id: accountId || undefined }); }}>
        <div className="field">
          <label className="lbl">Account</label>
          <select value={accountId} onChange={e => setAccountId(e.target.value)} required>
            <option value="" disabled>Pick an account…</option>
            {accounts.map(a => (
              <option key={a.id} value={a.id}>{a.name} ({a.id.slice(0, 8)}…)</option>
            ))}
          </select>
          <span className="hint">Tunnels and DNS will be created under the account you pick here.</span>
        </div>
        <div className="btn-row">
          <span className="spacer" />
          <button className="btn primary" type="submit" disabled={submit.isPending || !accountId}>
            {submit.isPending ? <span className="spinner" /> : "Use this account"}
          </button>
        </div>
      </form>
    );
  }

  return (
    <form onSubmit={e => { e.preventDefault(); connectWith(token); }}>
      <div className="btn-row" style={{ marginBottom: "0.75rem" }}>
        <a className="btn" href={CF_TOKEN_TEMPLATE_URL} target="_blank" rel="noopener">
          <ExternalLink size={14} /> Generate token on Cloudflare
        </a>
        <span className="dim">opens the Create Token page</span>
      </div>
      <div className="field">
        <div className="lbl">Required permissions: confirm all four before creating the token</div>
        <ul style={{ margin: "0.25rem 0 0", paddingLeft: "1.1rem", display: "flex", flexDirection: "column", gap: "0.3rem", fontSize: "0.85rem" }}>
          <li><code>Account · Cloudflare Tunnel · Edit</code> <span className="dim">(the pre-fill often drops this one, add it manually)</span></li>
          <li><code>Zone · DNS · Edit</code></li>
          <li><code>Zone · Zone · Edit</code> <span className="dim">(Edit, not Read, so Homebox can create zones for new domains)</span></li>
          <li><code>Account · Account Settings · Read</code></li>
        </ul>
        <span className="hint">The link preselects <strong>All accounts</strong> / <strong>All zones</strong>. Leave those as-is so one token manages every domain in the account.</span>
      </div>
      <div className="field">
        <label className="lbl">Cloudflare API token</label>
        <input
          type="password" value={token} autoFocus
          onChange={e => setToken(e.target.value)} onPaste={onPaste}
          placeholder="Paste your scoped token; it connects and verifies automatically"
          disabled={submit.isPending}
        />
        <span className="hint">
          {submit.isPending
            ? "Verifying scopes with Cloudflare…"
            : "Stored encrypted at rest. We check the scopes the moment you paste."}
        </span>
        {submit.isPending && <span className="spinner" />}
      </div>
    </form>
  );
}

// ─── Step 2: Create tunnel ───────────────────────────────────────────────────

function Step2Tunnel() {
  const qc = useQueryClient();
  const toast = useToast();
  const [name, setName] = useState("homebox");
  const [conflict, setConflict] = useState<TunnelConflict | null>(null);

  function invalidateAndToast(msg: string) {
    qc.invalidateQueries({ queryKey: ["onboarding"] });
    qc.invalidateQueries({ queryKey: ["tunnel"] });
    toast.show(msg, "ok");
  }

  const create = useMutation({
    mutationFn: () => api.post<{ ok: boolean; adopted?: boolean; ours?: boolean; tunnel_name: string }>(
      "/api/tunnel/connect", { name }),
    onSuccess: (resp) => {
      setConflict(null);
      invalidateAndToast(
        resp.adopted
          ? `Reusing existing tunnel ${resp.tunnel_name} (already linked to this Homebox install)`
          : "Tunnel created"
      );
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409 && e.body
          && typeof e.body === "object" && "detail" in e.body
          && (e.body as { detail: TunnelConflict }).detail?.kind === "name_collision") {
        setConflict((e.body as { detail: TunnelConflict }).detail);
        return;
      }
      toast.show(String(e), "fail");
    },
  });

  const adopt = useMutation({
    mutationFn: (tunnel_id: string) => api.post<{ ok: boolean; tunnel_name: string }>(
      "/api/tunnel/adopt", { tunnel_id }),
    onSuccess: (resp) => {
      setConflict(null);
      invalidateAndToast(`Adopted existing tunnel ${resp.tunnel_name}`);
    },
    onError: (e) => toast.show(String(e), "fail"),
  });

  function submit(e: FormEvent) { e.preventDefault(); setConflict(null); create.mutate(); }

  if (conflict) {
    return <ConflictPrompt
      conflict={conflict}
      onAdopt={() => adopt.mutate(conflict.tunnel.id)}
      onUseDifferentName={() => { setConflict(null); setName(""); }}
      adopting={adopt.isPending}
    />;
  }

  return (
    <form onSubmit={submit}>
      <div className="field">
        <label className="lbl">Tunnel name</label>
        <input value={name} onChange={e => setName(e.target.value)} placeholder="homebox" required autoFocus />
        <span className="hint">Name shown in your Cloudflare dashboard. <code>homebox</code> is fine.</span>
      </div>
      <div className="btn-row">
        <span className="spacer" />
        <button className="btn primary" type="submit" disabled={create.isPending || !name.trim()}>
          {create.isPending ? <span className="spinner" /> : <><Cloud size={14} /> Create tunnel</>}
        </button>
      </div>
    </form>
  );
}

function ConflictPrompt({
  conflict, onAdopt, onUseDifferentName, adopting,
}: {
  conflict: TunnelConflict;
  onAdopt: () => void;
  onUseDifferentName: () => void;
  adopting: boolean;
}) {
  const t = conflict.tunnel;
  return (
    <div className="card" style={{ borderColor: "var(--warn)" }}>
      <div className="row" style={{ marginBottom: "0.5rem" }}>
        <AlertTriangle size={16} style={{ color: "var(--warn)" }} />
        <strong>Existing tunnel found</strong>
      </div>
      <p className="dim" style={{ margin: "0 0 0.75rem" }}>{conflict.message}</p>
      <dl style={{ display: "grid", gridTemplateColumns: "max-content 1fr", gap: "0.25rem 1rem", margin: 0, fontSize: "0.85rem" }}>
        <dt className="dim">Name</dt>          <dd style={{ margin: 0 }}><code>{t.name}</code></dd>
        <dt className="dim">Tunnel ID</dt>     <dd style={{ margin: 0 }}><code>{t.id}</code></dd>
        <dt className="dim">Created</dt>       <dd style={{ margin: 0 }}>{t.created_at ? new Date(t.created_at).toLocaleString() : "—"}</dd>
        <dt className="dim">Config source</dt> <dd style={{ margin: 0 }}>{t.config_src || "—"}</dd>
        <dt className="dim">Active connectors</dt><dd style={{ margin: 0 }}>{t.connector_count}</dd>
      </dl>
      {t.connector_count > 0 && (
        <p className="dim" style={{ margin: "0.75rem 0 0", color: "var(--warn)" }}>
          ⚠ This tunnel has {t.connector_count} live connector{t.connector_count === 1 ? "" : "s"}. Adopting it
          will overwrite its ingress with this admin's. If another machine is serving traffic through it,
          that traffic will start coming here instead.
        </p>
      )}
      <div className="btn-row" style={{ marginTop: "1rem" }}>
        <button className="btn" type="button" onClick={onUseDifferentName} disabled={adopting}>
          Use a different name
        </button>
        <span className="spacer" />
        <button className="btn primary" type="button" onClick={onAdopt} disabled={adopting}>
          {adopting ? <span className="spinner" /> : <>Adopt this tunnel <ArrowRight size={14} /></>}
        </button>
      </div>
    </div>
  );
}

// ─── Step 3: Admin public URL ────────────────────────────────────────────────

function Step3AdminDomain({ onDone, onSkip }: { onDone: () => void; onSkip: () => void }) {
  const toast = useToast();
  const [zoneId, setZoneId] = useState("");
  const [subdomain, setSubdomain] = useState("admin");

  const { data: zones, isFetching, error } = useQuery<CloudflareZone[]>({
    queryKey: ["cf-zones"],
    queryFn: () => api.get<CloudflareZone[]>("/api/tunnel/zones"),
  });

  // Auto-select the first active zone so the user only has to confirm.
  useEffect(() => {
    if (!zoneId && zones && zones.length > 0) {
      const active = zones.find(z => z.status === "active") || zones[0];
      setZoneId(active.id);
    }
  }, [zones, zoneId]);

  const apply = useMutation({
    mutationFn: () => api.post<{ ok: boolean; hostname: string; url: string }>(
      "/api/onboarding/admin-domain", { zone_id: zoneId, subdomain }),
    onSuccess: (resp) => {
      toast.show(`Admin reachable at ${resp.hostname}`, "ok");
      onDone();
    },
    onError: (e) => toast.show(String(e), "fail"),
  });

  function submit(e: FormEvent) { e.preventDefault(); apply.mutate(); }

  const zone = zones?.find(z => z.id === zoneId);
  const previewHost = zone ? (subdomain ? `${subdomain}.${zone.name}` : zone.name) : "";

  return (
    <form onSubmit={submit}>
      {error && <div className="badge fail">Failed to load zones: {String(error)}</div>}
      {!zones && isFetching && <span className="spinner" />}
      {zones && zones.length === 0 && (
        <p className="dim">No zones in your Cloudflare account. Add a domain in Cloudflare first, or skip and reach the admin at <code>http://localhost:7765</code>.</p>
      )}

      {zones && zones.length > 0 && (
        <div className="row" style={{ alignItems: "flex-end", gap: "0.75rem", flexWrap: "wrap" }}>
          <div className="field" style={{ flex: "0 0 auto", marginBottom: 0 }}>
            <label className="lbl">Subdomain</label>
            <input value={subdomain} onChange={e => setSubdomain(e.target.value.trim())}
              placeholder="admin" style={{ width: "8em" }} />
          </div>
          <div className="dim" style={{ paddingBottom: "0.6rem" }}>.</div>
          <div className="field" style={{ flex: 1, minWidth: 200, marginBottom: 0 }}>
            <label className="lbl">Zone</label>
            <select value={zoneId} onChange={e => setZoneId(e.target.value)} required>
              {zones.map(z => (
                <option key={z.id} value={z.id}>{z.name}{z.status !== "active" ? ` (${z.status})` : ""}</option>
              ))}
            </select>
          </div>
        </div>
      )}

      {previewHost && (
        <p className="dim" style={{ marginTop: "0.75rem" }}>
          The admin will be reachable at <code>https://{previewHost}</code>. We'll create a CNAME, push tunnel ingress, and add a Traefik route.
        </p>
      )}

      <div className="btn-row" style={{ marginTop: "1rem" }}>
        <button className="btn ghost" type="button" onClick={onSkip}>
          Skip for now
        </button>
        <span className="spacer" />
        <button className="btn primary" type="submit"
          disabled={apply.isPending || !zoneId || !zones || zones.length === 0}>
          {apply.isPending ? <span className="spinner" /> : <>Set admin URL <ArrowRight size={14} /></>}
        </button>
      </div>
    </form>
  );
}
