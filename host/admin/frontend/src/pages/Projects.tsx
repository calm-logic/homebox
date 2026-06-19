import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, Rocket, Square } from "lucide-react";
import { api } from "../lib/api";
import { useToast } from "../lib/toast";
import { GitHub } from "./GitHub";
import type { DeploymentInfo, RepoItem } from "../lib/types";

const BUSY_STATUSES = ["queued", "cloning", "building", "starting"];

function StatusBadge({ d }: { d: DeploymentInfo | null }) {
  if (!d) return <span className="badge muted plain">Not deployed</span>;
  if (d.status === "running") return <span className="badge ok">Running</span>;
  if (d.status === "failed")
    return <span className="badge fail" title={d.error || undefined}>Failed</span>;
  if (d.status === "stopped") return <span className="badge muted">Stopped</span>;
  // queued / cloning / building / starting
  const label = d.status[0].toUpperCase() + d.status.slice(1);
  return <span className="badge info">{label}…</span>;
}

export function Projects() {
  const { data: repos } = useQuery<RepoItem[]>({
    queryKey: ["repos"],
    queryFn: () => api.get<RepoItem[]>("/api/repositories"),
    refetchInterval: 6000, // keep deploy status badges live
  });

  if (!repos) return <span className="spinner" />;

  const managed = repos.filter(r => r.managed);
  const unmanaged = repos.filter(r => !r.managed);

  return (
    <>
      <h1>Projects</h1>
      <p className="lede">
        Connect a source provider, then mark repositories as managed to deploy them as <code>&lt;slug&gt;.&lt;domain&gt;</code>.
      </p>

      <GitHub />

      <h2 style={{ marginTop: "2rem" }}>Managed projects</h2>
      <p className="dim" style={{ marginTop: "0.4rem", marginBottom: "1rem" }}>
        Repositories Homebox deploys. Toggle a repo on to give it a slug and deploy it; pushes to its default branch redeploy automatically.
      </p>

      {repos.length > 0 ? (
        <table className="data-table">
          <thead><tr><th>Repository</th><th>Branch</th><th>Managed</th><th>Project slug</th><th>Status</th><th className="right" /></tr></thead>
          <tbody>
            {managed.map(r => <RepoRow key={r.id} r={r} />)}
            {unmanaged.map(r => <RepoRow key={r.id} r={r} />)}
          </tbody>
        </table>
      ) : (
        <div className="empty-state">
          <h3>No repositories yet</h3>
          <p>Connect an organization above — its repositories are synced in automatically.</p>
        </div>
      )}
    </>
  );
}

function RepoRow({ r }: { r: RepoItem }) {
  const qc = useQueryClient();
  const toast = useToast();
  const defaultSlug = r.full_name.split("/").pop()!.toLowerCase();
  const [slug, setSlug] = useState(r.project_slug || defaultSlug);

  const invalidate = () => qc.invalidateQueries({ queryKey: ["repos"] });

  const bind = useMutation({
    mutationFn: (vars: { managed: boolean; project_slug: string }) =>
      api.post<RepoItem & { webhook_note?: string }>(`/api/repositories/${r.id}/bind`, vars),
    onSuccess: (res) => {
      invalidate();
      toast.show("Saved", "ok");
      if (res.webhook_note) toast.show(res.webhook_note, "info");
    },
    onError: (e) => toast.show(String(e), "fail"),
  });

  const deploy = useMutation({
    mutationFn: () => api.post(`/api/repositories/${r.id}/deploy`),
    onSuccess: () => { invalidate(); toast.show("Deploy started", "ok"); },
    onError: (e) => toast.show(String(e), "fail"),
  });

  const stop = useMutation({
    mutationFn: () => api.post(`/api/repositories/${r.id}/stop`),
    onSuccess: () => { invalidate(); toast.show("Stopped", "ok"); },
    onError: (e) => toast.show(String(e), "fail"),
  });

  const busy = !!r.deployment && BUSY_STATUSES.includes(r.deployment.status);

  return (
    <tr>
      <td>
        {r.managed
          ? <Link to={`/projects/${r.id}`} title="Open project"><strong>{r.full_name.split("/").pop()}</strong></Link>
          : <strong>{r.full_name.split("/").pop()}</strong>}
        <div className="dim">{r.full_name}</div>
      </td>
      <td><span className="badge muted plain">{r.default_branch}</span></td>
      <td>
        <label className="row" style={{ cursor: "pointer", gap: "0.4rem" }}>
          <input
            type="checkbox"
            checked={r.managed}
            disabled={bind.isPending}
            onChange={e => bind.mutate({ managed: e.target.checked, project_slug: slug })}
          />
          {r.managed ? "On" : "Off"}
        </label>
      </td>
      <td>
        {r.managed ? (
          <div className="row">
            <input value={slug} onChange={e => setSlug(e.target.value)} placeholder={defaultSlug}
              style={{ width: "auto", minWidth: "9em" }} />
            <button className="btn small" disabled={bind.isPending}
              onClick={() => bind.mutate({ managed: true, project_slug: slug })}>Save</button>
          </div>
        ) : <span className="dim">—</span>}
      </td>
      <td>
        <StatusBadge d={r.managed ? r.deployment : null} />
        {r.managed && r.deployment?.status === "running" && r.deployment.url && (
          <>{" "}<a className="dim" href={r.deployment.url} target="_blank" rel="noopener">
            <ExternalLink size={12} /></a></>
        )}
      </td>
      <td className="actions">
        {r.managed && (
          <>
            <button className="btn small" disabled={deploy.isPending || busy}
              onClick={() => deploy.mutate()} title="Deploy now">
              <Rocket size={12} /> Deploy
            </button>{" "}
            {r.deployment && r.deployment.status !== "stopped" && (
              <button className="btn small ghost" disabled={stop.isPending}
                onClick={() => stop.mutate()} title="Stop stack">
                <Square size={12} />
              </button>
            )}{" "}
          </>
        )}
        <a className="btn small ghost" href={`https://github.com/${r.full_name}`} target="_blank" rel="noopener"><ExternalLink size={12} /></a>
      </td>
    </tr>
  );
}
