import { FormEvent, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Github, RefreshCw, Plug, ExternalLink } from "lucide-react";
import { api } from "../lib/api";
import { Modal } from "../components/Modal";
import { useToast } from "../lib/toast";
import type { OAuthSettings, OrgItem } from "../lib/types";

export function GitHub() {
  const qc = useQueryClient();
  const toast = useToast();
  const [openConnect, setOpenConnect] = useState(false);
  const [disconnectTarget, setDisconnectTarget] = useState<string | null>(null);

  const { data: orgs } = useQuery<OrgItem[]>({
    queryKey: ["orgs"],
    queryFn: () => api.get<OrgItem[]>("/api/organizations"),
  });
  const { data: oauth } = useQuery<OAuthSettings>({
    queryKey: ["oauth-settings"],
    queryFn: () => api.get<OAuthSettings>("/api/oauth/settings"),
  });

  const sync = useMutation({
    mutationFn: (login: string) => api.post(`/api/organizations/${login}/sync`),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["repos"] }); toast.show("Synced", "ok"); },
    onError: (e) => toast.show(String(e), "fail"),
  });
  const disconnect = useMutation({
    mutationFn: (login: string) => api.del(`/api/organizations/${login}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["orgs"] });
      qc.invalidateQueries({ queryKey: ["repos"] });
      toast.show("Disconnected", "ok");
      setDisconnectTarget(null);
    },
    onError: (e) => toast.show(String(e), "fail"),
  });

  function startOAuth() {
    // The backend produces a redirect URL that includes a signed state and
    // the proxy URL — we simply navigate to it.
    window.location.href = "/api/oauth/github/start";
  }

  return (
    <>
      <div className="row" style={{ marginTop: "0.5rem" }}>
        <h2 style={{ margin: 0 }}>Source</h2>
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
      <p className="dim" style={{ marginTop: "0.4rem", marginBottom: "1rem" }}>Connect GitHub organizations to deploy their repositories. Tokens are encrypted at rest on this host.</p>

      {!oauth?.configured && (
        <div className="card" style={{ marginBottom: "1rem", borderColor: "var(--border-strong)" }}>
          <div className="row">
            <span className="badge warn">OAuth proxy unreachable</span>
            <span className="dim">Falling back to personal-access-token connect. See <a href="https://homebox.sh" target="_blank" rel="noopener">homebox.sh</a> for setup.</span>
          </div>
        </div>
      )}

      {orgs && orgs.length > 0 ? (
        <table className="data-table">
          <thead><tr><th>Organization</th><th>Source</th><th>Connected</th><th className="right">Actions</th></tr></thead>
          <tbody>
            {orgs.map(o => (
              <tr key={o.id}>
                <td>
                  <strong>{o.login}</strong>
                  <div className="dim">github.com/{o.login}</div>
                </td>
                <td>{o.source === "oauth" ? <span className="badge info plain">OAuth</span> : <span className="badge plain">PAT</span>}</td>
                <td><span className="badge ok">Active</span> <span className="dim">since {new Date(o.created_at).toLocaleDateString()}</span></td>
                <td className="actions">
                  <button className="btn small" disabled={sync.isPending} onClick={() => sync.mutate(o.login)}>
                    <RefreshCw size={12} /> Sync repos
                  </button>{" "}
                  <button className="btn small danger" disabled={disconnect.isPending}
                    onClick={() => setDisconnectTarget(o.login)}>
                    Disconnect
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : orgs ? (
        <div className="empty-state">
          <h3>No organizations connected</h3>
          <p>Connect your first GitHub org to start deploying repositories from it.</p>
          {oauth?.configured && (
            <button className="btn primary" onClick={startOAuth}><Github size={14} /> Connect with GitHub</button>
          )}
        </div>
      ) : <span className="spinner" />}

      <ConnectPatModal open={openConnect} onClose={() => setOpenConnect(false)} />

      <Modal
        open={disconnectTarget !== null}
        onClose={() => setDisconnectTarget(null)}
        title={`Disconnect ${disconnectTarget ?? ""}?`}
        footer={<>
          <span className="spacer" />
          <button className="btn" type="button" onClick={() => setDisconnectTarget(null)}>Cancel</button>
          <button className="btn danger" type="button" disabled={disconnect.isPending}
            onClick={() => disconnectTarget && disconnect.mutate(disconnectTarget)}>
            {disconnect.isPending ? <span className="spinner" /> : "Disconnect"}
          </button>
        </>}
      >
        <p>
          This removes the GitHub connection for <strong>{disconnectTarget}</strong> and
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
    mutationFn: () => api.post(`/api/organizations/connect-pat`, { login, pat }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["orgs"] });
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
