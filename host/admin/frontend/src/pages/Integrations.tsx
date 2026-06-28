import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Github, RefreshCw, Plug, ExternalLink, Cloud } from "lucide-react";
import { api } from "../lib/api";
import { Modal } from "../components/Modal";
import { useToast } from "../lib/toast";
import type { IntegrationItem, OAuthSettings } from "../lib/types";

/**
 * Integrations — every connection to an external system (GitHub orgs +
 * Cloudflare). GitHub is connected here (OAuth or PAT); Cloudflare is connected
 * on the Routes page but listed here too.
 */
export function Integrations() {
  const qc = useQueryClient();
  const toast = useToast();
  const [openConnect, setOpenConnect] = useState(false);
  const [disconnectTarget, setDisconnectTarget] = useState<IntegrationItem | null>(null);

  const { data: integrations } = useQuery<IntegrationItem[]>({
    queryKey: ["integrations"],
    queryFn: () => api.get<IntegrationItem[]>("/api/integrations"),
  });
  const { data: oauth } = useQuery<OAuthSettings>({
    queryKey: ["oauth-settings"],
    queryFn: () => api.get<OAuthSettings>("/api/oauth/settings"),
  });

  const sync = useMutation({
    mutationFn: (id: number) => api.post(`/api/integrations/${id}/sync`),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["projects"] }); toast.show("Synced repositories", "ok"); },
    onError: (e) => toast.show(String(e), "fail"),
  });
  const disconnect = useMutation({
    mutationFn: (id: number) => api.del(`/api/integrations/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["integrations"] });
      qc.invalidateQueries({ queryKey: ["projects"] });
      toast.show("Disconnected", "ok");
      setDisconnectTarget(null);
    },
    onError: (e) => toast.show(String(e), "fail"),
  });

  function startOAuth() {
    window.location.href = "/api/oauth/github/start";
  }

  const github = (integrations ?? []).filter(i => i.provider === "github");
  const others = (integrations ?? []).filter(i => i.provider !== "github");

  return (
    <>
      <h1>Integrations</h1>
      <p className="lede">
        Connections to the systems Homebox builds on — GitHub organizations for source,
        Cloudflare for routing. Credentials are encrypted at rest on this host.
      </p>

      <div className="row" style={{ marginTop: "1rem" }}>
        <h2 style={{ margin: 0 }}>GitHub</h2>
        <div className="spacer" />
        {oauth?.configured && (
          <button className="btn primary" onClick={startOAuth}>
            <Github size={14} /> Connect with GitHub
          </button>
        )}
        <button className="btn" onClick={() => setOpenConnect(true)}>
          <Plug size={14} /> Use a token
        </button>
      </div>

      {!oauth?.configured && (
        <div className="card" style={{ marginTop: "0.75rem", borderColor: "var(--border-strong)" }}>
          <div className="row">
            <span className="badge warn">OAuth proxy unreachable</span>
            <span className="dim">Falling back to personal-access-token connect.</span>
          </div>
        </div>
      )}

      {github.length > 0 ? (
        <table className="data-table" style={{ marginTop: "0.75rem" }}>
          <thead><tr><th>Organization</th><th>Source</th><th>Projects</th><th>Status</th><th className="right">Actions</th></tr></thead>
          <tbody>
            {github.map(i => (
              <tr key={i.id}>
                <td>
                  <strong>{i.account_login}</strong>
                  <div className="dim">github.com/{i.account_login}</div>
                </td>
                <td>{i.source === "oauth" ? <span className="badge info plain">OAuth</span> : <span className="badge plain">PAT</span>}</td>
                <td><span className="dim">{i.project_count}</span></td>
                <td><span className="badge ok">Connected</span></td>
                <td className="actions">
                  <button className="btn small" disabled={sync.isPending} onClick={() => sync.mutate(i.id)}>
                    <RefreshCw size={12} /> Sync repos
                  </button>{" "}
                  <button className="btn small danger" disabled={disconnect.isPending}
                    onClick={() => setDisconnectTarget(i)}>
                    Disconnect
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : integrations ? (
        <div className="empty-state" style={{ marginTop: "0.75rem" }}>
          <h3>No GitHub organizations connected</h3>
          <p>Connect your first org to start deploying its repositories.</p>
          {oauth?.configured && (
            <button className="btn primary" onClick={startOAuth}><Github size={14} /> Connect with GitHub</button>
          )}
        </div>
      ) : <span className="spinner" />}

      <h2 style={{ marginTop: "2rem" }}>Cloudflare</h2>
      {others.length > 0 ? (
        <table className="data-table" style={{ marginTop: "0.75rem" }}>
          <thead><tr><th>Provider</th><th>Account</th><th>Status</th><th className="right">Manage</th></tr></thead>
          <tbody>
            {others.map(i => (
              <tr key={i.id}>
                <td><strong style={{ textTransform: "capitalize" }}>{i.provider}</strong></td>
                <td className="dim">{i.name || i.account_id || "—"}</td>
                <td>{i.status === "connected" ? <span className="badge ok">Connected</span> : <span className="badge warn">{i.status}</span>}</td>
                <td className="actions"><Link className="btn small ghost" to="/tunnel">Routes <ExternalLink size={12} /></Link></td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div className="card" style={{ marginTop: "0.75rem" }}>
          <div className="row">
            <span className="badge warn"><Cloud size={12} /> Not connected</span>
            <span className="dim">Connect Cloudflare on the <Link to="/tunnel">Routes</Link> page to publish projects.</span>
          </div>
        </div>
      )}

      <ConnectPatModal open={openConnect} onClose={() => setOpenConnect(false)} />

      <Modal
        open={disconnectTarget !== null}
        onClose={() => setDisconnectTarget(null)}
        title={`Disconnect ${disconnectTarget?.account_login ?? ""}?`}
        footer={<>
          <span className="spacer" />
          <button className="btn" type="button" onClick={() => setDisconnectTarget(null)}>Cancel</button>
          <button className="btn danger" type="button" disabled={disconnect.isPending}
            onClick={() => disconnectTarget && disconnect.mutate(disconnectTarget.id)}>
            {disconnect.isPending ? <span className="spinner" /> : "Disconnect"}
          </button>
        </>}
      >
        <p>
          This removes the GitHub connection for <strong>{disconnectTarget?.account_login}</strong> and
          <strong> stops all of its managed project containers</strong> on this host.
        </p>
        <p className="dim">
          Project data volumes (databases, uploads) are kept, so reconnecting and redeploying
          restores each project with its data.
        </p>
      </Modal>
    </>
  );
}

function ConnectPatModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [login, setLogin] = useState("");
  const [pat, setPat] = useState("");

  const connect = useMutation({
    mutationFn: () => api.post(`/api/integrations/github/connect-pat`, { login, pat }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["integrations"] });
      qc.invalidateQueries({ queryKey: ["projects"] });
      toast.show("Connected", "ok");
      setLogin(""); setPat("");
      onClose();
    },
    onError: (e) => toast.show(String(e), "fail"),
  });

  function submit(e: FormEvent) { e.preventDefault(); connect.mutate(); }

  const tokenUrl = "https://github.com/settings/tokens/new?scopes=repo,admin:org,admin:repo_hook,workflow&description=Homebox%20Admin";

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Connect with a personal token"
      footer={<>
        <span className="spacer" />
        <button className="btn" type="button" onClick={onClose}>Cancel</button>
        <button className="btn primary" type="submit" form="connect-pat-form" disabled={connect.isPending}>
          {connect.isPending ? <span className="spinner" /> : "Connect"}
        </button>
      </>}
    >
      <form id="connect-pat-form" onSubmit={submit}>
        <div className="field">
          <label className="lbl">Organization login</label>
          <input value={login} onChange={e => setLogin(e.target.value)} placeholder="my-org" required />
          <span className="hint">The slug from github.com/<strong>my-org</strong>.</span>
        </div>
        <div className="field">
          <label className="lbl">Personal access token</label>
          <input type="password" value={pat} onChange={e => setPat(e.target.value)} placeholder="ghp_… or github_pat_…" required />
          <span className="hint">Needs <code>repo</code>, <code>admin:org</code>, and <code>admin:repo_hook</code> (for push auto-deploy) scopes.</span>
        </div>
        <div className="row" style={{ marginTop: "0.5rem" }}>
          <a className="btn" href={tokenUrl} target="_blank" rel="noopener">
            <ExternalLink size={14} /> Generate token on GitHub
          </a>
          <span className="dim">opens a tab with the right scopes pre-filled</span>
        </div>
      </form>
    </Modal>
  );
}
