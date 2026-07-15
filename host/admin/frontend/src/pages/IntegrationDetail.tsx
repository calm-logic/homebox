import { FormEvent, useState } from "react";
import { Link, Navigate, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ExternalLink, Plug, RefreshCw, Trash2, Unplug } from "lucide-react";
import { api } from "../lib/api";
import { Modal } from "../components/Modal";
import { useToast } from "../lib/toast";
import { providerLogo, statusDot } from "./Integrations";
import type { CloudflareAccount, IntegrationItem, SetTokenResponse, TunnelStatus } from "../lib/types";

const CF_TOKEN_TEMPLATE_URL =
  // Pre-fills Cloudflare's "Create Custom Token" with the scopes Homebox needs.
  // Zone:Edit (not just Read) so Homebox can CREATE zones for brand-new domains.
  "https://dash.cloudflare.com/profile/api-tokens?permissionGroupKeys=%5B%7B%22key%22%3A%22argo_tunnel%22%2C%22type%22%3A%22edit%22%7D%2C%7B%22key%22%3A%22account_settings%22%2C%22type%22%3A%22read%22%7D%2C%7B%22key%22%3A%22dns%22%2C%22type%22%3A%22edit%22%7D%2C%7B%22key%22%3A%22zone%22%2C%22type%22%3A%22edit%22%7D%5D&name=Homebox+Admin&accountId=*&zoneId=all";

/** Full details + actions for one integration (linked from the card list). */
export function IntegrationDetail() {
  const { integrationId } = useParams();
  const id = Number(integrationId);
  const qc = useQueryClient();
  const nav = useNavigate();
  const toast = useToast();
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);

  const { data: integrations } = useQuery<IntegrationItem[]>({
    queryKey: ["integrations"],
    queryFn: () => api.get<IntegrationItem[]>("/api/integrations"),
  });

  const sync = useMutation({
    mutationFn: () => api.post(`/api/integrations/${id}/sync`),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["projects"] }); toast.show("Synced repositories", "ok"); },
    onError: (e) => toast.show(String(e), "fail"),
  });
  const disconnect = useMutation({
    mutationFn: () => api.del(`/api/integrations/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["integrations"] });
      qc.invalidateQueries({ queryKey: ["projects"] });
      toast.show("Disconnected", "ok");
      nav("/integrations", { replace: true });
    },
    onError: (e) => toast.show(String(e), "fail"),
  });

  if (!integrations) return <span className="spinner" />;
  const i = integrations.find(x => x.id === id);
  if (!i) return <Navigate to="/integrations" replace />;

  const isGithub = i.provider === "github";
  const title = isGithub ? (i.account_login ?? "GitHub") : (i.name || "Cloudflare");
  const fmt = (iso: string | null) => iso ? new Date(iso).toLocaleString() : "—";

  return (
    <>
      <div className="row">
        <Link to="/integrations" className="back-btn" aria-label="Back to integrations" title="Back to integrations">
          <ArrowLeft size={18} />
        </Link>
        <span className="provider-logo">{providerLogo(i.provider)}</span>
        <h1 style={{ margin: 0 }}>{title}</h1>
        {statusDot(i.status)}
        <div className="spacer" />
        {isGithub ? (
          <>
            <button className="btn" disabled={sync.isPending} onClick={() => sync.mutate()}>
              {sync.isPending ? <span className="spinner" /> : <RefreshCw size={14} />} Sync
            </button>
            <a className="btn ghost" href={`https://github.com/${i.account_login}`} target="_blank" rel="noopener">
              <ExternalLink size={14} /> GitHub
            </a>
            <button className="btn danger" onClick={() => setConfirmDisconnect(true)}>Disconnect</button>
          </>
        ) : null}
      </div>

      <div className="card" style={{ marginTop: "1.25rem" }}>
        <dl style={{ display: "grid", gridTemplateColumns: "max-content 1fr", gap: "0.5rem 1.5rem", margin: 0 }}>
          <dt className="dim">Provider</dt>
          <dd style={{ margin: 0, textTransform: "capitalize" }}>{i.provider}</dd>
          <dt className="dim">Status</dt>
          <dd style={{ margin: 0 }} className="row">{statusDot(i.status)} <span style={{ textTransform: "capitalize" }}>{i.status}</span></dd>
          {isGithub ? (
            <>
              <dt className="dim">{i.scope === "account" ? "Account" : "Organization"}</dt>
              <dd style={{ margin: 0 }}><a href={`https://github.com/${i.account_login}`} target="_blank" rel="noopener">github.com/{i.account_login}</a></dd>
              {i.scope === "account" && (
                <>
                  <dt className="dim">Organizations</dt>
                  <dd style={{ margin: 0 }} className="row">
                    {(i.orgs ?? []).length > 0
                      ? (i.orgs ?? []).map(o => <span key={o} className="badge plain">{o}</span>)
                      : <span className="dim">none granted</span>}
                    <button className="btn small ghost" title="Re-run the GitHub consent screen to grant more organizations"
                      onClick={() => { window.location.href = "/api/oauth/github/start"; }}>
                      Grant orgs…
                    </button>
                  </dd>
                </>
              )}
              <dt className="dim">Auth</dt>
              <dd style={{ margin: 0 }}>{i.source === "oauth" ? "OAuth" : "Personal access token"}</dd>
              <dt className="dim">Projects</dt>
              <dd style={{ margin: 0 }}><Link to="/projects">{i.project_count}</Link></dd>
            </>
          ) : (
            <>
              <dt className="dim">Account</dt>
              <dd style={{ margin: 0 }}>{i.name || "—"} {i.account_id && <code>{i.account_id}</code>}</dd>
            </>
          )}
          <dt className="dim">Connected</dt>
          <dd style={{ margin: 0 }}>{fmt(i.created_at)}</dd>
          <dt className="dim">Last verified</dt>
          <dd style={{ margin: 0 }}>{fmt(i.last_verified_at)}</dd>
        </dl>
      </div>

      {isGithub && (
        <p className="dim" style={{ marginTop: "0.75rem" }}>
          {i.scope === "account"
            ? <>Repositories from this account and its granted organizations appear on <Link to="/projects">Projects</Link>. <strong>Sync</strong> refreshes the list.</>
            : <>Repositories from this organization appear on <Link to="/projects">Projects</Link>. <strong>Sync</strong> refreshes the list.</>}
        </p>
      )}

      {!isGithub && <CloudflareManagement />}

      <Modal
        open={confirmDisconnect}
        onClose={() => setConfirmDisconnect(false)}
        title={`Disconnect ${title}?`}
        footer={<>
          <span className="spacer" />
          <button className="btn ghost" type="button" onClick={() => setConfirmDisconnect(false)}>Cancel</button>
          <button className="btn danger" type="button" disabled={disconnect.isPending} onClick={() => disconnect.mutate()}>
            {disconnect.isPending ? <span className="spinner" /> : "Disconnect"}
          </button>
        </>}
      >
        <p>
          Removes the connection and <strong>stops all of its managed project containers</strong>.
        </p>
        <p className="dim">
          Data volumes are kept — reconnecting and redeploying restores each project with its data.
        </p>
      </Modal>
    </>
  );
}


// ─── Cloudflare management: tunnel lifecycle + token (moved from Routes) ─────

function CloudflareManagement() {
  const qc = useQueryClient();
  const toast = useToast();
  const [tokenModal, setTokenModal] = useState(false);
  const [tunnelModal, setTunnelModal] = useState(false);
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);

  const { data } = useQuery<TunnelStatus>({
    queryKey: ["tunnel"],
    queryFn: () => api.get<TunnelStatus>("/api/tunnel"),
    refetchInterval: 10000,
  });

  const restart = useMutation({
    mutationFn: () => api.post("/api/tunnel/restart"),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["tunnel"] }); toast.show("Tunnel restarted", "ok"); },
    onError: (e) => toast.show(String(e), "fail"),
  });
  const disconnect = useMutation({
    mutationFn: () => api.post("/api/tunnel/disconnect"),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["tunnel"] }); setConfirmDisconnect(false); toast.show("Tunnel disconnected", "ok"); },
    onError: (e) => toast.show(String(e), "fail"),
  });
  const forgetToken = useMutation({
    mutationFn: () => api.del("/api/tunnel/token"),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tunnel"] });
      qc.invalidateQueries({ queryKey: ["integrations"] });
      toast.show("Cloudflare token cleared", "ok");
    },
    onError: (e) => toast.show(String(e), "fail"),
  });

  if (!data) return <span className="spinner" />;
  const tokenSet = data.cloudflare.token_set;
  const tunnelConnected = data.mode === "remote" && !!data.tunnel_id;

  return (
    <>
      <h2 style={{ marginTop: "2rem" }}>Tunnel</h2>
      <div className="card card-row">
        <div className="grow">
          <div className="row">
            {data.running ? <span className="badge ok">Connected</span>
              : data.exists ? <span className="badge fail">Stopped</span>
              : <span className="badge warn">Not configured</span>}
            {data.tunnel_name && <strong>{data.tunnel_name}</strong>}
          </div>
          <div className="dim" style={{ marginTop: "0.4rem" }}>
            {data.tunnel_id
              ? <code>{data.tunnel_id}</code>
              : tokenSet
                ? "No tunnel yet — create one to route domains through Cloudflare."
                : "Connect a token below first."}
          </div>
        </div>
        <div className="btn-row">
          {!tunnelConnected && tokenSet && data.cloudflare.account_id && (
            <button className="btn primary" onClick={() => setTunnelModal(true)}>
              <Plug size={14} /> Connect tunnel
            </button>
          )}
          {tunnelConnected && (
            <>
              <button className="btn" onClick={() => restart.mutate()} disabled={restart.isPending || !data.exists} title="Rarely needed — the monitor self-heals a downed connector">
                {restart.isPending ? <span className="spinner" /> : <RefreshCw size={14} />} Restart
              </button>
              <button className="btn danger" onClick={() => setConfirmDisconnect(true)} disabled={disconnect.isPending}>
                <Unplug size={14} /> Disconnect
              </button>
            </>
          )}
        </div>
      </div>

      <h2 style={{ marginTop: "1.5rem" }}>API token</h2>
      <div className="card card-row">
        <div className="grow">
          <div className="row">
            {tokenSet ? <span className="badge ok">Connected</span> : <span className="badge warn">Not connected</span>}
            <span className="dim">{data.cloudflare.account_name || data.cloudflare.account_id || ""}</span>
          </div>
        </div>
        <div className="btn-row">
          <button className={`btn ${tokenSet ? "" : "primary"}`} onClick={() => setTokenModal(true)}>
            <Plug size={14} /> {tokenSet ? "Replace token" : "Connect"}
          </button>
          {tokenSet && (
            <button className="btn danger" onClick={() => { if (confirm("Forget the Cloudflare token? The tunnel keeps running until disconnected.")) forgetToken.mutate(); }}>
              <Trash2 size={14} /> Forget
            </button>
          )}
        </div>
      </div>

      <ConnectTokenModal open={tokenModal} onClose={() => setTokenModal(false)} />
      <ConnectTunnelModal open={tunnelModal} onClose={() => setTunnelModal(false)} accountId={data.cloudflare.account_id} />

      <Modal
        open={confirmDisconnect}
        onClose={() => setConfirmDisconnect(false)}
        title="Disconnect the tunnel?"
        footer={<>
          <span className="spacer" />
          <button className="btn ghost" type="button" onClick={() => setConfirmDisconnect(false)}>Cancel</button>
          <button className="btn danger" type="button" disabled={disconnect.isPending} onClick={() => disconnect.mutate()}>
            {disconnect.isPending ? <span className="spinner" /> : "Disconnect"}
          </button>
        </>}
      >
        <p style={{ margin: 0 }}>
          Deletes the tunnel from Cloudflare — every domain routed through it goes offline
          until a new tunnel is connected.
        </p>
      </Modal>
    </>
  );
}

function ConnectTokenModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [token, setToken] = useState("");
  const [accounts, setAccounts] = useState<CloudflareAccount[]>([]);
  const [accountId, setAccountId] = useState("");

  function finishConnected(msg: string) {
    qc.invalidateQueries({ queryKey: ["tunnel"] });
    qc.invalidateQueries({ queryKey: ["integrations"] });
    toast.show(msg, "ok");
    reset(); onClose();
  }

  const submit = useMutation({
    mutationFn: (body: { token: string; account_id?: string }) =>
      api.post<SetTokenResponse>("/api/tunnel/token", body),
    onSuccess: (resp) => {
      if (resp.account_id) finishConnected("Cloudflare token connected");
      else { setAccounts(resp.accounts); toast.show("Pick which Cloudflare account to use", "ok"); }
    },
    onError: (e) => toast.show(String(e), "fail"),
  });

  function reset() { setToken(""); setAccountId(""); setAccounts([]); }
  function close() { reset(); onClose(); }
  function connectWith(raw: string) {
    const t = raw.trim();
    if (!t || submit.isPending) return;
    setToken(t); submit.mutate({ token: t });
  }
  function onPaste(e: React.ClipboardEvent<HTMLInputElement>) {
    const pasted = e.clipboardData.getData("text");
    if (pasted.trim()) { e.preventDefault(); connectWith(pasted); }
  }

  const picking = accounts.length > 0;

  return (
    <Modal
      open={open}
      onClose={close}
      title={picking ? "Pick a Cloudflare account" : "Connect Cloudflare"}
      footer={picking ? <>
        <span className="spacer" />
        <button className="btn ghost" type="button" onClick={close}>Cancel</button>
        <button className="btn primary" type="button" disabled={submit.isPending || !accountId}
          onClick={() => submit.mutate({ token, account_id: accountId })}>
          {submit.isPending ? <span className="spinner" /> : "Use this account"}
        </button>
      </> : <>
        <span className="spacer" />
        <button className="btn ghost" type="button" onClick={close}>Cancel</button>
      </>}
    >
      {picking ? (
        <div className="field">
          <label className="lbl">Account</label>
          <select value={accountId} onChange={e => setAccountId(e.target.value)} required>
            <option value="" disabled>Pick an account…</option>
            {accounts.map(a => (
              <option key={a.id} value={a.id}>{a.name} ({a.id.slice(0, 8)}…)</option>
            ))}
          </select>
          <span className="hint">Tunnels and DNS will be created under the account you pick.</span>
        </div>
      ) : (
        <form onSubmit={e => { e.preventDefault(); connectWith(token); }}>
          <div className="field">
            <label className="lbl">Cloudflare API token</label>
            <input
              type="password" value={token} autoFocus onPaste={onPaste}
              onChange={e => setToken(e.target.value)} disabled={submit.isPending}
              placeholder="Paste your scoped token"
            />
            <span className="hint">
              {submit.isPending ? "Verifying scopes with Cloudflare…"
                : <>Scopes: <code>Cloudflare Tunnel:Edit</code>, <code>DNS:Edit</code>, <code>Zone:Edit</code>, <code>Account Settings:Read</code> — with Zone Resources set to <strong>All zones</strong> so one integration manages every domain in the account.</>}
            </span>
          </div>
          <div className="row" style={{ marginTop: "0.5rem" }}>
            <a className="btn" href={CF_TOKEN_TEMPLATE_URL} target="_blank" rel="noopener">
              <ExternalLink size={14} /> Generate token
            </a>
            <span className="dim">permissions pre-filled on Cloudflare</span>
          </div>
        </form>
      )}
    </Modal>
  );
}

function ConnectTunnelModal({
  open, onClose, accountId,
}: { open: boolean; onClose: () => void; accountId: string | null }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [name, setName] = useState("homebox");

  const create = useMutation({
    mutationFn: () => api.post("/api/tunnel/connect", { name, account_id: accountId }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tunnel"] });
      toast.show("Tunnel created and connected", "ok");
      setName("homebox"); onClose();
    },
    onError: (e) => toast.show(String(e), "fail"),
  });

  function submit(e: FormEvent) { e.preventDefault(); create.mutate(); }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Create a Cloudflare Tunnel"
      footer={<>
        <span className="spacer" />
        <button className="btn ghost" type="button" onClick={onClose}>Cancel</button>
        <button className="btn primary" type="submit" form="cf-tunnel-form" disabled={create.isPending}>
          {create.isPending ? <span className="spinner" /> : "Create tunnel"}
        </button>
      </>}
    >
      <form id="cf-tunnel-form" onSubmit={submit}>
        <div className="field">
          <label className="lbl">Tunnel name</label>
          <input value={name} onChange={e => setName(e.target.value)} placeholder="homebox" required />
          <span className="hint">Shows up in your Cloudflare dashboard. <code>homebox</code> is fine.</span>
        </div>
      </form>
    </Modal>
  );
}
