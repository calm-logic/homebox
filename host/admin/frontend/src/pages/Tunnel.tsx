import { FormEvent, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, Play, Plug, Unplug, ExternalLink, Cloud, Trash2, Activity, Wrench, Gauge } from "lucide-react";
import { api } from "../lib/api";
import { Modal } from "../components/Modal";
import { useToast } from "../lib/toast";
import { Domains } from "./Domains";
import type { CloudflareAccount, DnsReport, DnsResyncResult, SetTokenResponse, TunnelStatus, UptimeReport, UptimeStatus } from "../lib/types";

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

      {tunnelConnected && <TunnelUptime />}

      {tunnelConnected && <RoutingHealth />}

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

// ─── Tunnel / infrastructure uptime ───────────────────────────────────────────
// Uptime % + a recent status timeline per piece of infra, fed by the background
// monitor (app/monitor.py), which also self-heals a downed connector/traefik.

const UPTIME_COLORS: Record<UptimeStatus, string> = {
  up: "var(--ok, #2a9d4a)",
  degraded: "var(--warn, #d9a400)",
  down: "var(--fail, #d33)",
  unknown: "var(--border, #cbd5e1)",
};
const UPTIME_BADGE: Record<UptimeStatus, string> = {
  up: "ok", degraded: "warn", down: "fail", unknown: "warn",
};
const COMPONENT_LABEL: Record<string, string> = {
  admin_url: "Public URL (end-to-end)",
  tunnel: "Tunnel at Cloudflare edge",
  cloudflared: "Connector (cloudflared)",
  traefik: "Traefik router",
  docker_proxy: "Docker socket proxy",
};
const UPTIME_WINDOWS = ["6h", "24h", "7d", "14d"];

function Sparkline({ points }: { points: { status: UptimeStatus }[] }) {
  if (points.length === 0) return <span className="dim">collecting…</span>;
  return (
    <div style={{ display: "flex", gap: 1, alignItems: "stretch", height: 16 }}>
      {points.map((p, i) => (
        <div
          key={i}
          title={p.status}
          style={{ width: 4, borderRadius: 1, background: UPTIME_COLORS[p.status] ?? UPTIME_COLORS.unknown }}
        />
      ))}
    </div>
  );
}

function TunnelUptime() {
  const [window, setWindow] = useState("24h");
  const { data } = useQuery<UptimeReport>({
    queryKey: ["tunnel-uptime", window],
    queryFn: () => api.get<UptimeReport>(`/api/tunnel/uptime?window=${window}`),
    refetchInterval: 15000,
  });

  return (
    <div className="card" style={{ marginTop: "1rem" }}>
      <div className="card-row">
        <div className="grow">
          <div className="row">
            <span className="badge ok"><Gauge size={12} /> Uptime</span>
            <span className="dim">Infrastructure health (auto-monitored every 30s, self-healing)</span>
          </div>
        </div>
        <div className="btn-row">
          {UPTIME_WINDOWS.map((w) => (
            <button
              key={w}
              className={`btn ${w === window ? "primary" : ""}`}
              onClick={() => setWindow(w)}
            >
              {w}
            </button>
          ))}
        </div>
      </div>

      <div style={{ marginTop: "0.75rem", display: "flex", flexDirection: "column", gap: "0.6rem" }}>
        {!data ? (
          <span className="spinner" />
        ) : (
          data.components.map((c) => (
            <div key={c.component} className="row" style={{ justifyContent: "space-between", gap: "0.75rem", flexWrap: "wrap" }}>
              <div className="row" style={{ gap: "0.5rem", minWidth: 0 }}>
                <span className={`badge ${UPTIME_BADGE[c.current]}`}>{c.current}</span>
                <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>
                  {COMPONENT_LABEL[c.component] ?? c.component}
                </span>
              </div>
              <div className="row" style={{ gap: "0.75rem" }}>
                <Sparkline points={c.timeline} />
                {c.latency_ms != null && <span className="dim">{c.latency_ms} ms</span>}
                <strong style={{ minWidth: "3.5rem", textAlign: "right" }}>
                  {c.uptime_pct == null ? "—" : `${c.uptime_pct}%`}
                </strong>
              </div>
            </div>
          ))
        )}
      </div>

      {data && data.components.every((c) => c.sample_count === 0) && (
        <div className="dim" style={{ marginTop: "0.6rem" }}>
          No samples yet — the monitor records one per component every 30 seconds.
        </div>
      )}
    </div>
  );
}

// ─── DNS routing health ───────────────────────────────────────────────────────
// Ingress decides what the tunnel serves; DNS decides which tunnel the edge
// routes a hostname to. They drift when a tunnel is re-created/adopted (new id)
// but the CNAMEs still point at the old, dead target → Cloudflare Error 1033 /
// HTTP 530. This panel checks each managed record and one-click repairs them.

const DNS_STATUS: Record<string, { cls: string; label: string }> = {
  ok:      { cls: "ok",   label: "OK" },
  stale:   { cls: "fail", label: "Stale" },
  missing: { cls: "warn", label: "Missing" },
  no_zone: { cls: "warn", label: "No zone" },
  error:   { cls: "fail", label: "Error" },
};

function RoutingHealth() {
  const qc = useQueryClient();
  const toast = useToast();
  const [open, setOpen] = useState(false);

  const dns = useQuery<DnsReport>({
    queryKey: ["tunnel-dns"],
    queryFn: () => api.get<DnsReport>("/api/tunnel/dns"),
    enabled: open,
  });

  const repair = useMutation({
    mutationFn: () => api.post<DnsResyncResult>("/api/tunnel/resync-dns"),
    onSuccess: (r) => {
      const n = r.updated.length;
      toast.show(n ? `Repointed ${n} DNS record${n === 1 ? "" : "s"} at this tunnel` : "DNS already up to date", "ok");
      setOpen(true);
      qc.invalidateQueries({ queryKey: ["tunnel-dns"] });
      qc.invalidateQueries({ queryKey: ["tunnel"] });
    },
    onError: (e) => toast.show(String(e), "fail"),
  });

  const report = dns.data;
  const badge = !open || dns.isLoading
    ? null
    : report?.in_sync
      ? <span className="badge ok">All routed</span>
      : <span className="badge fail">Out of sync</span>;

  return (
    <div className="card" style={{ marginTop: "1rem" }}>
      <div className="card-row">
        <div className="grow">
          <div className="row">
            {badge ?? <span className="badge warn">Unchecked</span>}
            <span className="dim">DNS routing health</span>
          </div>
          <div className="dim" style={{ marginTop: "0.4rem" }}>
            Verifies each domain's Cloudflare DNS record still points at this tunnel.
            Stale records are the usual cause of <code>Error 1033</code> / HTTP 530.
          </div>
        </div>
        <div className="btn-row">
          <button className="btn" onClick={() => { setOpen(true); dns.refetch(); }} disabled={dns.isFetching}>
            {dns.isFetching ? <span className="spinner" /> : <Activity size={14} />} Check routing
          </button>
          <button className="btn primary" onClick={() => repair.mutate()} disabled={repair.isPending}>
            {repair.isPending ? <span className="spinner" /> : <Wrench size={14} />} Repair DNS
          </button>
        </div>
      </div>

      {open && report?.error && (
        <div className="dim" style={{ marginTop: "0.75rem", color: "var(--fail, #d33)" }}>
          Couldn't read DNS from Cloudflare: {report.error}
        </div>
      )}

      {open && report && !report.error && report.records.length === 0 && (
        <div className="dim" style={{ marginTop: "0.75rem" }}>
          No Cloudflare-routed domains yet — add one under Domains below.
        </div>
      )}

      {open && report && report.records.length > 0 && (
        <div style={{ marginTop: "0.75rem", display: "flex", flexDirection: "column", gap: "0.4rem" }}>
          {report.records.map((r) => {
            const s = DNS_STATUS[r.status] ?? { cls: "warn", label: r.status };
            return (
              <div key={r.hostname} className="row" style={{ justifyContent: "space-between", gap: "0.75rem" }}>
                <div className="row" style={{ gap: "0.5rem", minWidth: 0 }}>
                  <span className={`badge ${s.cls}`}>{s.label}</span>
                  <code style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{r.hostname}</code>
                </div>
                <span className="dim" style={{ textAlign: "right" }}>
                  {r.status === "ok" && "→ this tunnel (proxied)"}
                  {r.status === "stale" && (r.proxied === false ? "DNS-only (not proxied)" : `→ ${r.actual}`)}
                  {r.status === "missing" && "no CNAME record"}
                  {r.status === "no_zone" && "zone not in this Cloudflare account"}
                  {r.status === "error" && (r.error || "lookup failed")}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
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
