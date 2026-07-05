import { useEffect, useState } from "react";
import { Link, Navigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AreaChart, Area, LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from "recharts";
import { ArrowLeft, RefreshCw } from "lucide-react";
import { api } from "../lib/api";
import { useToast } from "../lib/toast";
import type { EnvironmentInfo, MetricsResponse, ProjectDetailData, ServiceItem } from "../lib/types";

const WINDOWS = ["1h", "6h", "24h", "7d"] as const;
// Matches app/models.py SECRET_MASK — the API returns this instead of a
// secret's real value, and treats it on save as "keep the stored value".
const SECRET_MASK = "••••••";
type Win = (typeof WINDOWS)[number];

const fmtTime = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(0)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}

/**
 * Service detail: env-var management plus per-environment data browsing
 * (postgres tables / redis keys) or request-level monitoring for stateless
 * public services.
 */

interface DataOverview {
  flavor: "postgres" | "redis";
  tables?: string[];
  dbs?: { index: number; keys: number }[];
}
interface RowsResponse {
  table: string; columns: string[]; rows: Record<string, unknown>[];
  total: number; limit: number; offset: number;
}
interface KeysResponse {
  db: number;
  keys: { key: string; type: string; ttl: number | null }[];
}
interface RequestEntry {
  time: string | null; method: string; path: string;
  status: number; duration_ms: number; client: string;
}

export function ServiceDetail() {
  const { projectId, serviceId } = useParams();
  const pid = Number(projectId);
  const sid = Number(serviceId);

  const { data: project, isError } = useQuery<ProjectDetailData>({
    queryKey: ["project", pid],
    queryFn: () => api.get<ProjectDetailData>(`/api/projects/${pid}`),
  });

  const [envId, setEnvId] = useState<number | null>(null);

  if (isError) return <Navigate to="/projects" replace />;
  if (!project) return <span className="spinner" />;
  const svc = project.services.find(s => s.id === sid);
  if (!svc) return <Navigate to={`/projects/${pid}`} replace />;

  const env = project.environments.find(e => e.id === envId) ?? project.environments[0];
  const isData = svc.kind === "database" || svc.kind === "cache";

  return (
    <>
      <Link to={`/projects/${pid}`} className="dim" style={{ display: "inline-flex", alignItems: "center", gap: "0.3rem" }}>
        <ArrowLeft size={14} /> {project.name}
      </Link>

      <div className="row" style={{ marginTop: "0.5rem" }}>
        <h1 style={{ margin: 0 }}>{svc.name}</h1>
        <span className="badge plain">{svc.kind}</span>
        {svc.is_public
          ? <span className="badge ok">Public</span>
          : <span className="badge muted plain">Internal</span>}
        {svc.internal_port && <span className="dim">port {svc.internal_port}</span>}
      </div>

      {/* Environment picker applies to the data/requests sections. */}
      <div className="tabs" role="tablist" style={{ marginTop: "1rem" }}>
        {project.environments.map(e => (
          <button key={e.id} role="tab" aria-selected={e.id === env?.id}
            className={`tab ${e.id === env?.id ? "active" : ""}`}
            onClick={() => setEnvId(e.id)}>
            <span style={{ textTransform: "capitalize" }}>{e.name}</span>
          </button>
        ))}
      </div>

      {env && isData && <DataBrowser svc={svc} env={env} />}
      {env && !isData && svc.is_public && <RequestMonitor pid={pid} svc={svc} env={env} />}
      {env && !isData && !svc.is_public && (
        <div className="card"><span className="dim">
          Internal stateless service — no public endpoint to monitor. Resource metrics are on the project page.
        </span></div>
      )}

      {env && <Monitoring svc={svc} env={env} />}

      <EnvVarsEditor svc={svc} projectId={pid} />
    </>
  );
}

// ─── Resource monitoring (CPU / memory, per environment) ────────────────────

function Monitoring({ svc, env }: { svc: ServiceItem; env: EnvironmentInfo }) {
  const [win, setWin] = useState<Win>("1h");

  const { data } = useQuery<MetricsResponse>({
    queryKey: ["service-metrics", svc.id, env.id, win],
    queryFn: () => api.get<MetricsResponse>(
      `/api/services/${svc.id}/metrics?window=${win}&environment_id=${env.id}`),
    refetchInterval: 15000,
  });
  const points = data?.points ?? [];

  return (
    <>
      <div className="row" style={{ marginTop: "1.75rem" }}>
        <h2 style={{ margin: 0 }}>Monitoring</h2>
        <div className="spacer" />
        <div className="mode-chips">
          {WINDOWS.map(w => <span key={w} className={`chip ${win === w ? "active" : ""}`} onClick={() => setWin(w)}>{w}</span>)}
        </div>
      </div>
      {points.length === 0 ? (
        <div className="card" style={{ marginTop: "0.5rem" }}>
          <span className="dim">No samples yet — metrics appear within a minute of a running deploy.</span>
        </div>
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

// ─── Data: postgres tables / redis keys ──────────────────────────────────────

function DataBrowser({ svc, env }: { svc: ServiceItem; env: EnvironmentInfo }) {
  const { data: overview, error } = useQuery<DataOverview>({
    queryKey: ["svc-data", svc.id, env.id],
    queryFn: () => api.get<DataOverview>(`/api/services/${svc.id}/data?environment_id=${env.id}`),
    retry: false,
  });

  if (error) return <div className="card"><span className="dim">{String(error)}</span></div>;
  if (!overview) return <span className="spinner" />;
  return overview.flavor === "postgres"
    ? <PostgresBrowser svc={svc} env={env} tables={overview.tables ?? []} />
    : <RedisBrowser svc={svc} env={env} dbs={overview.dbs ?? []} />;
}

function PostgresBrowser({ svc, env, tables }: { svc: ServiceItem; env: EnvironmentInfo; tables: string[] }) {
  const [table, setTable] = useState(tables[0] ?? "");
  const [offset, setOffset] = useState(0);
  useEffect(() => { setTable(tables[0] ?? ""); setOffset(0); }, [env.id, tables.join(",")]);

  const { data, isFetching } = useQuery<RowsResponse>({
    queryKey: ["svc-rows", svc.id, env.id, table, offset],
    queryFn: () => api.get<RowsResponse>(
      `/api/services/${svc.id}/data/rows?environment_id=${env.id}&table=${encodeURIComponent(table)}&offset=${offset}`),
    enabled: !!table,
  });

  if (tables.length === 0) return <div className="card"><span className="dim">No tables yet in this database.</span></div>;

  return (
    <>
      <div className="row">
        <h3 style={{ margin: 0 }}>Data</h3>
        <select value={table} onChange={e => { setTable(e.target.value); setOffset(0); }} style={{ width: "auto" }}>
          {tables.map(t => <option key={t} value={t}>{t}</option>)}
        </select>
        {isFetching && <span className="spinner" />}
        <div className="spacer" />
        {data && (
          <span className="dim">
            {data.total === 0 ? "0 rows" : `${offset + 1}–${Math.min(offset + data.limit, data.total)} of ${data.total}`}
          </span>
        )}
        <button className="btn small" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>‹ Prev</button>
        <button className="btn small" disabled={!data || offset + data.limit >= data.total} onClick={() => setOffset(offset + 50)}>Next ›</button>
      </div>
      {data && (
        data.rows.length === 0 ? (
          <div className="card" style={{ marginTop: "0.5rem" }}><span className="dim">Table is empty.</span></div>
        ) : (
          <div style={{ overflowX: "auto", marginTop: "0.5rem" }}>
            <table className="data-table" style={{ margin: 0 }}>
              <thead><tr>{data.columns.map(c => <th key={c}>{c}</th>)}</tr></thead>
              <tbody>
                {data.rows.map((r, i) => (
                  <tr key={i}>
                    {data.columns.map(c => <td key={c} className="dim" style={{ maxWidth: 260, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={fmtCell(r[c])}>{fmtCell(r[c])}</td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      )}
    </>
  );
}

function fmtCell(v: unknown): string {
  if (v === null || v === undefined) return "∅";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function RedisBrowser({ svc, env, dbs }: { svc: ServiceItem; env: EnvironmentInfo; dbs: { index: number; keys: number }[] }) {
  const list = dbs.length > 0 ? dbs : [{ index: 0, keys: 0 }];
  const [db, setDb] = useState(list[0].index);

  const { data, isFetching } = useQuery<KeysResponse>({
    queryKey: ["svc-keys", svc.id, env.id, db],
    queryFn: () => api.get<KeysResponse>(`/api/services/${svc.id}/data/keys?environment_id=${env.id}&db=${db}`),
  });

  return (
    <>
      <div className="row">
        <h3 style={{ margin: 0 }}>Data</h3>
        <select value={db} onChange={e => setDb(Number(e.target.value))} style={{ width: "auto" }}>
          {list.map(d => <option key={d.index} value={d.index}>db{d.index} ({d.keys} keys)</option>)}
        </select>
        {isFetching && <span className="spinner" />}
      </div>
      {data && (
        data.keys.length === 0 ? (
          <div className="card" style={{ marginTop: "0.5rem" }}><span className="dim">No keys in db{db}.</span></div>
        ) : (
          <table className="data-table" style={{ marginTop: "0.5rem" }}>
            <thead><tr><th>Key</th><th>Type</th><th>TTL</th></tr></thead>
            <tbody>
              {data.keys.map(k => (
                <tr key={k.key}>
                  <td><code>{k.key}</code></td>
                  <td><span className="badge plain">{k.type}</span></td>
                  <td className="dim">{k.ttl === null || k.ttl < 0 ? "∞" : `${k.ttl}s`}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )
      )}
    </>
  );
}

// ─── Requests (stateless public services) ────────────────────────────────────

function RequestMonitor({ pid, svc, env }: { pid: number; svc: ServiceItem; env: EnvironmentInfo }) {
  const { data, error } = useQuery<{ requests: RequestEntry[] }>({
    queryKey: ["svc-requests", svc.id, env.id],
    queryFn: () => api.get<{ requests: RequestEntry[] }>(
      `/api/services/${svc.id}/requests?environment_id=${env.id}`),
    refetchInterval: 5000,
    retry: false,
  });

  if (error) return <div className="card"><span className="dim">{String(error)}</span></div>;
  if (!data) return <span className="spinner" />;

  return (
    <>
      <h3>Requests <span className="dim" style={{ fontWeight: 400 }}>(live, via Traefik access log)</span></h3>
      {data.requests.length === 0 ? (
        <div className="card"><span className="dim">No requests recorded yet — traffic appears here within seconds.</span></div>
      ) : (
        <div style={{ maxHeight: "50vh", overflow: "auto" }}>
          <table className="data-table" style={{ margin: 0 }}>
            <thead><tr><th>Time</th><th>Method</th><th>Path</th><th>Status</th><th className="right">Duration</th></tr></thead>
            <tbody>
              {data.requests.map((r, i) => (
                <tr key={i}>
                  <td className="dim">{r.time ? new Date(r.time).toLocaleTimeString() : "—"}</td>
                  <td><span className="badge plain">{r.method}</span></td>
                  <td className="dim" style={{ maxWidth: 340, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={r.path}>{r.path}</td>
                  <td>{statusBadge(r.status)}</td>
                  <td className="right dim">{r.duration_ms} ms</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function statusBadge(status: number) {
  if (status >= 500) return <span className="badge fail plain">{status}</span>;
  if (status >= 400) return <span className="badge warn plain">{status}</span>;
  return <span className="badge success plain">{status}</span>;
}

// ─── Env vars ────────────────────────────────────────────────────────────────

function EnvVarsEditor({ svc, projectId }: { svc: ServiceItem; projectId: number }) {
  const qc = useQueryClient();
  const toast = useToast();
  const auto = svc.env_vars.filter(v => v.source === "auto");
  const [rows, setRows] = useState(
    svc.env_vars.filter(v => v.source === "user").map(v => ({ key: v.key, value: v.value, is_secret: v.is_secret }))
  );

  const save = useMutation({
    mutationFn: () => api.put(`/api/services/${svc.id}/env-vars`, { vars: rows.filter(r => r.key.trim()) }),
    onSuccess: () => { toast.show("Env vars saved — redeploy to apply", "ok"); qc.invalidateQueries({ queryKey: ["project", projectId] }); },
    onError: (e) => toast.show(String(e), "fail"),
  });

  return (
    <>
      <div className="row" style={{ marginTop: "1.75rem" }}>
        <h2 style={{ margin: 0 }}>Environment</h2>
        <div className="spacer" />
        <button className="btn primary" disabled={save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? <span className="spinner" /> : <><RefreshCw size={14} /> Save</>}
        </button>
      </div>
      {auto.length > 0 && (
        <>
          <div className="lbl" style={{ marginTop: "0.75rem" }}>Auto-wired</div>
          <div className="card" style={{ marginTop: "0.3rem" }}>
            {auto.map(v => (
              <div key={v.id} className="row" style={{ justifyContent: "space-between", gap: "0.5rem" }}>
                <code>{v.key}</code>
                <span className="dim" style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{v.value}</span>
              </div>
            ))}
          </div>
        </>
      )}
      <div className="lbl" style={{ marginTop: "0.75rem" }}>Overrides</div>
      <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem", marginTop: "0.3rem" }}>
        {rows.map((r, i) => (
          <div key={i} className="row" style={{ gap: "0.4rem" }}>
            <input placeholder="KEY" value={r.key} onChange={e => setRows(rows.map((x, j) => j === i ? { ...x, key: e.target.value } : x))} style={{ flex: "0 0 35%" }} />
            <input
              placeholder={r.is_secret && r.value === SECRET_MASK ? "•••••• (unchanged — type to replace)" : "value"}
              value={r.value === SECRET_MASK ? "" : r.value}
              type={r.is_secret ? "password" : "text"}
              onChange={e => setRows(rows.map((x, j) => j === i ? { ...x, value: e.target.value } : x))}
            />
            <label className="row" style={{ gap: "0.3rem", cursor: "pointer" }} title="Secret">
              <input type="checkbox" checked={r.is_secret} onChange={e => setRows(rows.map((x, j) => j === i ? { ...x, is_secret: e.target.checked } : x))} />🔒
            </label>
            <button className="btn small ghost" onClick={() => setRows(rows.filter((_, j) => j !== i))}>✕</button>
          </div>
        ))}
        <div><button className="btn small" onClick={() => setRows([...rows, { key: "", value: "", is_secret: false }])}>+ Add variable</button></div>
      </div>
    </>
  );
}
