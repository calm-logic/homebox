import { useEffect, useRef, useState } from "react";
import { Link, Navigate, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useMutation, UseMutationResult, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft, ChevronRight, ExternalLink, MoreVertical, RefreshCw, Rocket, Square,
  Settings, LayoutDashboard, Boxes, Trash2,
} from "lucide-react";
import { api } from "../lib/api";
import { Modal } from "../components/Modal";
import { useToast } from "../lib/toast";
import { useTabIndicator } from "../lib/useTabIndicator";
import type {
  DeploymentItem, DeploymentStatus, DomainItem, EnvironmentInfo,
  ProjectDetailData, ProjectWorkflowRun,
} from "../lib/types";

export const BUSY: DeploymentStatus[] = ["queued", "cloning", "dissecting", "building", "starting"];

// Backend timestamps are naive UTC — tag them so Date() doesn't read them as local.
export const utcDate = (iso: string) => new Date(/Z$|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + "Z");

type Section = "overview" | "deployments" | "services" | "settings";
const SECTIONS: { key: Section; label: string; icon: typeof Settings }[] = [
  { key: "overview", label: "Overview", icon: LayoutDashboard },
  { key: "deployments", label: "Deployments", icon: Rocket },
  { key: "services", label: "Services", icon: Boxes },
  { key: "settings", label: "Settings", icon: Settings },
];

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
      : <span className="badge success">Live</span>;
  }
  if (status && BUSY.includes(status as DeploymentStatus)) {
    return <span className="badge info">Deploying…</span>;
  }
  if (status === "failed") return <span className="badge fail">Failed</span>;
  if (status === "blocked") return <span className="badge warn">Blocked</span>;
  if (status === "pending_checks") return <span className="badge info">Waiting for checks…</span>;
  if (status === "pending_promotion") return <span className="badge info">Waiting for source env…</span>;
  if (status === "pending_e2e") return <span className="badge info">Running e2e…</span>;
  if (status === "stopped") return <span className="badge muted">Stopped</span>;
  // Superseded = it succeeded and was later replaced by a newer deploy.
  if (status === "superseded") return <span className="badge muted plain">Succeeded</span>;
  if (!status) return <span className="badge muted plain">Not deployed</span>;
  return <span className="badge info">{status[0].toUpperCase() + status.slice(1)}…</span>;
}

/** History badge (deployments list/detail): how did this deploy end? */
export function historyBadge(status: string) {
  // Green only while this deploy is the live one; once a newer deploy
  // replaces it (superseded) it still succeeded — render it grey.
  if (status === "running") return <span className="badge success">Succeeded</span>;
  if (status === "superseded") return <span className="badge muted plain">Succeeded</span>;
  if (status === "failed") return <span className="badge fail">Failed</span>;
  if (status === "blocked") return <span className="badge warn">Blocked</span>;
  if (status === "pending_checks") return <span className="badge info">Waiting for checks…</span>;
  if (status === "pending_promotion") return <span className="badge info">Waiting for source env…</span>;
  if (status === "pending_e2e") return <span className="badge info">Running e2e…</span>;
  if (status === "stopped") return <span className="badge muted">Stopped</span>;
  return <span className="badge info">{status[0].toUpperCase() + status.slice(1)}…</span>;
}

function serviceEnvBadge(env: EnvironmentInfo, serviceName: string) {
  const inst = env.instances.find(i => i.service_name === serviceName);
  if (!inst) return <span key={env.id} className="badge muted plain" style={{ textTransform: "capitalize" }}>{env.name}</span>;
  if (inst.status === "unreachable") {
    return <span key={env.id} className="badge fail" style={{ textTransform: "capitalize" }} title="Not responding">{env.name}</span>;
  }
  return <span key={env.id} className="badge ok" style={{ textTransform: "capitalize" }}>{env.name}</span>;
}

export function envUnreachable(env: EnvironmentInfo): boolean {
  return env.deployment?.status === "running"
    && env.instances.some(i => i.url && i.status === "unreachable");
}

export function predictedHost(
  name: string, label: string, slugSuffix: string,
  domain: string | null, mode: "container" | "base" | null,
): string | null {
  if (!domain) return null;
  if (mode === "base") {
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
  const { projectId, section: sectionParam } = useParams();
  const id = Number(projectId);
  const qc = useQueryClient();
  const nav = useNavigate();
  const toast = useToast();
  const section: Section = SECTIONS.some(s => s.key === sectionParam) ? (sectionParam as Section) : "overview";
  const [searchParams] = useSearchParams();
  const [envTab, setEnvTab] = useState<number | null>(() => {
    const raw = searchParams.get("env");
    return raw ? Number(raw) : null;
  });

  function goToSection(key: Section) {
    const path = key === "overview" ? `/projects/${id}` : `/projects/${id}/${key}`;
    nav({ pathname: path, search: searchParams.toString() });
  }

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
  const refreshRuns = useMutation({
    mutationFn: () => api.post(`/api/workflows/refresh`),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["project-workflows", id] }); toast.show("Refreshed", "ok"); },
    onError: (e) => toast.show(String(e), "fail"),
  });

  const vtabsRef = useRef<HTMLDivElement>(null);
  useTabIndicator(vtabsRef, ".vtab.active", [section]);
  const envTabsRef = useRef<HTMLDivElement>(null);
  useTabIndicator(envTabsRef, ".tab.active", [section, envTab, project?.environments?.length]);

  if (isError) return <Navigate to="/projects" replace />;
  if (sectionParam && !SECTIONS.some(s => s.key === sectionParam)) {
    return <Navigate to={{ pathname: `/projects/${id}`, search: searchParams.toString() }} replace />;
  }
  if (!project) return <span className="spinner" />;
  if (!project.managed) return <Navigate to="/projects" replace />;

  return (
    <>
      <Link to="/projects" className="dim" style={{ display: "inline-flex", alignItems: "center", gap: "0.3rem" }}>
        <ArrowLeft size={14} /> Projects
      </Link>

      <h1 style={{ margin: "0.5rem 0 0" }}>{project.name}</h1>
      <p className="dim" style={{ marginTop: "0.25rem" }}>
        {project.services.length} service{project.services.length === 1 ? "" : "s"} · {project.environments.length} environment{project.environments.length === 1 ? "" : "s"}
      </p>

      <div className="project-layout">
        {/* ─── Section nav (vertical tabs) ─────────────────────── */}
        <div className="vtabs" role="tablist" aria-orientation="vertical" ref={vtabsRef}>
          <span className="tab-indicator" aria-hidden />
          {SECTIONS.map(s => {
            const Icon = s.icon;
            return (
              <button
                key={s.key}
                role="tab"
                aria-selected={section === s.key}
                className={`vtab ${section === s.key ? "active" : ""}`}
                onClick={() => goToSection(s.key)}
              >
                <Icon size={16} aria-hidden /> {s.label}
              </button>
            );
          })}
        </div>

        <div className="project-panel">
          {/* ─── Overview / Deployments / Services (one horizontal tab per env) ── */}
          {(section === "overview" || section === "deployments" || section === "services") && (() => {
            const activeEnv = project.environments.find(e => e.id === envTab) ?? project.environments[0];
            if (!activeEnv) {
              return <div className="card"><span className="dim">No environments yet.</span></div>;
            }
            return (
              <>
                <div className="tabs" role="tablist" ref={envTabsRef}>
                  <span className="tab-indicator" aria-hidden />
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
                {section === "overview" && (
                  <EnvironmentCard key={activeEnv.id} projectId={id} repoFullName={project.repo_full_name} env={activeEnv} onChange={invalidate} />
                )}
                {section === "deployments" && (
                  <>
                    <Deployments key={activeEnv.id} projectId={id} envId={activeEnv.id} />
                    <WorkflowRuns runs={runs} refreshRuns={refreshRuns} />
                  </>
                )}
                {section === "services" && (
                  project.services.length === 0 ? (
                    <div className="card">
                      <span className="dim">No services detected yet. Click <strong>Sync</strong> to dissect the repo.</span>
                    </div>
                  ) : (
                    <table className="data-table">
                      <thead><tr><th>Service</th><th>Kind</th><th>Exposure</th><th>Environments</th><th>Hostname</th><th className="right" /></tr></thead>
                      <tbody>
                        {project.services.map(s => {
                          const host = s.is_public
                            ? predictedHost(project.name, s.subdomain_label, activeEnv.slug_suffix, project.domain, project.domain_mode)
                            : null;
                          return (
                            <tr key={s.id} className="clickable" onClick={() => nav(`/projects/${id}/services/${s.id}`)}>
                              <td><strong>{s.name}</strong>{s.internal_port && <span className="dim"> :{s.internal_port}</span>}</td>
                              <td><span className="badge plain">{s.kind}</span></td>
                              <td>{s.is_public ? <span className="badge ok">Public</span> : <span className="badge muted plain">Internal</span>}</td>
                              <td>
                                <div className="row" style={{ gap: "0.35rem", flexWrap: "wrap" }}>
                                  {project.environments.map(env => serviceEnvBadge(env, s.name))}
                                </div>
                              </td>
                              <td className="dim">{host ? <code>{host}</code> : "—"}</td>
                              <td className="actions"><ChevronRight size={15} className="dim" aria-hidden /></td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  )
                )}
              </>
            );
          })()}

          {/* ─── Settings ────────────────────────────────────────── */}
          {section === "settings" && <SettingsPanel project={project} onSaved={invalidate} />}
        </div>
      </div>
    </>
  );
}

function EnvironmentCard({ projectId, repoFullName, env, onChange }: {
  projectId: number; repoFullName: string; env: EnvironmentInfo; onChange: () => void;
}) {
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
        <strong style={{ textTransform: "capitalize" }}>{env.name}</strong>
        <span className="spacer" />
        {depBadge(dep?.status, envUnreachable(env))}
        <EnvActionsMenu
          canStop={!!dep && dep.status !== "stopped"}
          deployDisabled={deploy.isPending || busy}
          stopDisabled={stop.isPending}
          onDeploy={() => deploy.mutate()}
          onStop={() => stop.mutate()}
        />
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
      {dep?.commit_sha && (
        <div className="dim" style={{ marginTop: "0.4rem" }}>
          commit{" "}
          <a href={`https://github.com/${repoFullName}/commit/${dep.commit_sha}`} target="_blank" rel="noopener">
            <code>{dep.commit_sha.slice(0, 7)}</code>
          </a>
        </div>
      )}
    </div>
  );
}

/** Deploy/Stop actions collapsed behind a kebab menu in the card header. */
function EnvActionsMenu({ canStop, deployDisabled, stopDisabled, onDeploy, onStop }: {
  canStop: boolean;
  deployDisabled: boolean;
  stopDisabled: boolean;
  onDeploy: () => void;
  onStop: () => void;
}) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

  return (
    <div className="menu-wrap" ref={wrapRef}>
      <button className="icon-btn" aria-label="Environment actions" aria-expanded={open}
        onClick={() => setOpen(o => !o)}>
        <MoreVertical size={15} />
      </button>
      {open && (
        <div className="menu" role="menu">
          <button className="menu-item" role="menuitem" disabled={deployDisabled}
            onClick={() => { setOpen(false); onDeploy(); }}>
            <Rocket size={13} /> Deploy
          </button>
          {canStop && (
            <button className="menu-item" role="menuitem" disabled={stopDisabled}
              onClick={() => { setOpen(false); onStop(); }}>
              <Square size={13} /> Stop
            </button>
          )}
        </div>
      )}
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
  if (deps.length === 0) {
    return <div className="card" style={{ marginTop: "0.75rem" }}><span className="dim">No deployments yet for this environment.</span></div>;
  }

  return (
    <table className="data-table" style={{ margin: "0.75rem 0 0" }}>
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
  );
}

function WorkflowRuns({ runs, refreshRuns }: {
  runs: ProjectWorkflowRun[] | undefined;
  refreshRuns: UseMutationResult<unknown, unknown, void, unknown>;
}) {
  return (
    <>
      <div className="row" style={{ marginTop: "1.75rem" }}>
        <h3 style={{ margin: 0 }}>GitHub Actions</h3>
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

// ─── Settings panel (name + domain) ───────────────────────────────────────────
function SettingsPanel({ project, onSaved }: { project: ProjectDetailData; onSaved: () => void }) {
  const qc = useQueryClient();
  const nav = useNavigate();
  const toast = useToast();
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [name, setName] = useState(project.name);
  const [domainId, setDomainId] = useState<string>(project.domain_id ? String(project.domain_id) : "");
  const [requireChecks, setRequireChecks] = useState(project.require_checks);
  // "By environment" reveals the per-env override rows below each toggle;
  // saving while collapsed clears any stale overrides back to the default.
  const [domainScope, setDomainScope] = useState<"single" | "by_env">(
    () => project.environments.some(e => e.domain_id) ? "by_env" : "single"
  );
  // "Manual only" folds the old auto-deploy switch into this select: pushes
  // are ignored entirely (auto_deploy=false). Per-env promotion settings are
  // left untouched while manual — they're inert without auto-deploy.
  const [deployMode, setDeployMode] = useState<"manual" | "simple" | "by_env">(
    () => !project.auto_deploy ? "manual"
      : project.environments.some(e => e.promotion_gate) ? "by_env" : "simple"
  );
  // How this project's hostnames are shaped (see app/urls.py) — a
  // project-level setting, not tied to whichever domain it's assigned to.
  const [domainMode, setDomainMode] = useState<"container" | "base">(
    project.domain_mode ?? "container"
  );
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
  const effectiveEnvDomainObj = (envId: number) =>
    envDomains[envId] ? resolveDomain(envDomains[envId]) : resolveDomain(domainId);

  const save = useMutation({
    mutationFn: async () => {
      await api.patch(`/api/projects/${project.id}`, {
        name, domain_id: domainId ? Number(domainId) : 0, domain_mode: domainMode,
        auto_deploy: deployMode !== "manual", require_checks: requireChecks,
      });
      for (const env of project.environments) {
        const body: Record<string, unknown> = {};
        const chosen = domainScope === "by_env" ? (envDomains[env.id] ?? "") : "";
        if (chosen !== (env.domain_id ? String(env.domain_id) : "")) body.domain_id = chosen ? Number(chosen) : 0;
        const gate = deployMode === "by_env" ? (envGates[env.id] ?? false)
          : deployMode === "simple" ? false : env.promotion_gate;
        if (gate !== env.promotion_gate) body.promotion_gate = gate;
        const e2e = deployMode === "by_env" ? (envE2e[env.id] ?? "").trim()
          : deployMode === "simple" ? "" : (env.e2e_workflow ?? "");
        if (e2e !== (env.e2e_workflow ?? "")) body.e2e_workflow = e2e;
        if (Object.keys(body).length > 0) {
          await api.patch(`/api/projects/${project.id}/environments/${env.id}`, body);
        }
      }
    },
    onSuccess: () => { toast.show("Saved — redeploy to apply hostname changes", "ok"); onSaved(); },
    onError: (e) => toast.show(String(e), "fail"),
  });

  const remove = useMutation({
    mutationFn: () => api.post(`/api/projects/${project.id}/release`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      toast.show(`Removed ${project.name} — Homebox resources torn down`, "ok");
      nav("/projects", { replace: true });
    },
    onError: (e) => toast.show(String(e), "fail"),
  });

  const sync = useMutation({
    mutationFn: () => api.post(`/api/projects/${project.id}/sync`),
    onSuccess: () => { onSaved(); toast.show("Re-dissected services", "ok"); },
    onError: (e) => toast.show(String(e), "fail"),
  });

  return (
    <>
    <div className="card">
      <div className="field">
        <label className="lbl">Repository</label>
        <div className="row" style={{ gap: "0.5rem" }}>
          <a
            href={`https://github.com/${project.repo_full_name}`} target="_blank" rel="noopener"
            style={{ flex: 1, display: "flex", alignItems: "center", gap: "0.4rem" }}
          >
            <code>{project.repo_full_name}</code> <ExternalLink size={12} />
          </a>
          <button className="btn small" disabled={sync.isPending} onClick={() => sync.mutate()} title="Re-read repo and refresh services">
            {sync.isPending ? <span className="spinner" /> : <RefreshCw size={12} />} Sync
          </button>
        </div>
      </div>
      <div className="field">
        <label className="lbl">Project name (URL slug)</label>
        <input value={name} onChange={e => setName(e.target.value)} placeholder={project.name} />
        <span className="hint">
          {domainMode === "base"
            ? <>Base domain — the app lives at <code>{projectDomain}</code> (name is used for stacks and wildcard fallbacks).</>
            : <>Used as the hostname base, e.g. <code>{predictedHost(name || project.name, "", "", projectDomain, domainMode)}</code>.</>}
        </span>
      </div>
      <div className="field">
        <label className="lbl">Domain</label>
        <select
          value={domainScope === "by_env" ? "__by_env" : domainId}
          onChange={e => {
            if (e.target.value === "__by_env") setDomainScope("by_env");
            else { setDomainScope("single"); setDomainId(e.target.value); }
          }}
        >
          <option value="">Primary (default)</option>
          {(domains ?? []).map(d => <option key={d.id} value={d.id}>{d.name}{d.is_primary ? " (primary)" : ""}</option>)}
          <option value="__by_env">By environment</option>
        </select>
      </div>

      {domainScope === "by_env" && (
        <>
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
                  effectiveEnvDomainObj(env.id)?.name ?? "…", domainMode)}
              </code>
            </div>
          ))}
        </>
      )}

      <div className="field" style={{ marginTop: "0.85rem" }}>
        <label className="lbl">Domain type</label>
        <select value={domainMode} onChange={e => setDomainMode(e.target.value as "container" | "base")}>
          <option value="container">Container</option>
          <option value="base">Base</option>
        </select>
        <span className="hint">
          {domainMode === "base"
            ? <>This project owns the domain outright. Production lives at the root, other public services path-proxied (e.g. <code>/api</code>), dev at <code>dev.&lt;domain&gt;</code>.</>
            : <>Every environment gets a name-prefixed subdomain, so multiple projects can share one domain.</>}
        </span>
      </div>

      <div className="field" style={{ marginTop: "0.85rem" }}>
        <label className="lbl">Deploy pipeline</label>
        <select
          value={deployMode}
          onChange={e => setDeployMode(e.target.value as "manual" | "simple" | "by_env")}
        >
          <option value="manual">Manual only</option>
          <option value="simple">Deploy on push</option>
          <option value="by_env">By environment</option>
        </select>
      </div>

      {deployMode === "by_env" && project.environments.map(env => {
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

      <label className="row" style={{ cursor: "pointer", gap: "0.4rem", marginTop: "0.85rem" }}>
        <input type="checkbox" checked={requireChecks} onChange={e => setRequireChecks(e.target.checked)} disabled={deployMode === "manual"} />
        Wait for GitHub checks to pass before deploying
      </label>

      <div className="row" style={{ marginTop: "1.25rem", paddingTop: "1rem", borderTop: "1px solid var(--border)" }}>
        <span className="spacer" />
        <button className="btn primary" disabled={save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? <span className="spinner" /> : "Save changes"}
        </button>
      </div>
    </div>

    <div className="card" style={{ marginTop: "1.25rem", borderColor: "var(--danger)" }}>
      <strong>Danger zone</strong>
      <p className="dim" style={{ marginTop: "0.35rem" }}>
        Stops and removes this project's containers on every environment. The GitHub
        repository is untouched and can be re-added later.
      </p>
      <button className="btn danger" onClick={() => setConfirmRemove(true)}>
        <Trash2 size={14} /> Remove project
      </button>
    </div>

    <Modal
      open={confirmRemove}
      onClose={() => { if (!remove.isPending) setConfirmRemove(false); }}
      title={`Remove ${project.name}?`}
      footer={<>
        <span className="spacer" />
        <button className="btn ghost" onClick={() => setConfirmRemove(false)} disabled={remove.isPending}>Cancel</button>
        <button className="btn danger" disabled={remove.isPending} onClick={() => remove.mutate()}>
          {remove.isPending ? <span className="spinner" /> : <><Trash2 size={14} /> Remove</>}
        </button>
      </>}
    >
      <p style={{ margin: 0 }}>
        Tears down every environment's containers and networks for <strong>{project.name}</strong>.
      </p>
      <p className="dim">
        The GitHub repository ({project.repo_full_name}) is not deleted or modified — you can
        add this project again later. Data volumes are kept.
      </p>
    </Modal>
    </>
  );
}
