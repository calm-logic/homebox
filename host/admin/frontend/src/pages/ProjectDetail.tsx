import { useState } from "react";
import { Link, Navigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AreaChart, Area, LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from "recharts";
import { ArrowLeft, ExternalLink, RefreshCw, Rocket, Square } from "lucide-react";
import { api } from "../lib/api";
import { useToast } from "../lib/toast";
import type { MetricsResponse, ProjectWorkflowRun, RepoItem } from "../lib/types";

const WINDOWS = ["1h", "6h", "24h", "7d"] as const;
type Window = (typeof WINDOWS)[number];

const fmtTime = (iso: string) =>
  new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(0)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}

function runStatusBadge(status: string, conclusion: string | null) {
  if (status === "completed") {
    if (conclusion === "success") return <span className="badge ok">Success</span>;
    if (conclusion === "failure" || conclusion === "timed_out") return <span className="badge fail">{conclusion}</span>;
    if (conclusion === "cancelled") return <span className="badge warn">Cancelled</span>;
    return <span className="badge plain">{conclusion || "completed"}</span>;
  }
  if (status === "in_progress") return <span className="badge info">In progress</span>;
  if (status === "queued") return <span className="badge info">Queued</span>;
  return <span className="badge plain">{status}</span>;
}

function deployBadge(status: string | undefined) {
  if (status === "running") return <span className="badge ok">Running</span>;
  if (status === "failed") return <span className="badge fail">Failed</span>;
  if (status === "stopped") return <span className="badge muted">Stopped</span>;
  if (!status) return <span className="badge muted plain">Not deployed</span>;
  return <span className="badge info">{status[0].toUpperCase() + status.slice(1)}…</span>;
}

export function ProjectDetail() {
  const { repoId } = useParams();
  const id = Number(repoId);
  const qc = useQueryClient();
  const toast = useToast();
  const [window, setWindow] = useState<Window>("1h");

  // The repo comes from the shared list cache; poll so deploy status stays live.
  const { data: repos } = useQuery<RepoItem[]>({
    queryKey: ["repos"],
    queryFn: () => api.get<RepoItem[]>("/api/repositories"),
    refetchInterval: 6000,
  });

  const repo = repos?.find(r => r.id === id);

  const { data: metrics } = useQuery<MetricsResponse>({
    queryKey: ["repo-metrics", id, window],
    queryFn: () => api.get<MetricsResponse>(`/api/repositories/${id}/metrics?window=${window}`),
    refetchInterval: 15000,
    enabled: !!repo?.managed,
  });

  const { data: runs } = useQuery<ProjectWorkflowRun[]>({
    queryKey: ["repo-workflows", id],
    queryFn: () => api.get<ProjectWorkflowRun[]>(`/api/repositories/${id}/workflows`),
    enabled: !!repo?.managed,
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["repos"] });
  const deploy = useMutation({
    mutationFn: () => api.post(`/api/repositories/${id}/deploy`),
    onSuccess: () => { invalidate(); toast.show("Deploy started", "ok"); },
    onError: (e) => toast.show(String(e), "fail"),
  });
  const stop = useMutation({
    mutationFn: () => api.post(`/api/repositories/${id}/stop`),
    onSuccess: () => { invalidate(); toast.show("Stopped", "ok"); },
    onError: (e) => toast.show(String(e), "fail"),
  });
  const refreshRuns = useMutation({
    mutationFn: () => api.post(`/api/workflows/refresh`),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["repo-workflows", id] }); toast.show("Refreshed", "ok"); },
    onError: (e) => toast.show(String(e), "fail"),
  });

  if (repos && !repo) return <Navigate to="/projects" replace />;
  // The detail page is only reachable for managed projects.
  if (repo && !repo.managed) return <Navigate to="/projects" replace />;
  if (!repo) return <span className="spinner" />;

  const dep = repo.deployment;
  const points = metrics?.points ?? [];
  const busy = !!dep && ["queued", "cloning", "building", "starting"].includes(dep.status);

  return (
    <>
      <Link to="/projects" className="dim" style={{ display: "inline-flex", alignItems: "center", gap: "0.3rem" }}>
        <ArrowLeft size={14} /> Projects
      </Link>

      <div className="row" style={{ marginTop: "0.5rem" }}>
        <h1 style={{ margin: 0 }}>{repo.project_slug || repo.full_name.split("/").pop()}</h1>
        <div className="spacer" />
        <button className="btn" disabled={deploy.isPending || busy} onClick={() => deploy.mutate()}>
          <Rocket size={14} /> Deploy
        </button>
        {dep && dep.status !== "stopped" && (
          <button className="btn ghost" disabled={stop.isPending} onClick={() => stop.mutate()}>
            <Square size={14} /> Stop
          </button>
        )}
      </div>

      <div className="card card-row" style={{ marginTop: "1rem" }}>
        <div className="grow">
          <div className="row">
            {deployBadge(dep?.status)}
            <span className="dim">{repo.full_name}</span>
            <span className="badge muted plain">{repo.default_branch}</span>
          </div>
          <div className="dim" style={{ marginTop: "0.4rem" }}>
            {dep?.url ? <>URL: <a href={dep.url} target="_blank" rel="noopener">{dep.url} <ExternalLink size={11} /></a></> : "Not deployed yet."}
            {dep?.commit_sha && <> · commit <code>{dep.commit_sha.slice(0, 7)}</code></>}
            {dep?.trigger && <> · last trigger: {dep.trigger}</>}
          </div>
          {dep?.status === "failed" && dep.error && (
            <pre className="dim" style={{ marginTop: "0.5rem", whiteSpace: "pre-wrap", fontSize: "0.78rem" }}>{dep.error}</pre>
          )}
        </div>
        <a className="btn ghost" href={`https://github.com/${repo.full_name}`} target="_blank" rel="noopener">
          <ExternalLink size={14} /> GitHub
        </a>
      </div>

      {/* ─── Monitoring ─────────────────────────────────────────── */}
      <div className="row" style={{ marginTop: "2rem" }}>
        <h2 style={{ margin: 0 }}>Monitoring</h2>
        <div className="spacer" />
        <div className="mode-chips">
          {WINDOWS.map(w => (
            <span key={w} className={`chip ${window === w ? "active" : ""}`} onClick={() => setWindow(w)}>{w}</span>
          ))}
        </div>
      </div>

      {points.length === 0 ? (
        <div className="card" style={{ marginTop: "1rem" }}>
          <span className="dim">
            {dep?.status === "running"
              ? "Collecting samples… resource metrics appear within a minute of deploy."
              : "Metrics are collected while the project is running."}
          </span>
        </div>
      ) : (
        <div style={{ display: "grid", gap: "1rem", marginTop: "1rem" }}>
          <ChartCard title="CPU">
            <ResponsiveContainer width="100%" height={200}>
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
            <ResponsiveContainer width="100%" height={200}>
              <AreaChart data={points}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                <XAxis dataKey="ts" tickFormatter={fmtTime} stroke="var(--muted)" fontSize={11} />
                <YAxis tickFormatter={fmtBytes} stroke="var(--muted)" fontSize={11} width={70} />
                <Tooltip labelFormatter={(l: any) => fmtTime(l)} formatter={(v: any) => [fmtBytes(v), "Memory"]} />
                <Area type="monotone" dataKey="mem_used" stroke="var(--accent)" fill="var(--accent-glow)" isAnimationActive={false} />
              </AreaChart>
            </ResponsiveContainer>
          </ChartCard>

          <ChartCard title="Network I/O">
            <ResponsiveContainer width="100%" height={200}>
              <LineChart data={points}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                <XAxis dataKey="ts" tickFormatter={fmtTime} stroke="var(--muted)" fontSize={11} />
                <YAxis tickFormatter={(v: number) => fmtBytes(v)} stroke="var(--muted)" fontSize={11} width={70} />
                <Tooltip labelFormatter={(l: any) => fmtTime(l)} formatter={(v: any, n: any) => [`${fmtBytes(v)}/s`, n === "net_rx_bps" ? "In" : "Out"]} />
                <Line type="monotone" dataKey="net_rx_bps" stroke="var(--info)" dot={false} isAnimationActive={false} />
                <Line type="monotone" dataKey="net_tx_bps" stroke="var(--warn)" dot={false} isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          </ChartCard>
        </div>
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
        <table className="data-table" style={{ marginTop: "1rem" }}>
          <thead><tr><th>Workflow</th><th>Branch</th><th>Status</th><th>When</th><th className="right" /></tr></thead>
          <tbody>
            {runs.map(run => (
              <tr key={run.id}>
                <td>{run.name}</td>
                <td><span className="badge muted plain">{run.head_branch}</span></td>
                <td>{runStatusBadge(run.status, run.conclusion)}</td>
                <td className="dim">{run.created_at ? new Date(run.created_at).toLocaleString() : "—"}</td>
                <td className="actions">
                  <a className="btn small ghost" href={run.html_url} target="_blank" rel="noopener"><ExternalLink size={12} /></a>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div className="card" style={{ marginTop: "1rem" }}>
          <span className="dim">No workflow runs cached yet. Click <strong>Refresh</strong> to pull them from GitHub.</span>
        </div>
      )}
    </>
  );
}

function ChartCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="card">
      <div className="dim" style={{ marginBottom: "0.5rem", fontWeight: 500 }}>{title}</div>
      {children}
    </div>
  );
}
