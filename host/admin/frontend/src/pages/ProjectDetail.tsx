import { useState } from "react";
import { Link, Navigate, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ChevronRight, ExternalLink, RefreshCw, Rocket, Square, Settings } from "lucide-react";
import { api } from "../lib/api";
import { Modal } from "../components/Modal";
import { useToast } from "../lib/toast";
import type {
  DeploymentItem, DeploymentStatus, DomainItem, EnvironmentInfo,
  ProjectDetailData, ProjectWorkflowRun,
} from "../lib/types";

export const BUSY: DeploymentStatus[] = ["queued", "cloning", "dissecting", "building", "starting"];

// Backend timestamps are naive UTC — tag them so Date() doesn't read them as local.
export const utcDate = (iso: string) => new Date(/Z$|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + "Z");

function envDotColor(status: DeploymentStatus | undefined): string {
  if (status === "running") return "var(--accent)";
  if (status === "failed") return "var(--danger)";
  if (status && BUSY.includes(status)) return "var(--info)";
  return "var(--muted)";
}

/** Current-state badge (environment card): is the env serving right now? */
export function depBadge(status: string | undefined, unreachable = false) {
  if (status === "running") {
    return unreachable
      ? <span className="badge fail">Down</span>
      : <span className="badge info">Running</span>;
  }
  if (status === "failed") return <span className="badge fail">Failed</span>;
  if (status === "blocked") return <span className="badge warn">Blocked</span>;
  if (status === "pending_checks") return <span className="badge info">Waiting for checks…</span>;
  if (status === "pending_promotion") return <span className="badge info">Waiting for source env…</span>;
  if (status === "pending_e2e") return <span className="badge info">Running e2e…</span>;
  if (status === "stopped") return <span className="badge muted">Stopped</span>;
  if (status === "superseded") return <span className="badge muted plain">Skipped</span>;
  if (!status) return <span className="badge muted plain">Not deployed</span>;
  return <span className="badge info">{status[0].toUpperCase() + status.slice(1)}…</span>;
}

/** History badge (deployments list/detail): how did this deploy end? */
export function historyBadge(status: string) {
  if (status === "running") return <span className="badge success">Succeeded</span>;
  if (status === "superseded") return <span className="badge muted plain">Skipped</span>;
  if (status === "failed") return <span className="badge fail">Failed</span>;
  if (status === "blocked") return <span className="badge warn">Blocked</span>;
  if (status === "pending_checks") return <span className="badge info">Waiting for checks…</span>;
  if (status === "pending_promotion") return <span className="badge info">Waiting for source env…</span>;
  if (status === "pending_e2e") return <span className="badge info">Running e2e…</span>;
  if (status === "stopped") return <span className="badge muted">Stopped</span>;
  return <span className="badge info">{status[0].toUpperCase() + status.slice(1)}…</span>;
}

export function envUnreachable(env: EnvironmentInfo): boolean {
  return env.deployment?.status === "running"
    && env.instances.some(i => i.url && i.status === "unreachable");
}

export function predictedHost(
  name: string, label: string, slugSuffix: string,
  domain: string | null, mode: "wildcard" | "dedicated" | null,
): string | null {
  if (!domain) return null;
  if (mode === "dedicated") {
    // The project owns the domain: prod at the root, envs as subdomains,
    // non-main services path-proxied (infinitescroll.io/api).
    const envPart = (slugSuffix || "").replace(/^-+/, "");
    const host = envPart ? `${envPart}.${domain}` : domain;
    return label ? `${host}/${label}` : host;
  }
  const base = label ? `${name}-${label}` : name;
  return `${base}${slugSuffix}.${domain}`;
}

export function ProjectDetail() {
  const { projectId } = useParams();
  const id = Number(projectId);
  const qc = useQueryClient();
  const nav = useNavigate();
  const toast = useToast();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [envTab, setEnvTab] = useState<number | null>(null);

  const { data: project, isError } = useQuery<ProjectDetailData>({
    queryKey: ["project", id],
    queryFn: () => api.get<ProjectDetailData>(`/api/projects/${id}`),
    refetchInterval: 6000,
  });
  const { data: runs } = useQuery<ProjectWorkflowRun[]>({
    queryKey: ["project-workflows", id],
    queryFn: () => api.get<ProjectWorkflowRun[]>(`/api/projects/${id}/workflows`),
    enabled: !!project?.managed,
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["project", id] });
  const sync = useMutation({
    mutationFn: () => api.post(`/api/projects/${id}/sync`),
    onSuccess: () => { invalidate(); toast.show("Re-dissected services", "ok"); },
    onError: (e) => toast.show(String(e), "fail"),
  });
  const refreshRuns = useMutation({
    mutationFn: () => api.post(`/api/workflows/refresh`),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["project-workflows", id] }); toast.show("Refreshed", "ok"); },
    onError: (e) => toast.show(String(e), "fail"),
  });

  if (isError) return <Navigate to="/projects" replace />;
  if (!project) return <span className="spinner" />;
  if (!project.managed) return <Navigate to="/projects" replace />;

  return (
    <>
      <Link to="/projects" className="dim" style={{ display: "inline-flex", alignItems: "center", gap: "0.3rem" }}>
        <ArrowLeft size={14} /> Projects
      </Link>

      <div className="row" style={{ marginTop: "0.5rem" }}>
        <h1 style={{ margin: 0 }}>{project.name}</h1>
        <div className="spacer" />
        <button className="btn" disabled={sync.isPending} onClick={() => sync.mutate()} title="Re-read repo and refresh services">
          {sync.isPending ? <span className="spinner" /> : <RefreshCw size={14} />} Sync
        </button>
        <button className="btn ghost" onClick={() => setSettingsOpen(true)}><Settings size={14} /> Settings</button>
        <a className="btn ghost" href={`https://github.com/${project.repo_full_name}`} target="_blank" rel="noopener">
          <ExternalLink size={14} /> GitHub
        </a>
      </div>
      <p className="dim" style={{ marginTop: "0.25rem" }}>
        {project.repo_full_name} · domain <strong>{project.domain ?? "none (set one in Settings)"}</strong>
        {project.dissected_at && <> · {project.services.length} service{project.services.length === 1 ? "" : "s"}</>}
      </p>

      {/* ─── Environments (one tab per env) ─────────────────────── */}
      <h2 style={{ marginTop: "1.5rem" }}>Environments</h2>
      {(() => {
        const activeEnv = project.environments.find(e => e.id === envTab) ?? project.environments[0];
        if (!activeEnv) return null;
        return (
          <>
            <div className="tabs" role="tablist">
              {project.environments.map(env => (
                <button
                  key={env.id}
                  role="tab"
                  aria-selected={env.id === activeEnv.id}
                  className={`tab ${env.id === activeEnv.id ? "active" : ""}`}
                  onClick={() => setEnvTab(env.id)}
                >
                  <span className="tab-dot" style={{ background: envUnreachable(env) ? "var(--danger)" : envDotColor(env.deployment?.status) }} />
                  <span style={{ textTransform: "capitalize" }}>{env.name}</span>
                </button>
              ))}
            </div>
            <EnvironmentCard key={activeEnv.id} projectId={id} env={activeEnv} onChange={invalidate} />
            <Deployments projectId={id} envId={activeEnv.id} />
          </>
        );
      })()}

      {/* ─── Services ───────────────────────────────────────────── */}
      <h2 style={{ marginTop: "2rem" }}>Services</h2>
      {project.services.length === 0 ? (
        <div className="card" style={{ marginTop: "0.5rem" }}>
          <span className="dim">No services detected yet. Click <strong>Sync</strong> to dissect the repo.</span>
        </div>
      ) : (
        <table className="data-table" style={{ marginTop: "0.5rem" }}>
          <thead><tr><th>Service</th><th>Kind</th><th>Exposure</th><th>Hostname</th><th>Env</th><th className="right" /></tr></thead>
          <tbody>
            {project.services.map(s => {
              const host = s.is_public ? predictedHost(project.name, s.subdomain_label, "", project.domain, project.domain_mode) : null;
              return (
                <tr key={s.id} className="clickable" onClick={() => nav(`/projects/${id}/services/${s.id}`)}>
                  <td><strong>{s.name}</strong>{s.internal_port && <span className="dim"> :{s.internal_port}</span>}</td>
                  <td><span className="badge plain">{s.kind}</span></td>
                  <td>{s.is_public ? <span className="badge ok">Public</span> : <span className="badge muted plain">Internal</span>}</td>
                  <td className="dim">{host ? <code>{host}</code> : "—"}</td>
                  <td className="dim">{s.env_vars.length}{s.env_vars.some(v => v.source === "auto") && <span className="badge info plain" style={{ marginLeft: 6 }}>auto</span>}</td>
                  <td className="actions"><ChevronRight size={15} className="dim" aria-hidden /></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      {/* ─── CI/CD runs ─────────────────────────────────────────── */}
      <div className="row" style={{ marginTop: "2rem" }}>
        <h2 style={{ margin: 0 }}>CI/CD runs</h2>
        <div className="spacer" />
        <button className="btn small" disabled={refreshRuns.isPending} onClick={() => refreshRuns.mutate()}>
          <RefreshCw size={12} /> Refresh
        </button>
      </div>
      {runs && runs.length > 0 ? (
        <table className="data-table" style={{ marginTop: "0.5rem" }}>
          <thead><tr><th>Workflow</th><th>Branch</th><th>Status</th><th>When</th><th className="right" /></tr></thead>
          <tbody>
            {runs.map(run => (
              <tr key={run.id}>
                <td>{run.name}</td>
                <td><span className="badge muted plain">{run.head_branch}</span></td>
                <td>{runBadge(run.status, run.conclusion)}</td>
                <td className="dim">{run.created_at ? new Date(run.created_at).toLocaleString() : "—"}</td>
                <td className="actions"><a className="btn small ghost" href={run.html_url} target="_blank" rel="noopener"><ExternalLink size={12} /></a></td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div className="card" style={{ marginTop: "0.5rem" }}>
          <span className="dim">No GitHub Actions runs cached. Click <strong>Refresh</strong> to pull them.</span>
        </div>
      )}

      {settingsOpen && <SettingsModal project={project} onClose={() => { setSettingsOpen(false); invalidate(); }} />}
    </>
  );
}

function EnvironmentCard({ projectId, env, onChange }: { projectId: number; env: EnvironmentInfo; onChange: () => void }) {
  const toast = useToast();
  const dep = env.deployment;
  const busy = !!dep && BUSY.includes(dep.status);

  const deploy = useMutation({
    mutationFn: () => api.post(`/api/projects/${projectId}/environments/${env.id}/deploy`),
    onSuccess: () => { onChange(); toast.show(`Deploying ${env.name}`, "ok"); },
    onError: (e) => toast.show(String(e), "fail"),
  });
  const stop = useMutation({
    mutationFn: () => api.post(`/api/projects/${projectId}/environments/${env.id}/stop`),
    onSuccess: () => { onChange(); toast.show(`Stopped ${env.name}`, "ok"); },
    onError: (e) => toast.show(String(e), "fail"),
  });

  return (
    <div className="card">
      <div className="row">
        {depBadge(dep?.status, envUnreachable(env))}
        <strong style={{ textTransform: "capitalize" }}>{env.name}</strong>
        <span className="dim">{env.branch ? `branch ${env.branch}` : "default branch"}</span>
      </div>
      <div style={{ marginTop: "0.6rem", display: "flex", flexDirection: "column", gap: "0.3rem" }}>
        {env.instances.filter(i => i.url).length > 0
          ? env.instances.filter(i => i.url).map(i => {
              const down = i.status === "unreachable";
              return (
                <div key={i.service_name} className="row" style={{ justifyContent: "space-between" }}>
                  <span className="dim">{i.service_name}</span>
                  <a
                    href={i.url!} target="_blank" rel="noopener"
                    style={down ? { color: "var(--danger)" } : undefined}
                    title={down ? "URL did not respond on the last check" : undefined}
                  >
                    {i.url!.replace("https://", "")} <ExternalLink size={11} />
                  </a>
                </div>
              );
            })
          : <span className="dim">No public URLs yet — deploy to create them.</span>}
      </div>
      {dep?.status === "failed" && dep.error && (
        <pre className="dim" style={{ marginTop: "0.5rem", whiteSpace: "pre-wrap", fontSize: "0.72rem", maxHeight: 120, overflow: "auto" }}>{dep.error}</pre>
      )}
      {dep?.commit_sha && <div className="dim" style={{ marginTop: "0.4rem" }}>commit <code>{dep.commit_sha.slice(0, 7)}</code>{dep.trigger && <> · {dep.trigger}</>}</div>}
      <div className="btn-row" style={{ marginTop: "0.7rem" }}>
        <button className="btn small primary" disabled={deploy.isPending || busy} onClick={() => deploy.mutate()}>
          <Rocket size={12} /> Deploy
        </button>
        {dep && dep.status !== "stopped" && (
          <button className="btn small ghost" disabled={stop.isPending} onClick={() => stop.mutate()}>
            <Square size={12} /> Stop
          </button>
        )}
      </div>
    </div>
  );
}

function Deployments({ projectId, envId }: { projectId: number; envId: number }) {
  const nav = useNavigate();
  const { data: deps } = useQuery<DeploymentItem[]>({
    queryKey: ["env-deployments", envId],
    queryFn: () => api.get<DeploymentItem[]>(`/api/projects/${projectId}/environments/${envId}/deployments`),
    refetchInterval: 6000,
  });

  if (!deps) return <span className="spinner" />;
  if (deps.length === 0) return null;

  return (
    <>
      <h3>Deployments</h3>
      <table className="data-table" style={{ margin: "0.25rem 0 0" }}>
        <thead><tr><th>Status</th><th>Commit</th><th>Trigger</th><th>Started</th><th className="right" /></tr></thead>
        <tbody>
          {deps.map(d => (
            <tr
              key={d.id}
              className="clickable"
              onClick={() => nav(`/projects/${projectId}/deployments/${d.id}`)}
            >
              <td title={d.error ?? undefined}>{historyBadge(d.status)}</td>
              <td>{d.commit_sha ? <code>{d.commit_sha.slice(0, 7)}</code> : <span className="dim">—</span>}</td>
              <td><span className="badge plain muted" style={{ textTransform: "capitalize" }}>{d.trigger}</span></td>
              <td className="dim">{d.created_at ? utcDate(d.created_at).toLocaleString() : "—"}</td>
              <td className="actions"><ChevronRight size={15} className="dim" aria-hidden /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

function runBadge(status: string, conclusion: string | null) {
  if (status === "completed") {
    if (conclusion === "success") return <span className="badge ok">Success</span>;
    if (conclusion === "failure" || conclusion === "timed_out") return <span className="badge fail">Failed</span>;
    if (conclusion === "cancelled") return <span className="badge warn">Cancelled</span>;
    return <span className="badge plain">{conclusion || "Completed"}</span>;
  }
  if (status === "in_progress") return <span className="badge info">Running</span>;
  if (status === "queued") return <span className="badge info">Queued</span>;
  return <span className="badge plain">{status}</span>;
}

// ─── Settings modal (name + domain) ───────────────────────────────────────────
function SettingsModal({ project, onClose }: { project: ProjectDetailData; onClose: () => void }) {
  const toast = useToast();
  const [name, setName] = useState(project.name);
  const [domainId, setDomainId] = useState<string>(project.domain_id ? String(project.domain_id) : "");
  const [autoDeploy, setAutoDeploy] = useState(project.auto_deploy);
  const [requireChecks, setRequireChecks] = useState(project.require_checks);
  // Per-environment domain overrides ("" = inherit the project domain).
  const [envDomains, setEnvDomains] = useState<Record<number, string>>(
    Object.fromEntries(project.environments.map(e => [e.id, e.domain_id ? String(e.domain_id) : ""]))
  );
  const [envGates, setEnvGates] = useState<Record<number, boolean>>(
    Object.fromEntries(project.environments.map(e => [e.id, e.promotion_gate]))
  );
  const [envE2e, setEnvE2e] = useState<Record<number, string>>(
    Object.fromEntries(project.environments.map(e => [e.id, e.e2e_workflow ?? ""]))
  );

  const { data: domains } = useQuery<DomainItem[]>({ queryKey: ["domains"], queryFn: () => api.get<DomainItem[]>("/api/domains") });

  const resolveDomain = (id: string): DomainItem | undefined =>
    id ? (domains ?? []).find(d => d.id === Number(id)) : (domains ?? []).find(d => d.is_primary);
  const domainName = (id: string): string => resolveDomain(id)?.name ?? "…";
  const projectDomain = domainName(domainId);
  const projectDomainMode = resolveDomain(domainId)?.mode ?? null;
  const effectiveEnvDomainObj = (envId: number) =>
    envDomains[envId] ? resolveDomain(envDomains[envId]) : resolveDomain(domainId);

  const save = useMutation({
    mutationFn: async () => {
      await api.patch(`/api/projects/${project.id}`, {
        name, domain_id: domainId ? Number(domainId) : 0,
        auto_deploy: autoDeploy, require_checks: requireChecks,
      });
      for (const env of project.environments) {
        const body: Record<string, unknown> = {};
        const chosen = envDomains[env.id] ?? "";
        if (chosen !== (env.domain_id ? String(env.domain_id) : "")) body.domain_id = chosen ? Number(chosen) : 0;
        const gate = envGates[env.id] ?? false;
        if (gate !== env.promotion_gate) body.promotion_gate = gate;
        const e2e = (envE2e[env.id] ?? "").trim();
        if (e2e !== (env.e2e_workflow ?? "")) body.e2e_workflow = e2e;
        if (Object.keys(body).length > 0) {
          await api.patch(`/api/projects/${project.id}/environments/${env.id}`, body);
        }
      }
    },
    onSuccess: () => { toast.show("Saved — redeploy to apply hostname changes", "ok"); onClose(); },
    onError: (e) => toast.show(String(e), "fail"),
  });

  return (
    <Modal open onClose={onClose} title="Project settings" footer={<>
      <span className="spacer" />
      <button className="btn ghost" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={save.isPending} onClick={() => save.mutate()}>
        {save.isPending ? <span className="spinner" /> : "Save"}
      </button>
    </>}>
      <div className="field">
        <label className="lbl">Project name (URL slug)</label>
        <input value={name} onChange={e => setName(e.target.value)} placeholder={project.name} />
        <span className="hint">
          {projectDomainMode === "dedicated"
            ? <>Dedicated domain — the app lives at <code>{projectDomain}</code> (name is used for stacks and wildcard fallbacks).</>
            : <>Used as the hostname base, e.g. <code>{predictedHost(name || project.name, "", "", projectDomain, projectDomainMode)}</code>.</>}
        </span>
      </div>
      <div className="field">
        <label className="lbl">Domain</label>
        <select value={domainId} onChange={e => setDomainId(e.target.value)}>
          <option value="">Primary (default)</option>
          {(domains ?? []).map(d => <option key={d.id} value={d.id}>{d.name}{d.is_primary ? " (primary)" : ""}</option>)}
        </select>
      </div>

      <div className="lbl" style={{ marginTop: "0.75rem", marginBottom: "0.3rem" }}>Per-environment overrides</div>
      {project.environments.map(env => (
        <div key={env.id} className="row" style={{ gap: "0.6rem", marginBottom: "0.4rem" }}>
          <span style={{ textTransform: "capitalize", flex: "0 0 5.5rem" }}>{env.name}</span>
          <select
            value={envDomains[env.id] ?? ""}
            onChange={e => setEnvDomains({ ...envDomains, [env.id]: e.target.value })}
            style={{ flex: 1 }}
          >
            <option value="">Project domain</option>
            {(domains ?? []).map(d => <option key={d.id} value={d.id}>{d.name}</option>)}
          </select>
          <code style={{ flex: "0 0 auto" }}>
            {predictedHost(name || project.name, "", env.slug_suffix,
              effectiveEnvDomainObj(env.id)?.name ?? "…", effectiveEnvDomainObj(env.id)?.mode ?? null)}
          </code>
        </div>
      ))}
      <span className="hint">Point environments at different domains, e.g. production on its own domain.</span>

      <div className="lbl" style={{ marginTop: "0.85rem", marginBottom: "0.3rem" }}>Deploy pipeline</div>
      {project.environments.map(env => {
        const others = project.environments.filter(e => e.id !== env.id);
        const sourceName = others.find(e => e.id === (env.promote_from_env_id ?? -1))?.name
          ?? others.find(e => e.kind !== "production")?.name ?? "dev";
        const gated = envGates[env.id] ?? false;
        return (
          <div key={env.id} className="row" style={{ gap: "0.6rem", marginBottom: "0.4rem", flexWrap: "wrap" }}>
            <span style={{ textTransform: "capitalize", flex: "0 0 5.5rem" }}>{env.name}</span>
            <select
              value={gated ? "promote" : "push"}
              onChange={e => setEnvGates({ ...envGates, [env.id]: e.target.value === "promote" })}
              style={{ flex: "0 0 15rem" }}
            >
              <option value="push">Deploy on push</option>
              <option value="promote">Promote from {sourceName} after it deploys</option>
            </select>
            {gated && (
              <input
                placeholder="e2e workflow file, e.g. e2e.yml (optional)"
                value={envE2e[env.id] ?? ""}
                onChange={e => setEnvE2e({ ...envE2e, [env.id]: e.target.value })}
                style={{ flex: 1, minWidth: "12rem" }}
              />
            )}
          </div>
        );
      })}
      <span className="hint">
        Promotion waits for the source environment to deploy this commit, then (if set) dispatches the
        e2e workflow with <code>base_url</code>/<code>environment</code> inputs against it — this
        environment deploys only when that passes.
      </span>

      <label className="row" style={{ cursor: "pointer", gap: "0.4rem", marginTop: "0.85rem" }}>
        <input type="checkbox" checked={autoDeploy} onChange={e => setAutoDeploy(e.target.checked)} />
        Auto-deploy on push to the tracked branch
      </label>
      <label className="row" style={{ cursor: "pointer", gap: "0.4rem", marginTop: "0.5rem" }}>
        <input type="checkbox" checked={requireChecks} onChange={e => setRequireChecks(e.target.checked)} disabled={!autoDeploy} />
        Wait for GitHub checks to pass before deploying
      </label>
      <span className="hint">Applies only when the repo has workflows; repos without CI deploy immediately.</span>
    </Modal>
  );
}
