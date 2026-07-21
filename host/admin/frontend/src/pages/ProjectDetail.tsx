import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Link, Navigate, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft, ChevronRight, ExternalLink, MoreVertical, RefreshCw, Rocket, Square,
  Settings, LayoutDashboard, Boxes, Trash2,
} from "lucide-react";
import { api } from "../lib/api";
import { HeaderSave, HeaderSaveButton, useHeaderSave } from "../lib/headerSave";
import { DeploymentPanel } from "./DeploymentDetail";
import { RuntimeLogsPanel } from "./RuntimeLogs";
import { ServicePanel } from "./ServiceDetail";
import { Modal } from "../components/Modal";
import { ProjectIconPicker } from "../components/ProjectIconPicker";
import { ServiceIcon, ServiceStatus } from "../components/ServiceIcon";
import { useToast } from "../lib/toast";
import { timeAgo } from "../lib/time";
import { useTabIndicator } from "../lib/useTabIndicator";
import type {
  DeploymentItem, DeploymentStatus, DomainItem, EnvironmentInfo,
  ProjectDetailData,
} from "../lib/types";

export const BUSY: DeploymentStatus[] = ["queued", "cloning", "dissecting", "building", "starting"];

// Backend timestamps are naive UTC — tag them so Date() doesn't read them as local.
export const utcDate = (iso: string) => new Date(/Z$|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + "Z");

type Section = "overview" | "deployments" | "services" | "settings";
interface ProjectTargetOptions {
  options: { value: string; label: string; locations?: { kind: "local" | "cluster" | "node"; id: string | null; name: string; local: boolean }[] }[];
  integrations: { id: number; provider: string; label: string }[];
}
const SECTIONS: { key: Section; label: string; icon: typeof Settings }[] = [
  { key: "overview", label: "Overview", icon: LayoutDashboard },
  { key: "services", label: "Services", icon: Boxes },
  { key: "deployments", label: "Deploys", icon: Rocket },
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
  const { projectId, section: sectionParam, deploymentId, serviceId } = useParams();
  const id = Number(projectId);
  const qc = useQueryClient();
  const nav = useNavigate();
  // Deployment- and service-detail URLs render inside the same chrome with
  // their section active; the panel swaps the list for the detail. /logs is a
  // pseudo-section under Overview: runtime container logs for the active env,
  // reached by clicking an environment's status badge.
  const logsView = sectionParam === "logs";
  const section: Section = deploymentId ? "deployments"
    : serviceId ? "services"
    : logsView ? "overview"
    : SECTIONS.some(s => s.key === sectionParam) ? (sectionParam as Section) : "overview";
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
  const invalidate = () => qc.invalidateQueries({ queryKey: ["project", id] });

  const vtabsRef = useRef<HTMLDivElement>(null);
  useTabIndicator(vtabsRef, ".vtab.active", [section]);
  const envTabsRef = useRef<HTMLDivElement>(null);
  useTabIndicator(envTabsRef, ".tab.active", [section, envTab, project?.environments?.length]);

  // Settings' conditional Save lives in the page header row; the panel
  // reports its dirty/saving state up while mounted (settings section only).
  const [settingsSave, setSettingsSave] = useState<HeaderSave | null>(null);

  if (isError) return <Navigate to="/projects" replace />;
  if (sectionParam && !logsView && !SECTIONS.some(s => s.key === sectionParam)) {
    return <Navigate to={{ pathname: `/projects/${id}`, search: searchParams.toString() }} replace />;
  }
  if (!project) return <span className="spinner" />;
  if (!project.managed) return <Navigate to="/projects" replace />;

  return (
    <>
      <div className="row">
        <Link to="/projects" className="back-btn" aria-label="Back to projects" title="Back to projects">
          <ArrowLeft size={18} />
        </Link>
        <ProjectIconPicker projectId={project.id} icon={project.icon} name={project.name} />
        <h1 style={{ margin: 0 }}>{project.name}</h1>
        <span className="spacer" />
        <HeaderSaveButton state={settingsSave} />
      </div>
      <p className="dim" style={{ marginTop: "0.25rem" }}>
        {project.services.length} service{project.services.length === 1 ? "" : "s"} · {project.environments.length} environment{project.environments.length === 1 ? "" : "s"}
      </p>

      <div className="project-layout">
        {/* ─── Section nav (vertical tabs; hidden on mobile) ─────── */}
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

        {/* Mobile: the same nav as a bottom bar, portaled to <body> so no
            app container (transform/overflow/stacking) can trap or bury the
            fixed positioning. Shown/hidden purely via CSS breakpoints. */}
        {createPortal(
          <nav className="vtabs-mobile" role="tablist">
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
                  <Icon size={17} aria-hidden />
                  <span>{s.label}</span>
                </button>
              );
            })}
          </nav>,
          document.body,
        )}

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
                      onClick={() => {
                        setEnvTab(env.id);
                        // From a deployment detail, an env tab returns to that
                        // env's deployments list.
                        if (deploymentId) nav(`/projects/${id}/deployments?env=${env.id}`);
                      }}
                    >
                      <span className="tab-dot" style={{ background: envUnreachable(env) ? "var(--danger)" : envDotColor(env.deployment?.status) }} />
                      <span style={{ textTransform: "capitalize" }}>{env.name}</span>
                    </button>
                  ))}
                  {section === "deployments" && (
                    <DeployEnvironmentControl
                      projectId={id}
                      env={activeEnv}
                      onChange={invalidate}
                    />
                  )}
                </div>
                {section === "overview" && (logsView ? (
                  <RuntimeLogsPanel key={activeEnv.id} projectId={id} env={activeEnv} />
                ) : (
                  <EnvironmentCard key={activeEnv.id} projectId={id} repoFullName={project.repo_full_name} env={activeEnv} onChange={invalidate} />
                ))}
                {section === "deployments" && (deploymentId ? (
                  <DeploymentPanel
                    projectId={id}
                    deploymentId={Number(deploymentId)}
                    onEnv={eid => setEnvTab(prev => (prev === eid ? prev : eid))}
                  />
                ) : <Deployments key={activeEnv.id} projectId={id} envId={activeEnv.id} />)}
                {section === "services" && serviceId && (() => {
                  const svc = project.services.find(s => s.id === Number(serviceId));
                  if (!svc) return <Navigate to={`/projects/${id}/services`} replace />;
                  return <ServicePanel projectId={id} svc={svc} env={activeEnv} />;
                })()}
                {section === "services" && !serviceId && (
                  project.services.length === 0 ? (
                    <div className="card">
                      <span className="dim">No services detected yet. Click <strong>Sync</strong> to dissect the repo.</span>
                    </div>
                  ) : (
                    <ServicesTable project={project} env={activeEnv} />
                  )
                )}
              </>
            );
          })()}

          {/* ─── Settings ────────────────────────────────────────── */}
          {section === "settings" && <SettingsPanel project={project} onSaved={invalidate} onStatus={setSettingsSave} />}
        </div>
      </div>
    </>
  );
}

function ServicesTable({ project, env }: { project: ProjectDetailData; env: EnvironmentInfo }) {
  const nav = useNavigate();
  const { data: runtime } = useQuery<{ containers: { service: string; state: string }[] }>({
    queryKey: ["environment-runtime", project.id, env.id],
    queryFn: () => api.get(`/api/projects/${project.id}/environments/${env.id}/runtime-logs`),
    refetchInterval: 6000,
    retry: false,
  });
  const liveState = new Map((runtime?.containers ?? []).map(container => [container.service, container.state]));

  return (
    <table className="data-table">
      <thead><tr><th>Service</th><th>Exposure</th><th>Status</th><th>Hostname</th><th className="right" /></tr></thead>
      <tbody>
        {project.services.map(service => {
          const host = service.is_public
            ? predictedHost(project.name, service.subdomain_label, env.slug_suffix, project.domain, project.domain_mode)
            : null;
          const persisted = env.instances.find(instance => instance.service_name === service.name)?.status;
          return (
            <tr key={service.id} className="clickable" onClick={() => nav(`/projects/${project.id}/services/${service.id}`)}>
              <td><span className="row" style={{ flexWrap: "nowrap" }}><ServiceIcon kind={service.kind} /><strong>{service.name}</strong>{service.internal_port && <span className="dim"> :{service.internal_port}</span>}</span></td>
              <td>{service.is_public ? <span className="badge ok">Public</span> : <span className="badge muted plain">Internal</span>}</td>
              <td><ServiceStatus status={liveState.get(service.name) ?? persisted} /></td>
              <td className="dim">{host ? <code>{host}</code> : "—"}</td>
              <td className="actions"><ChevronRight size={15} className="dim" aria-hidden /></td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function deploymentState(env: EnvironmentInfo): string {
  const status = env.deployment?.status;
  if (status === "running") return envUnreachable(env) ? "Down" : "Running";
  if (status === "stopped") return "Paused";
  if (status && (BUSY.includes(status) || status.startsWith("pending"))) return "Deploying";
  if (status === "failed" || status === "blocked") return "Failed";
  return "Not deployed";
}

function DeployEnvironmentControl({ projectId, env, onChange }: {
  projectId: number; env: EnvironmentInfo; onChange: () => void;
}) {
  const toast = useToast();
  const busy = !!env.deployment && (BUSY.includes(env.deployment.status) || env.deployment.status.startsWith("pending"));
  const deploy = useMutation({
    mutationFn: () => api.post(`/api/projects/${projectId}/environments/${env.id}/deploy`),
    onSuccess: () => { onChange(); toast.show(`Deploying ${env.name}`, "ok"); },
    onError: e => toast.show(String(e), "fail"),
  });
  const state = deploymentState(env);
  const stateClass = state === "Running" ? "success"
    : state === "Deploying" ? "info"
    : state === "Failed" || state === "Down" ? "fail"
    : "muted";
  return (
    <div className="row deploy-tab-action">
      <span className={`badge ${stateClass}`}>{state}</span>
      <button className="btn small primary" disabled={busy || deploy.isPending} onClick={() => deploy.mutate()}>
        {deploy.isPending ? <span className="spinner" /> : <Rocket size={13} />} Deploy
      </button>
    </div>
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
        <Link
          to={`/projects/${projectId}/logs?env=${env.id}`}
          style={{ textDecoration: "none", display: "inline-flex" }}
          title="See what's actually running — live container state and logs"
        >
          {depBadge(dep?.status, envUnreachable(env))}
        </Link>
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
            <td className="dim" title={d.created_at ? utcDate(d.created_at).toLocaleString() : undefined}>
              {timeAgo(d.created_at) ?? "—"}
            </td>
            <td className="actions"><ChevronRight size={15} className="dim" aria-hidden /></td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// ─── Settings panel (name + domain) ───────────────────────────────────────────

/** The select-encoded deployment target the server currently holds. */
function projectTargetValue(project: ProjectDetailData): string {
  if (project.deployment_target !== "homebox") return project.deployment_target;
  if (project.deployment_target_config.cluster_id) return `homebox:cluster:${project.deployment_target_config.cluster_id}`;
  if (project.deployment_target_config.node_id) return `homebox:node:${project.deployment_target_config.node_id}`;
  return "homebox";
}

function SettingsPanel({ project, onSaved, onStatus }: {
  project: ProjectDetailData;
  onSaved: () => void;
  onStatus: (s: HeaderSave | null) => void;
}) {
  const qc = useQueryClient();
  const nav = useNavigate();
  const toast = useToast();
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [name, setName] = useState(project.name);
  const serverTarget = projectTargetValue(project);
  const [targetValue, setTargetValue] = useState(serverTarget);
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
  const { data: targetOptions } = useQuery<ProjectTargetOptions>({
    queryKey: ["project-target-options", project.id],
    queryFn: () => api.get<ProjectTargetOptions>(`/api/projects/${project.id}/target-options`),
  });

  // ── Dirty tracking against server truth ──────────────────────────────
  // Effective values exactly as the save flow would persist them; the header
  // Save renders only when at least one differs from what the server holds,
  // so reverting an edit hides the button again.
  const serverDomainId = project.domain_id ? String(project.domain_id) : "";
  const effectiveEnvDomain = (envId: number): string =>
    domainScope === "by_env" ? (envDomains[envId] ?? "") : "";
  const effectiveGate = (env: EnvironmentInfo): boolean =>
    deployMode === "by_env" ? (envGates[env.id] ?? false)
      : deployMode === "simple" ? false : env.promotion_gate;
  const effectiveE2e = (env: EnvironmentInfo): string =>
    deployMode === "by_env" ? (envE2e[env.id] ?? "").trim()
      : deployMode === "simple" ? "" : (env.e2e_workflow ?? "");
  const dirty =
    name !== project.name
    || targetValue !== serverTarget
    || domainId !== serverDomainId
    || domainMode !== (project.domain_mode ?? "container")
    || (deployMode !== "manual") !== project.auto_deploy
    || requireChecks !== project.require_checks
    || project.environments.some(env =>
      effectiveEnvDomain(env.id) !== (env.domain_id ? String(env.domain_id) : "")
      || effectiveGate(env) !== env.promotion_gate
      || effectiveE2e(env) !== (env.e2e_workflow ?? ""));

  // The project query polls every 6s. When it brings genuinely new server
  // state and there are no unsaved edits, reseed the form; while dirty, the
  // user's staged edits are never clobbered by a background refetch.
  const serverSer = JSON.stringify([
    project.name, serverTarget, serverDomainId, project.domain_mode,
    project.auto_deploy, project.require_checks,
    project.environments.map(e => [e.id, e.domain_id, e.promotion_gate, e.e2e_workflow]),
  ]);
  useEffect(() => {
    if (dirty) return;
    setName(project.name);
    setTargetValue(serverTarget);
    setDomainId(serverDomainId);
    setRequireChecks(project.require_checks);
    setDomainScope(project.environments.some(e => e.domain_id) ? "by_env" : "single");
    setDeployMode(!project.auto_deploy ? "manual"
      : project.environments.some(e => e.promotion_gate) ? "by_env" : "simple");
    setDomainMode(project.domain_mode ?? "container");
    setEnvDomains(Object.fromEntries(project.environments.map(e => [e.id, e.domain_id ? String(e.domain_id) : ""])));
    setEnvGates(Object.fromEntries(project.environments.map(e => [e.id, e.promotion_gate])));
    setEnvE2e(Object.fromEntries(project.environments.map(e => [e.id, e.e2e_workflow ?? ""])));
    // Deps are server truth only: an edit flipping `dirty` must not reseed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverSer]);

  const resolveDomain = (id: string): DomainItem | undefined =>
    id ? (domains ?? []).find(d => d.id === Number(id)) : (domains ?? []).find(d => d.is_primary);
  const domainName = (id: string): string => resolveDomain(id)?.name ?? "…";
  const projectDomain = domainName(domainId);
  const effectiveEnvDomainObj = (envId: number) =>
    envDomains[envId] ? resolveDomain(envDomains[envId]) : resolveDomain(domainId);

  const save = useMutation({
    mutationFn: async () => {
      const projectPatch: Record<string, unknown> = {
        name, domain_id: domainId ? Number(domainId) : 0, domain_mode: domainMode,
        auto_deploy: deployMode !== "manual", require_checks: requireChecks,
      };
      if (targetValue !== serverTarget) {
        const [target, locationKind, ...locationParts] = targetValue.split(":");
        const locationId = locationParts.join(":");
        projectPatch.deployment_target = target;
        projectPatch.deployment_target_config = locationKind && locationId
          ? { [`${locationKind}_id`]: locationId }
          : {};
        projectPatch.deployment_target_integration_id =
          targetOptions?.integrations.find(i => i.provider === target)?.id ?? null;
      }
      await api.patch(`/api/projects/${project.id}`, projectPatch);
      for (const env of project.environments) {
        const body: Record<string, unknown> = {};
        const chosen = effectiveEnvDomain(env.id);
        if (chosen !== (env.domain_id ? String(env.domain_id) : "")) body.domain_id = chosen ? Number(chosen) : 0;
        const gate = effectiveGate(env);
        if (gate !== env.promotion_gate) body.promotion_gate = gate;
        const e2e = effectiveE2e(env);
        if (e2e !== (env.e2e_workflow ?? "")) body.e2e_workflow = e2e;
        if (Object.keys(body).length > 0) {
          await api.patch(`/api/projects/${project.id}/environments/${env.id}`, body);
        }
      }
    },
    onSuccess: () => { toast.show("Saved — redeploy to apply hostname changes", "ok"); onSaved(); },
    onError: (e) => toast.show(String(e), "fail"),
  });

  useHeaderSave(onStatus, dirty, save.isPending, () => save.mutate());

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
        <label className="lbl">Project name</label>
        <input value={name} onChange={e => setName(e.target.value)} placeholder={project.name} />
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
          {/* Show where the app will actually land per environment for the
              selected mode, e.g. "production: listless.app, dev: dev.listless.app". */}
          {project.environments.map((env, i) => (
            <span key={env.id}>
              {i > 0 && ", "}
              {env.name}:{" "}
              <code>
                {predictedHost(name || project.name, "", env.slug_suffix,
                  effectiveEnvDomainObj(env.id)?.name ?? projectDomain, domainMode)}
              </code>
            </span>
          ))}
        </span>
      </div>

      <div className="field" style={{ marginTop: "0.85rem" }}>
        <label className="lbl">Deployment target</label>
        <select value={targetValue} onChange={e => setTargetValue(e.target.value)}>
          <option value="automatic">Automatic</option>
          <optgroup label="Homebox">
            {(targetOptions?.options.find(o => o.value === "homebox")?.locations ?? [{ kind: "local", id: null, name: "This Homebox", local: true }]).map(location => {
              const value = location.kind === "local" || location.id === null
                ? "homebox"
                : `homebox:${location.kind}:${location.id}`;
              return <option key={value} value={value}>{location.name}{location.local && location.kind !== "local" ? ` (this ${location.kind})` : ""}</option>;
            })}
          </optgroup>
          <optgroup label="Cloud">
            {(targetOptions?.options ?? []).filter(o => ["cloudflare", "aws", "gcp"].includes(o.value)).map(option => {
              const connected = targetOptions?.integrations.some(i => i.provider === option.value);
              return <option key={option.value} value={option.value} disabled={!connected}>
                {option.label}{connected ? "" : " (connect first)"}
              </option>;
            })}
          </optgroup>
        </select>
        <span className="hint">
          Automatic keeps development on Homebox and uses connected, supported cloud targets for production. A service can override this default.
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

      {project.integration && (
        <label className="row" style={{ cursor: "pointer", gap: "0.4rem", marginTop: "0.85rem" }}>
          <input type="checkbox" checked={requireChecks} onChange={e => setRequireChecks(e.target.checked)} disabled={deployMode === "manual"} />
          Wait for GitHub checks to pass before deploying
        </label>
      )}
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
