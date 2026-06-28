import { useState } from "react";
import { Link, Navigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AreaChart, Area, LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from "recharts";
import { ArrowLeft, ExternalLink, RefreshCw, Rocket, Square, Settings, KeyRound } from "lucide-react";
import { api } from "../lib/api";
import { Modal } from "../components/Modal";
import { useToast } from "../lib/toast";
import type {
  DeploymentStatus, DomainItem, EnvironmentInfo, MetricsResponse, ProjectDetailData,
  ProjectWorkflowRun, ServiceItem,
} from "../lib/types";

const WINDOWS = ["1h", "6h", "24h", "7d"] as const;
type Win = (typeof WINDOWS)[number];
const BUSY: DeploymentStatus[] = ["queued", "cloning", "dissecting", "building", "starting"];

const fmtTime = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(0)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}

function depBadge(status: string | undefined) {
  if (status === "running") return <span className="badge ok">Running</span>;
  if (status === "failed") return <span className="badge fail">Failed</span>;
  if (status === "stopped") return <span className="badge muted">Stopped</span>;
  if (!status) return <span className="badge muted plain">Not deployed</span>;
  return <span className="badge info">{status[0].toUpperCase() + status.slice(1)}…</span>;
}

function predictedHost(name: string, label: string, slugSuffix: string, domain: string | null): string | null {
  if (!domain) return null;
  const base = label ? `${name}-${label}` : name;
  return `${base}${slugSuffix}.${domain}`;
}

export function ProjectDetail() {
  const { projectId } = useParams();
  const id = Number(projectId);
  const qc = useQueryClient();
  const toast = useToast();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [envVarsFor, setEnvVarsFor] = useState<ServiceItem | null>(null);

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

      {/* ─── Environments ───────────────────────────────────────── */}
      <h2 style={{ marginTop: "1.5rem" }}>Environments</h2>
      <div style={{ display: "grid", gap: "1rem", gridTemplateColumns: "repeat(auto-fit,minmax(320px,1fr))", marginTop: "0.5rem" }}>
        {project.environments.map(env => (
          <EnvironmentCard key={env.id} projectId={id} env={env} onChange={invalidate} />
        ))}
      </div>

      {/* ─── Services ───────────────────────────────────────────── */}
      <h2 style={{ marginTop: "2rem" }}>Services</h2>
      {project.services.length === 0 ? (
        <div className="card" style={{ marginTop: "0.5rem" }}>
          <span className="dim">No services detected yet. Click <strong>Sync</strong> to dissect the repo.</span>
        </div>
      ) : (
        <table className="data-table" style={{ marginTop: "0.5rem" }}>
          <thead><tr><th>Service</th><th>Kind</th><th>Exposure</th><th>Hostname (prod)</th><th>Env vars</th><th className="right" /></tr></thead>
          <tbody>
            {project.services.map(s => {
              const host = s.is_public ? predictedHost(project.name, s.subdomain_label, "", project.domain) : null;
              return (
                <tr key={s.id}>
                  <td><strong>{s.name}</strong>{s.internal_port && <span className="dim"> :{s.internal_port}</span>}</td>
                  <td><span className="badge plain">{s.kind}</span></td>
                  <td>{s.is_public ? <span className="badge ok">public</span> : <span className="badge muted plain">internal</span>}</td>
                  <td className="dim">{host ? <code>{host}</code> : "—"}</td>
                  <td className="dim">{s.env_vars.length}{s.env_vars.some(v => v.source === "auto") && <span className="badge info plain" style={{ marginLeft: 6 }}>auto</span>}</td>
                  <td className="actions">
                    <button className="btn small ghost" onClick={() => setEnvVarsFor(s)}><KeyRound size={12} /> Env</button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      {/* ─── Metrics ────────────────────────────────────────────── */}
      <Metrics services={project.services} />

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
      {envVarsFor && <EnvVarsModal service={envVarsFor} onClose={() => { setEnvVarsFor(null); invalidate(); }} />}
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
        {depBadge(dep?.status)}
        <strong style={{ textTransform: "capitalize" }}>{env.name}</strong>
        <span className="dim">{env.branch ? `branch ${env.branch}` : "default branch"}</span>
      </div>
      <div style={{ marginTop: "0.6rem", display: "flex", flexDirection: "column", gap: "0.3rem" }}>
        {env.instances.filter(i => i.url).length > 0
          ? env.instances.filter(i => i.url).map(i => (
              <div key={i.service_name} className="row" style={{ justifyContent: "space-between" }}>
                <span className="dim">{i.service_name}</span>
                <a href={i.url!} target="_blank" rel="noopener">{i.url!.replace("https://", "")} <ExternalLink size={11} /></a>
              </div>
            ))
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

function Metrics({ services }: { services: ServiceItem[] }) {
  const [serviceId, setServiceId] = useState<number | null>(services[0]?.id ?? null);
  const [win, setWin] = useState<Win>("1h");
  const sid = serviceId ?? services[0]?.id;

  const { data } = useQuery<MetricsResponse>({
    queryKey: ["service-metrics", sid, win],
    queryFn: () => api.get<MetricsResponse>(`/api/services/${sid}/metrics?window=${win}`),
    refetchInterval: 15000,
    enabled: !!sid,
  });
  if (services.length === 0) return null;
  const points = data?.points ?? [];

  return (
    <>
      <div className="row" style={{ marginTop: "2rem" }}>
        <h2 style={{ margin: 0 }}>Monitoring</h2>
        <div className="spacer" />
        <select value={sid} onChange={e => setServiceId(Number(e.target.value))} style={{ width: "auto" }}>
          {services.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
        </select>
        <div className="mode-chips">
          {WINDOWS.map(w => <span key={w} className={`chip ${win === w ? "active" : ""}`} onClick={() => setWin(w)}>{w}</span>)}
        </div>
      </div>
      {points.length === 0 ? (
        <div className="card" style={{ marginTop: "0.5rem" }}><span className="dim">No samples yet — metrics appear within a minute of a running deploy.</span></div>
      ) : (
        <div style={{ display: "grid", gap: "1rem", marginTop: "0.5rem" }}>
          <ChartCard title="CPU">
            <ResponsiveContainer width="100%" height={180}>
              <LineChart data={points}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                <XAxis dataKey="ts" tickFormatter={fmtTime} stroke="var(--muted)" fontSize={11} />
                <YAxis unit="%" stroke="var(--muted)" fontSize={11} />
                <Tooltip labelFormatter={(l: any) => fmtTime(l)} formatter={(v: any) => [`${v}%`, "CPU"]} />
                <Line type="monotone" dataKey="cpu_pct" stroke="var(--accent)" dot={false} isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          </ChartCard>
          <ChartCard title="Memory">
            <ResponsiveContainer width="100%" height={180}>
              <AreaChart data={points}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                <XAxis dataKey="ts" tickFormatter={fmtTime} stroke="var(--muted)" fontSize={11} />
                <YAxis tickFormatter={fmtBytes} stroke="var(--muted)" fontSize={11} width={70} />
                <Tooltip labelFormatter={(l: any) => fmtTime(l)} formatter={(v: any) => [fmtBytes(v), "Memory"]} />
                <Area type="monotone" dataKey="mem_used" stroke="var(--accent)" fill="var(--accent-glow)" isAnimationActive={false} />
              </AreaChart>
            </ResponsiveContainer>
          </ChartCard>
        </div>
      )}
    </>
  );
}

function ChartCard({ title, children }: { title: string; children: React.ReactNode }) {
  return <div className="card"><div className="dim" style={{ marginBottom: "0.5rem", fontWeight: 500 }}>{title}</div>{children}</div>;
}

function runBadge(status: string, conclusion: string | null) {
  if (status === "completed") {
    if (conclusion === "success") return <span className="badge ok">Success</span>;
    if (conclusion === "failure" || conclusion === "timed_out") return <span className="badge fail">{conclusion}</span>;
    if (conclusion === "cancelled") return <span className="badge warn">Cancelled</span>;
    return <span className="badge plain">{conclusion || "completed"}</span>;
  }
  if (status === "in_progress" || status === "queued") return <span className="badge info">{status}</span>;
  return <span className="badge plain">{status}</span>;
}

// ─── Settings modal (name + domain) ───────────────────────────────────────────
function SettingsModal({ project, onClose }: { project: ProjectDetailData; onClose: () => void }) {
  const toast = useToast();
  const [name, setName] = useState(project.name);
  const [domainId, setDomainId] = useState<string>(project.domain_id ? String(project.domain_id) : "");
  const [autoDeploy, setAutoDeploy] = useState(project.auto_deploy);

  const { data: domains } = useQuery<DomainItem[]>({ queryKey: ["domains"], queryFn: () => api.get<DomainItem[]>("/api/domains") });

  const save = useMutation({
    mutationFn: () => api.patch(`/api/projects/${project.id}`, {
      name, domain_id: domainId ? Number(domainId) : 0, auto_deploy: autoDeploy,
    }),
    onSuccess: () => { toast.show("Saved", "ok"); onClose(); },
    onError: (e) => toast.show(String(e), "fail"),
  });

  return (
    <Modal open onClose={onClose} title="Project settings" footer={<>
      <span className="spacer" />
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={save.isPending} onClick={() => save.mutate()}>
        {save.isPending ? <span className="spinner" /> : "Save"}
      </button>
    </>}>
      <div className="field">
        <label className="lbl">Project name (URL slug)</label>
        <input value={name} onChange={e => setName(e.target.value)} placeholder="box" />
        <span className="hint">Used as the hostname base, e.g. <code>{name || "box"}.example.dev</code>.</span>
      </div>
      <div className="field">
        <label className="lbl">Domain</label>
        <select value={domainId} onChange={e => setDomainId(e.target.value)}>
          <option value="">Primary (default)</option>
          {(domains ?? []).map(d => <option key={d.id} value={d.id}>{d.name}{d.is_primary ? " (primary)" : ""}</option>)}
        </select>
      </div>
      <label className="row" style={{ cursor: "pointer", gap: "0.4rem", marginTop: "0.5rem" }}>
        <input type="checkbox" checked={autoDeploy} onChange={e => setAutoDeploy(e.target.checked)} />
        Auto-deploy on push to the tracked branch
      </label>
    </Modal>
  );
}

// ─── Env var editor ───────────────────────────────────────────────────────────
function EnvVarsModal({ service, onClose }: { service: ServiceItem; onClose: () => void }) {
  const toast = useToast();
  const auto = service.env_vars.filter(v => v.source === "auto");
  const [rows, setRows] = useState(
    service.env_vars.filter(v => v.source === "user").map(v => ({ key: v.key, value: v.value, is_secret: v.is_secret }))
  );

  const save = useMutation({
    mutationFn: () => api.put(`/api/services/${service.id}/env-vars`, { vars: rows.filter(r => r.key.trim()) }),
    onSuccess: () => { toast.show("Env vars saved", "ok"); onClose(); },
    onError: (e) => toast.show(String(e), "fail"),
  });

  return (
    <Modal open onClose={onClose} title={`Env vars — ${service.name}`} footer={<>
      <span className="spacer" />
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={save.isPending} onClick={() => save.mutate()}>
        {save.isPending ? <span className="spinner" /> : "Save"}
      </button>
    </>}>
      {auto.length > 0 && (
        <>
          <div className="lbl">Auto-wired (from dissection)</div>
          <div className="card" style={{ marginBottom: "1rem" }}>
            {auto.map(v => (
              <div key={v.id} className="row" style={{ justifyContent: "space-between", gap: "0.5rem" }}>
                <code>{v.key}</code><span className="dim" style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{v.value}</span>
              </div>
            ))}
          </div>
        </>
      )}
      <div className="lbl">Your variables (override auto values)</div>
      <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem", marginTop: "0.4rem" }}>
        {rows.map((r, i) => (
          <div key={i} className="row" style={{ gap: "0.4rem" }}>
            <input placeholder="KEY" value={r.key} onChange={e => setRows(rows.map((x, j) => j === i ? { ...x, key: e.target.value } : x))} style={{ flex: "0 0 35%" }} />
            <input placeholder="value" value={r.value} type={r.is_secret ? "password" : "text"} onChange={e => setRows(rows.map((x, j) => j === i ? { ...x, value: e.target.value } : x))} />
            <label className="row" style={{ gap: "0.2rem", cursor: "pointer" }} title="Secret">
              <input type="checkbox" checked={r.is_secret} onChange={e => setRows(rows.map((x, j) => j === i ? { ...x, is_secret: e.target.checked } : x))} />🔒
            </label>
            <button className="btn small ghost" onClick={() => setRows(rows.filter((_, j) => j !== i))}>✕</button>
          </div>
        ))}
        <button className="btn small" onClick={() => setRows([...rows, { key: "", value: "", is_secret: false }])}>+ Add variable</button>
      </div>
    </Modal>
  );
}
