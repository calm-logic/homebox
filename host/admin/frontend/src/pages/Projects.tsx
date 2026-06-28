import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, Boxes } from "lucide-react";
import { api } from "../lib/api";
import { useToast } from "../lib/toast";
import type { DeploymentStatus, EnvironmentInfo, ProjectItem } from "../lib/types";

const BUSY: DeploymentStatus[] = ["queued", "cloning", "dissecting", "building", "starting"];

function envBadge(env: EnvironmentInfo) {
  const s = env.deployment?.status;
  if (s === "running") return <span className="badge ok">{env.name}</span>;
  if (s === "failed") return <span className="badge fail" title={env.deployment?.error || undefined}>{env.name}</span>;
  if (s === "stopped") return <span className="badge muted">{env.name}</span>;
  if (s && BUSY.includes(s)) return <span className="badge info">{env.name}…</span>;
  return <span className="badge muted plain">{env.name}</span>;
}

function prodUrl(p: ProjectItem): string | null {
  const prod = p.environments.find(e => e.kind === "production") ?? p.environments[0];
  const inst = prod?.instances.find(i => i.url);
  return inst?.url ?? null;
}

export function Projects() {
  const { data: projects } = useQuery<ProjectItem[]>({
    queryKey: ["projects"],
    queryFn: () => api.get<ProjectItem[]>("/api/projects"),
    refetchInterval: 6000, // keep deploy badges live
  });

  if (!projects) return <span className="spinner" />;

  // Group by integration org.
  const groups = new Map<string, ProjectItem[]>();
  for (const p of projects) {
    const key = p.integration?.account_login ?? "Unlinked";
    (groups.get(key) ?? groups.set(key, []).get(key)!).push(p);
  }
  const managedFirst = (a: ProjectItem, b: ProjectItem) =>
    Number(b.managed) - Number(a.managed) || a.name.localeCompare(b.name);

  return (
    <>
      <h1>Projects</h1>
      <p className="lede">
        Adopt a repository to deploy it. Homebox dissects it into services and gives each one a
        URL per environment — e.g. <code>box.x100.dev</code>, <code>box-api.x100.dev</code>, and
        their <code>--dev</code> variants.
      </p>

      {projects.length === 0 ? (
        <div className="empty-state">
          <h3>No projects yet</h3>
          <p>Connect a GitHub organization on the <strong>Integrations</strong> tab — its repositories appear here.</p>
          <Link className="btn primary" to="/integrations">Go to Integrations</Link>
        </div>
      ) : (
        [...groups.entries()].map(([org, items]) => (
          <div key={org} style={{ marginTop: "1.5rem" }}>
            <h2 style={{ marginBottom: "0.5rem" }}>{org}</h2>
            <table className="data-table">
              <thead><tr><th>Project</th><th>Repository</th><th>Adopted</th><th>Environments</th><th className="right" /></tr></thead>
              <tbody>
                {[...items].sort(managedFirst).map(p => <ProjectRow key={p.id} p={p} />)}
              </tbody>
            </table>
          </div>
        ))
      )}
    </>
  );
}

function ProjectRow({ p }: { p: ProjectItem }) {
  const qc = useQueryClient();
  const toast = useToast();
  const invalidate = () => qc.invalidateQueries({ queryKey: ["projects"] });

  const adopt = useMutation({
    mutationFn: () => api.post<{ note?: string; webhook_note?: string }>(`/api/projects/${p.id}/adopt`, {}),
    onSuccess: (r) => {
      invalidate();
      toast.show("Adopted — dissecting services", "ok");
      if (r.note) toast.show(r.note, "info");
    },
    onError: (e) => toast.show(String(e), "fail"),
  });
  const release = useMutation({
    mutationFn: () => api.post(`/api/projects/${p.id}/release`),
    onSuccess: () => { invalidate(); toast.show("Released", "ok"); },
    onError: (e) => toast.show(String(e), "fail"),
  });

  const url = prodUrl(p);
  const repoShort = p.repo_full_name.split("/").pop();

  return (
    <tr>
      <td>
        {p.managed
          ? <Link to={`/projects/${p.id}`} title="Open project"><strong>{p.name}</strong></Link>
          : <strong>{p.name}</strong>}
        {url && <> {" "}<a className="dim" href={url} target="_blank" rel="noopener"><ExternalLink size={12} /></a></>}
      </td>
      <td className="dim">{repoShort} <span className="badge muted plain">{p.default_branch}</span></td>
      <td>
        <label className="row" style={{ cursor: "pointer", gap: "0.4rem" }}>
          <input
            type="checkbox"
            checked={p.managed}
            disabled={adopt.isPending || release.isPending}
            onChange={e => (e.target.checked ? adopt : release).mutate()}
          />
          {p.managed ? "On" : "Off"}
        </label>
      </td>
      <td>
        {p.managed
          ? <div className="row" style={{ gap: "0.35rem", flexWrap: "wrap" }}>{p.environments.map(e => <span key={e.id}>{envBadge(e)}</span>)}</div>
          : <span className="dim">—</span>}
      </td>
      <td className="actions">
        {p.managed && (
          <Link className="btn small" to={`/projects/${p.id}`}><Boxes size={12} /> Open</Link>
        )}{" "}
        <a className="btn small ghost" href={`https://github.com/${p.repo_full_name}`} target="_blank" rel="noopener"><ExternalLink size={12} /></a>
      </td>
    </tr>
  );
}
