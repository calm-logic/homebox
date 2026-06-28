import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Play, RefreshCw, Trash2, Plus } from "lucide-react";
import { api } from "../lib/api";
import { useToast } from "../lib/toast";
import type { RunnerSummary, IntegrationItem } from "../lib/types";

export function Runner() {
  const qc = useQueryClient();
  const toast = useToast();

  const { data: runner } = useQuery<RunnerSummary>({
    queryKey: ["runner"],
    queryFn: () => api.get<RunnerSummary>("/api/runner"),
    refetchInterval: 6000,
  });
  const { data: integrations } = useQuery<IntegrationItem[]>({
    queryKey: ["integrations"],
    queryFn: () => api.get<IntegrationItem[]>("/api/integrations"),
  });
  const orgs = (integrations ?? []).filter(
    (i): i is IntegrationItem & { account_login: string } => i.provider === "github" && !!i.account_login
  );

  const install = useMutation({
    mutationFn: (login: string) => api.post(`/api/runner/install`, { org: login }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["runner"] }); toast.show("Runner started", "ok"); },
    onError: (e) => toast.show(String(e), "fail"),
  });
  const restart = useMutation({
    mutationFn: (name: string) => api.post(`/api/runner/${name}/restart`),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["runner"] }); toast.show("Runner restarted", "ok"); },
    onError: (e) => toast.show(String(e), "fail"),
  });
  const remove = useMutation({
    mutationFn: (name: string) => api.del(`/api/runner/${name}`),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["runner"] }); toast.show("Runner removed", "ok"); },
    onError: (e) => toast.show(String(e), "fail"),
  });

  if (!runner || !integrations) return <span className="spinner" />;

  const runnerByOrg: Record<string, typeof runner.containers[number] | undefined> = {};
  for (const c of runner.containers) runnerByOrg[c.org] = c;

  return (
    <>
      <h2 style={{ marginTop: "0.5rem" }}>Self-hosted runner</h2>
      <p className="dim" style={{ marginTop: "0.4rem", marginBottom: "1rem" }}>
        GitHub Actions runners run as containers on this host — fully managed from this UI. Workflows targeting <code>runs-on: [self-hosted, homebox]</code> will be picked up.
      </p>

      {orgs.length === 0 ? (
        <div className="empty-state">
          <h3>Connect a source organization first</h3>
          <p>Once an organization is connected (Integrations tab), you can install a runner that picks up jobs from every repo in it.</p>
          <a className="btn primary" href="/integrations">Go to Integrations</a>
        </div>
      ) : orgs.map(org => {
        const c = runnerByOrg[org.account_login];
        return (
          <div className="card" key={org.id}>
            <div className="card-row">
              <div className="grow">
                <div className="row">
                  <h3 style={{ margin: 0 }}>{org.account_login}</h3>
                  {c
                    ? (c.running
                        ? <span className="badge ok">Running</span>
                        : <span className="badge warn">{c.state}</span>)
                    : <span className="badge muted">Not installed</span>}
                </div>
                {c && (
                  <div className="dim" style={{ marginTop: "0.4rem" }}>
                    Container: <code>{c.name}</code> · image: <code>{c.image}</code>
                    {c.started_at && <> · started {new Date(c.started_at).toLocaleString()}</>}
                  </div>
                )}
                {!c && (
                  <div className="dim" style={{ marginTop: "0.4rem" }}>
                    Click <strong>Install runner</strong> to spin up a Docker-based GitHub Actions runner registered to this organization.
                  </div>
                )}
              </div>
              <div className="btn-row">
                {!c && (
                  <button className="btn primary" disabled={install.isPending}
                    onClick={() => install.mutate(org.account_login)}>
                    {install.isPending ? <span className="spinner" /> : <><Plus size={14} /> Install runner</>}
                  </button>
                )}
                {c && (
                  <>
                    <button className="btn" disabled={restart.isPending}
                      onClick={() => restart.mutate(c.name)}>
                      {restart.isPending ? <span className="spinner" /> : <><RefreshCw size={14} /> Restart</>}
                    </button>
                    <button className="btn danger" disabled={remove.isPending}
                      onClick={() => { if (confirm(`Remove runner for ${org.account_login}?`)) remove.mutate(c.name); }}>
                      <Trash2 size={14} /> Remove
                    </button>
                  </>
                )}
              </div>
            </div>

            {runner.org_runners[org.account_login] && runner.org_runners[org.account_login].length > 0 && (
              <table className="data-table" style={{ marginTop: "0.75rem" }}>
                <thead><tr><th>Name</th><th>Status</th><th>OS</th><th>Labels</th></tr></thead>
                <tbody>
                  {runner.org_runners[org.account_login].map(r => (
                    <tr key={r.id}>
                      <td><strong>{r.name}</strong></td>
                      <td>{r.status === "online" ? <span className="badge ok">Online</span> : <span className="badge warn">{r.status}</span>}</td>
                      <td>{r.os}</td>
                      <td>{r.labels.map(l => <span key={l.name} className="badge plain muted" style={{ marginRight: 4 }}>{l.name}</span>)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        );
      })}
    </>
  );
}
