import { FormEvent, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, Play, Plug, Unplug, ExternalLink, Cloud, Trash2 } from "lucide-react";
import { api } from "../lib/api";
import { Modal } from "../components/Modal";
import { useToast } from "../lib/toast";
import { Domains } from "./Domains";
import type { CloudflareAccount, SetTokenResponse, TunnelStatus } from "../lib/types";

const CF_TOKEN_TEMPLATE_URL =
  // Pre-fills Cloudflare's "Create Custom Token" with the scopes Homebox needs.
  // (The user still names the token + clicks Create — we can't issue tokens for them.)
  "https://dash.cloudflare.com/profile/api-tokens?permissionGroupKeys=%5B%7B%22key%22%3A%22cfd_tunnel%22%2C%22type%22%3A%22edit%22%7D%2C%7B%22key%22%3A%22account_settings%22%2C%22type%22%3A%22read%22%7D%2C%7B%22key%22%3A%22dns%22%2C%22type%22%3A%22edit%22%7D%2C%7B%22key%22%3A%22zone%22%2C%22type%22%3A%22read%22%7D%5D&name=Homebox+Admin";

export function Tunnel() {
  const qc = useQueryClient();
  const toast = useToast();
  const [tokenModal, setTokenModal] = useState(false);
  const [connectModal, setConnectModal] = useState(false);

  const { data } = useQuery<TunnelStatus>({
    queryKey: ["tunnel"],
    queryFn: () => api.get<TunnelStatus>("/api/tunnel"),
    refetchInterval: 5000,
  });

  const restart = useMutation({
    mutationFn: () => api.post("/api/tunnel/restart"),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["tunnel"] }); toast.show("Tunnel restarted", "ok"); },
    onError: (e) => toast.show(String(e), "fail"),
  });
  const apply = useMutation({
    mutationFn: () => api.post("/api/tunnel/apply"),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["tunnel"] }); toast.show("Ingress applied", "ok"); },
    onError: (e) => toast.show(String(e), "fail"),
  });
  const disconnect = useMutation({
    mutationFn: () => api.post("/api/tunnel/disconnect"),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["tunnel"] }); toast.show("Tunnel disconnected", "ok"); },
    onError: (e) => toast.show(String(e), "fail"),
  });
  const forgetToken = useMutation({
    mutationFn: () => api.del("/api/tunnel/token"),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["tunnel"] }); toast.show("Cloudflare token cleared", "ok"); },
    onError: (e) => toast.show(String(e), "fail"),
  });

  if (!data) return <span className="spinner" />;

  const tokenSet = data.cloudflare.token_set;
  const accountSet = !!data.cloudflare.account_id;
  const tunnelConnected = data.mode === "remote" && !!data.tunnel_id;

  return (
    <>
      <h1>Routes</h1>
      <p className="lede">A secure Cloudflare Tunnel from your host to the edge — no open ports, no public IP needed. Domains added here are routed to your projects through this tunnel.</p>

      {/* ─── Cloudflare account card ─────────────────────────────── */}
      <div className="card card-row">
        <div className="grow">
          <div className="row">
            {tokenSet
              ? <span className="badge ok">Token connected</span>
              : <span className="badge warn">Not connected</span>}
            <span className="dim">Cloudflare account</span>
          </div>
          <div className="dim" style={{ marginTop: "0.4rem" }}>
            {tokenSet
              ? <>Account: <strong>{data.cloudflare.account_name || data.cloudflare.account_id}</strong></>
              : <>Connect a Cloudflare API token to manage tunnels and domains from this UI.</>}
          </div>
        </div>
        <div className="btn-row">
          {tokenSet ? (
            <>
              <button className="btn" onClick={() => setTokenModal(true)}>
                <Plug size={14} /> Replace token
              </button>
              <button className="btn danger" onClick={() => { if (confirm("Forget the Cloudflare token? The tunnel will keep running until you disconnect it.")) forgetToken.mutate(); }}>
                <Trash2 size={14} /> Forget
              </button>
            </>
          ) : (
            <button className="btn primary" onClick={() => setTokenModal(true)}>
              <Cloud size={14} /> Connect Cloudflare
            </button>
          )}
        </div>
      </div>

      {/* ─── Tunnel card ────────────────────────────────────────── */}
      <div className="card card-row" style={{ marginTop: "1rem" }}>
        <div className="grow">
          <div className="row">
            {data.running ? <span className="badge ok">Connected</span>
              : data.exists ? <span className="badge fail">Stopped</span>
              : <span className="badge warn">Not configured</span>}
            <span className="dim">homebox-cloudflared</span>
          </div>
          <div className="dim" style={{ marginTop: "0.4rem" }}>
            {data.tunnel_id ? (
              <>
                {data.tunnel_name && <>Tunnel: <strong>{data.tunnel_name}</strong> · </>}
                <code>{data.tunnel_id}</code>
              </>
            ) : tokenSet
              ? <>No tunnel yet. Click <strong>Connect tunnel</strong> to create one in your Cloudflare account.</>
              : <>Connect a Cloudflare API token above, then create a tunnel here.</>}
          </div>
        </div>
        <div className="btn-row">
          {!tunnelConnected && tokenSet && accountSet && (
            <button className="btn primary" onClick={() => setConnectModal(true)}>
              <Plug size={14} /> Connect tunnel
            </button>
          )}
          {tunnelConnected && (
            <>
              <button className="btn" onClick={() => restart.mutate()} disabled={restart.isPending || !data.exists}>
                {restart.isPending ? <span className="spinner" /> : <RefreshCw size={14} />} Restart
              </button>
              <button className="btn primary" onClick={() => apply.mutate()} disabled={apply.isPending}>
                {apply.isPending ? <span className="spinner" /> : <Play size={14} />} Apply ingress
              </button>
              <button className="btn danger" onClick={() => { if (confirm("Disconnect the tunnel? This deletes it from Cloudflare too.")) disconnect.mutate(); }} disabled={disconnect.isPending}>
                <Unplug size={14} /> Disconnect
              </button>
            </>
          )}
        </div>
      </div>

      <Domains />

      <ConnectTokenModal open={tokenModal} onClose={() => setTokenModal(false)} />
      <ConnectTunnelModal
        open={connectModal}
        onClose={() => setConnectModal(false)}
        accountId={data.cloudflare.account_id}
      />
    </>
  );
}

// ─── Token connect modal ──────────────────────────────────────────────────────

function ConnectTokenModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [token, setToken] = useState("");
  const [accounts, setAccounts] = useState<CloudflareAccount[]>([]);
  const [accountId, setAccountId] = useState("");

  const submit = useMutation({
    mutationFn: (body: { token: string; account_id?: string }) =>
      api.post<SetTokenResponse>("/api/tunnel/token", body),
    onSuccess: (resp) => {
      if (resp.account_id) {
        // Fully resolved — close modal.
        qc.invalidateQueries({ queryKey: ["tunnel"] });
        toast.show("Cloudflare token connected", "ok");
        reset();
        onClose();
      } else {
        // Multi-account — ask the user to pick one.
        setAccounts(resp.accounts);
        toast.show("Pick which Cloudflare account to use", "ok");
      }
    },
    onError: (e) => toast.show(String(e), "fail"),
  });

  function reset() {
    setToken(""); setAccountId(""); setAccounts([]);
  }
  function close() { reset(); onClose(); }
  function save(e: FormEvent) {
    e.preventDefault();
    submit.mutate({ token, account_id: accountId || undefined });
  }

  return (
    <Modal
      open={open}
      onClose={close}
      title={accounts.length > 0 ? "Pick a Cloudflare account" : "Connect Cloudflare"}
      footer={<>
        <span className="spacer" />
        <button className="btn" type="button" onClick={close}>Cancel</button>
        <button className="btn primary" type="submit" form="cf-token-form" disabled={submit.isPending || (accounts.length > 0 && !accountId)}>
          {submit.isPending ? <span className="spinner" /> : (accounts.length > 0 ? "Use this account" : "Connect")}
        </button>
      </>}
    >
      <form id="cf-token-form" onSubmit={save}>
        {accounts.length === 0 ? (
          <>
            <div className="field">
              <label className="lbl">Cloudflare API token</label>
              <input
                type="password"
                value={token}
                onChange={e => setToken(e.target.value)}
                placeholder="Paste your scoped token here"
                required
              />
              <span className="hint">Stored encrypted at rest. Scopes needed: <code>Cloudflare Tunnel:Edit</code>, <code>DNS:Edit</code>, <code>Zone:Read</code>, <code>Account Settings:Read</code>.</span>
            </div>
            <div className="row" style={{ marginTop: "0.5rem" }}>
              <a className="btn" href={CF_TOKEN_TEMPLATE_URL} target="_blank" rel="noopener">
                <ExternalLink size={14} /> Generate token on Cloudflare
              </a>
              <span className="dim">opens a tab with the right scopes pre-filled</span>
            </div>
          </>
        ) : (
          <div className="field">
            <label className="lbl">Account</label>
            <select value={accountId} onChange={e => setAccountId(e.target.value)} required>
              <option value="" disabled>Pick an account…</option>
              {accounts.map(a => (
                <option key={a.id} value={a.id}>{a.name} ({a.id.slice(0, 8)}…)</option>
              ))}
            </select>
            <span className="hint">Your token has access to multiple Cloudflare accounts. Tunnels and DNS will be created under the one you pick.</span>
          </div>
        )}
      </form>
    </Modal>
  );
}

// ─── Tunnel create modal ──────────────────────────────────────────────────────

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
        <button className="btn" type="button" onClick={onClose}>Cancel</button>
        <button className="btn primary" type="submit" form="cf-tunnel-form" disabled={create.isPending}>
          {create.isPending ? <span className="spinner" /> : "Create tunnel"}
        </button>
      </>}
    >
      <form id="cf-tunnel-form" onSubmit={submit}>
        <div className="field">
          <label className="lbl">Tunnel name</label>
          <input value={name} onChange={e => setName(e.target.value)} placeholder="homebox" required />
          <span className="hint">Shows up as the tunnel name in your Cloudflare dashboard.</span>
        </div>
        <p className="dim">
          A new tunnel will be created in your Cloudflare account with ingress managed remotely.
          The connector token is stored encrypted on this host and used to run the local cloudflared container.
        </p>
      </form>
    </Modal>
  );
}
