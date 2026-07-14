import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ChevronRight, Plus, RefreshCw } from "lucide-react";
import { api } from "../lib/api";
import { Modal } from "../components/Modal";
import { useToast } from "../lib/toast";
import type { DeploymentStatus, EnvironmentInfo, IntegrationItem, ProjectItem } from "../lib/types";

const BUSY: DeploymentStatus[] = ["queued", "cloning", "dissecting", "building", "starting"];

function envBadge(env: EnvironmentInfo) {
  const s = env.deployment?.status;
  if (s === "running") {
    const down = env.instances.some(i => i.url && i.status === "unreachable");
    return down
      ? <span className="badge fail" title="Public URL not responding">{env.name}</span>
      : <span className="badge ok">{env.name}</span>;
  }
  if (s === "failed") return <span className="badge fail" title={env.deployment?.error || undefined}>{env.name}</span>;
  if (s === "stopped") return <span className="badge muted">{env.name}</span>;
  if (s && BUSY.includes(s)) return <span className="badge info">{env.name}…</span>;
  return <span className="badge muted plain">{env.name}</span>;
}

export function Projects() {
  const [addOpen, setAddOpen] = useState(false);

  const { data: projects } = useQuery<ProjectItem[]>({
    queryKey: ["projects"],
    queryFn: () => api.get<ProjectItem[]>("/api/projects"),
    refetchInterval: 6000, // keep deploy badges live
  });
  const { data: integrations } = useQuery<IntegrationItem[]>({
    queryKey: ["integrations"],
    queryFn: () => api.get<IntegrationItem[]>("/api/integrations"),
  });

  if (!projects) return <span className="spinner" />;

  const added = projects.filter(p => p.managed).sort((a, b) => a.name.localeCompare(b.name));
  const hasGithub = (integrations ?? []).some(i => i.provider === "github");

  return (
    <>
      <div className="row">
        <h1 style={{ margin: 0 }}>Projects</h1>
        <div className="spacer" />
        {hasGithub && (
          <button className="btn primary" onClick={() => setAddOpen(true)}><Plus size={14} /> Add</button>
        )}
      </div>
      <p className="lede" style={{ marginTop: "0.5rem" }}>
        Projects added from GitHub, each dissected into services and deployed per environment.
      </p>

      {!hasGithub ? (
        <div className="empty-state">
          <h3>No GitHub connected</h3>
          <p>Connect a GitHub organization, then add projects from its repositories.</p>
          <Link className="btn primary" to="/integrations">Connect GitHub</Link>
        </div>
      ) : added.length === 0 ? (
        <div className="empty-state">
          <h3>No projects yet</h3>
          <p>Add a project by selecting one of your GitHub repositories.</p>
          <button className="btn primary" onClick={() => setAddOpen(true)}><Plus size={14} /> Add</button>
        </div>
      ) : (
        <table className="data-table" style={{ marginTop: "1rem" }}>
          <thead><tr><th>Project</th><th>Repository</th><th>Environments</th><th className="right" /></tr></thead>
          <tbody>
            {added.map(p => <ProjectRow key={p.id} p={p} />)}
          </tbody>
        </table>
      )}

      <AddProjectModal open={addOpen} onClose={() => setAddOpen(false)} />
    </>
  );
}

function ProjectRow({ p }: { p: ProjectItem }) {
  const nav = useNavigate();
  const repoShort = p.repo_full_name.split("/").pop();

  return (
    <tr className="clickable" onClick={() => nav(`/projects/${p.id}`)}>
      <td><strong>{p.name}</strong></td>
      <td className="dim">
        <a href={`https://github.com/${p.repo_full_name}`} target="_blank" rel="noopener" onClick={e => e.stopPropagation()}>
          {repoShort}
        </a>{" "}
        <span className="badge muted plain">{p.default_branch}</span>
      </td>
      <td>
        {p.environments.length > 0 ? (
          <div className="row" style={{ gap: "0.35rem", flexWrap: "wrap" }}>
            {p.environments.map(e => (
              <Link key={e.id} to={`/projects/${p.id}?env=${e.id}`} onClick={ev => ev.stopPropagation()}>
                {envBadge(e)}
              </Link>
            ))}
          </div>
        ) : <span className="dim">—</span>}
      </td>
      <td className="actions"><ChevronRight size={15} className="dim" aria-hidden /></td>
    </tr>
  );
}

// ─── Add project: pick an un-added repo from a connected GitHub org ───────────
function AddProjectModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const nav = useNavigate();
  const toast = useToast();

  const { data: projects } = useQuery<ProjectItem[]>({
    queryKey: ["projects"],
    queryFn: () => api.get<ProjectItem[]>("/api/projects"),
    enabled: open,
  });
  const { data: integrations } = useQuery<IntegrationItem[]>({
    queryKey: ["integrations"],
    queryFn: () => api.get<IntegrationItem[]>("/api/integrations"),
    enabled: open,
  });

  const githubIntegrations = (integrations ?? []).filter(i => i.provider === "github");
  const available = (projects ?? []).filter(p => !p.managed);

  const sync = useMutation({
    mutationFn: () => Promise.all(githubIntegrations.map(i => api.post(`/api/integrations/${i.id}/sync`))),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["projects"] }); toast.show("Synced repositories", "ok"); },
    onError: (e) => toast.show(String(e), "fail"),
  });
  const adopt = useMutation({
    mutationFn: (p: ProjectItem) => api.post<{ note?: string; webhook_note?: string }>(`/api/projects/${p.id}/adopt`, {}),
    onSuccess: (r, p) => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      toast.show(`Added ${p.name} — dissecting services`, "ok");
      if (r.note) toast.show(r.note, "info");
      onClose();
      nav(`/projects/${p.id}`);
    },
    onError: (e) => toast.show(String(e), "fail"),
  });

  // Public repo by URL: register (integration-less, anonymous clone) + adopt.
  const [url, setUrl] = useState("");
  const addUrl = useMutation({
    mutationFn: async () => {
      const created = await api.post<{ id: number }>("/api/projects/add-url", { url: url.trim() });
      const adopted = await api.post<{ note?: string }>(`/api/projects/${created.id}/adopt`, {});
      return { id: created.id, note: adopted.note };
    },
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      toast.show("Added public repo — dissecting services", "ok");
      if (r.note) toast.show(r.note, "info");
      setUrl("");
      onClose();
      nav(`/projects/${r.id}`);
    },
    onError: (e) => toast.show(String(e), "fail"),
  });

  return (
    <Modal open={open} onClose={onClose} title="Add project" footer={
      <>
        <button className="btn ghost" type="button" disabled={sync.isPending} onClick={() => sync.mutate()}>
          {sync.isPending ? <span className="spinner" /> : <RefreshCw size={14} />} Sync repositories
        </button>
        <span className="spacer" />
        <button className="btn ghost" type="button" onClick={onClose}>Cancel</button>
      </>
    }>
      {available.length === 0 ? (
        <p className="dim" style={{ margin: 0 }}>
          No repositories available to add — every synced repository is already a project,
          or none have been fetched yet. Click <strong>Sync repositories</strong> to pull the latest from GitHub.
        </p>
      ) : (
        <div className="provider-list">
          {available.map(p => {
            const busy = adopt.isPending && adopt.variables?.id === p.id;
            return (
              <button
                key={p.id}
                className="provider-card"
                disabled={adopt.isPending}
                onClick={() => adopt.mutate(p)}
                style={{ width: "100%", textAlign: "left", cursor: "pointer", font: "inherit" }}
              >
                <span style={{ minWidth: 0 }}>
                  <div className="provider-title">{p.repo_full_name}</div>
                  <div className="provider-sub">default branch {p.default_branch}</div>
                </span>
                <span className="spacer" />
                {busy ? <span className="spinner" /> : <Plus size={16} className="chev" aria-hidden />}
              </button>
            );
          })}
        </div>
      )}

      <div className="section-divider">or add a public repo</div>
      <div className="row" style={{ gap: "0.5rem" }}>
        <input
          value={url}
          onChange={e => setUrl(e.target.value)}
          placeholder="https://github.com/owner/repo"
          style={{ flex: 1, minWidth: "220px" }}
          onKeyDown={e => { if (e.key === "Enter" && url.trim() && !addUrl.isPending) addUrl.mutate(); }}
        />
        <button className="btn primary" disabled={!url.trim() || addUrl.isPending} onClick={() => addUrl.mutate()}>
          {addUrl.isPending ? <span className="spinner" /> : <><Plus size={14} /> Add</>}
        </button>
      </div>
      <span className="hint" style={{ display: "block", marginTop: "0.35rem" }}>
        Any public GitHub repo. No push webhooks on repos you don't own — deploys are manual.
      </span>
    </Modal>
  );
}
