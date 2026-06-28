import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, RefreshCw } from "lucide-react";
import { api } from "../lib/api";
import { useToast } from "../lib/toast";
import { Runner } from "./Runner";
import type { WorkflowRun } from "../lib/types";

export function CICD() {
  const qc = useQueryClient();
  const toast = useToast();
  const { data: runs } = useQuery<WorkflowRun[]>({
    queryKey: ["workflows"],
    queryFn: () => api.get<WorkflowRun[]>("/api/workflows"),
  });
  const refresh = useMutation({
    mutationFn: () => api.post("/api/workflows/refresh"),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["workflows"] }); toast.show("Refreshed", "ok"); },
    onError: (e) => toast.show(String(e), "fail"),
  });

  if (!runs) return <span className="spinner" />;

  return (
    <>
      <h1>CI/CD</h1>
      <p className="lede">Self-hosted runners and recent workflow runs across your connected repositories.</p>

      <Runner />

      <div className="row" style={{ marginTop: "2rem" }}>
        <h2 style={{ margin: 0 }}>Workflow runs</h2>
        <div className="spacer" />
        <button className="btn primary" onClick={() => refresh.mutate()} disabled={refresh.isPending}>
          {refresh.isPending ? <span className="spinner" /> : <><RefreshCw size={14} /> Refresh from GitHub</>}
        </button>
      </div>

      {runs.length > 0 ? (
        <table className="data-table">
          <thead><tr>
            <th>Repository</th><th>Workflow</th><th>Branch</th><th>Status</th><th>When</th><th className="right" />
          </tr></thead>
          <tbody>
            {runs.map(r => (
              <tr key={r.id}>
                <td>
                  <strong>{r.repository_full_name.split("/").pop()}</strong>
                  <div className="dim">{r.repository_full_name}</div>
                </td>
                <td>{r.name}</td>
                <td><span className="badge muted plain">{r.head_branch}</span></td>
                <td>{statusBadge(r.status, r.conclusion)}</td>
                <td><span className="dim">{new Date(r.created_at).toLocaleString()}</span></td>
                <td className="actions"><a className="btn small ghost" href={r.html_url} target="_blank" rel="noopener"><ExternalLink size={12} /></a></td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div className="empty-state">
          <h3>No runs yet</h3>
          <p>Connect an organization on the <strong>Integrations</strong> tab, sync repositories, then click <strong>Refresh</strong> to pull recent workflow runs from GitHub.</p>
          <a className="btn primary" href="/integrations">Go to Integrations</a>
        </div>
      )}
    </>
  );
}

function statusBadge(status: string, conclusion: string | null) {
  if (status === "completed") {
    if (conclusion === "success") return <span className="badge ok">Success</span>;
    if (conclusion === "failure" || conclusion === "timed_out") return <span className="badge fail">{conclusion}</span>;
    if (conclusion === "cancelled") return <span className="badge warn">Cancelled</span>;
    return <span className="badge plain">{conclusion || "completed"}</span>;
  }
  if (status === "in_progress") return <span className="badge info">In progress</span>;
  return <span className="badge plain">{status}</span>;
}
