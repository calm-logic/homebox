import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
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

  // Unified search: filters your synced repos in realtime, and (debounced,
  // or immediately on Enter) searches public GitHub. Yours rank above public.
  const [term, setTerm] = useState("");
  const [debounced, setDebounced] = useState("");
  useEffect(() => {
    const t = setTimeout(() => setDebounced(term.trim()), 400);
    return () => clearTimeout(t);
  }, [term]);

  const { data: pub, isFetching: pubLoading } = useQuery<PublicSearch>({
    queryKey: ["gh-public-search", debounced],
    queryFn: () => api.get<PublicSearch>(`/api/projects/search-public?q=${encodeURIComponent(debounced)}`),
    enabled: open && debounced.length >= 2,
    staleTime: 60_000,
  });

  const q = term.trim().toLowerCase();
  const mine = available.filter(p => !q || p.repo_full_name.toLowerCase().includes(q));
  // Anything already synced locally shows under "yours" — drop duplicates.
  const localFulls = new Set((projects ?? []).map(p => p.repo_full_name.toLowerCase()));
  const publicRepos = (debounced.length >= 2 ? pub?.repos ?? [] : [])
    .filter(r => !localFulls.has(r.full_name.toLowerCase()));

  const addPublic = useMutation({
    mutationFn: async (fullName: string) => {
      const created = await api.post<{ id: number }>("/api/projects/add-url", { url: fullName });
      const adopted = await api.post<{ note?: string }>(`/api/projects/${created.id}/adopt`, {});
      return { id: created.id, note: adopted.note };
    },
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      toast.show("Added public repo — dissecting services", "ok");
      if (r.note) toast.show(r.note, "info");
      setTerm("");
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
      <div className="row" style={{ gap: "0.5rem", marginBottom: "0.75rem" }}>
        <input
          autoFocus
          value={term}
          onChange={e => setTerm(e.target.value)}
          placeholder="Search your repositories and public GitHub…"
          style={{ flex: 1, minWidth: "220px" }}
          onKeyDown={e => { if (e.key === "Enter") setDebounced(term.trim()); }}
        />
        {pubLoading && <span className="spinner" />}
      </div>

      {mine.length === 0 && publicRepos.length === 0 ? (
        <p className="dim" style={{ margin: 0 }}>
          {q
            ? (pubLoading ? "Searching…" : pub?.rate_limited
                ? "GitHub search is rate-limited right now — try again in a minute."
                : "No matches in your repositories or public GitHub.")
            : <>No repositories available to add — every synced repository is already a project,
               or none have been fetched yet. Click <strong>Sync repositories</strong> to pull
               the latest from GitHub, or search for a public repo above.</>}
        </p>
      ) : (
        <div className="provider-list">
          {mine.map(p => {
            const busy = adopt.isPending && adopt.variables?.id === p.id;
            return (
              <button
                key={p.id}
                className="provider-card"
                disabled={adopt.isPending || addPublic.isPending}
                onClick={() => adopt.mutate(p)}
                style={{ width: "100%", textAlign: "left", cursor: "pointer", font: "inherit" }}
              >
                <span style={{ minWidth: 0 }}>
                  <div className="provider-title">{p.repo_full_name}</div>
                  <div className="provider-sub">default branch {p.default_branch}</div>
                </span>
                <span className="spacer" />
                <span className="badge plain">yours</span>
                {busy ? <span className="spinner" /> : <Plus size={16} className="chev" aria-hidden />}
              </button>
            );
          })}
          {publicRepos.map(r => {
            const busy = addPublic.isPending && addPublic.variables === r.full_name;
            return (
              <button
                key={r.full_name}
                className="provider-card"
                disabled={adopt.isPending || addPublic.isPending}
                onClick={() => addPublic.mutate(r.full_name)}
                style={{ width: "100%", textAlign: "left", cursor: "pointer", font: "inherit" }}
                title="Public repo — no push webhooks on repos you don't own; deploys are manual."
              >
                <span style={{ minWidth: 0 }}>
                  <div className="provider-title">{r.full_name}</div>
                  <div className="provider-sub" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {r.description || `★ ${r.stars}`}
                  </div>
                </span>
                <span className="spacer" />
                <span className="badge info plain">public</span>
                {busy ? <span className="spinner" /> : <Plus size={16} className="chev" aria-hidden />}
              </button>
            );
          })}
        </div>
      )}
    </Modal>
  );
}

interface PublicSearch {
  repos: { full_name: string; description: string | null; stars: number; default_branch: string }[];
  rate_limited?: boolean;
}
