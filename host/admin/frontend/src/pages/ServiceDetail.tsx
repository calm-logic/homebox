import { Fragment, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AreaChart, Area, LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from "recharts";
import {
  ArrowLeft, ArrowRight, Braces, Check, ChevronDown, ChevronUp, Copy,
  ExternalLink, Filter as FilterIcon, Key, Lock, X,
} from "lucide-react";
import { api } from "../lib/api";
import { formatColumnName } from "../lib/format";
import { HeaderSave, HeaderSaveButton, useHeaderSave } from "../lib/headerSave";
import { useToast } from "../lib/toast";
import { Modal } from "../components/Modal";
import { ServiceIcon } from "../components/ServiceIcon";
import { useTabIndicator } from "../lib/useTabIndicator";
import type { EnvironmentInfo, MetricsResponse, ServiceItem } from "../lib/types";

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
 * public services. Rendered as a panel inside the ProjectDetail chrome —
 * the chrome's env tabs pick the environment, so there's no picker here.
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

/** One user-editable env-var row, as staged locally and sent to the API. */
interface EnvRow { key: string; value: string; is_secret: boolean }

export function ServicePanel({ projectId, svc, env }: {
  projectId: number;
  svc: ServiceItem;
  env: EnvironmentInfo | undefined;
}) {
  const pid = projectId;
  const isData = svc.kind === "database" || svc.kind === "cache";
  const [tab, setTab] = useState<"data" | "monitoring" | "deployment" | "environment">("data");
  const tabsRef = useRef<HTMLDivElement>(null);
  useTabIndicator(tabsRef, ".tab.active", [tab]);
  const instance = env?.instances.find(i => i.service_name === svc.name);

  // Staged-but-unsaved edits live up here (not inside the tab components) so
  // switching tabs keeps them; the single header Save acts on whichever tab
  // is active and only renders while that tab is dirty.
  const [headerSave, setHeaderSave] = useState<HeaderSave | null>(null);
  const [stagedTargets, setStagedTargets] = useState<Record<number, string>>({});
  const [envRows, setEnvRows] = useState<EnvRow[] | null>(null);
  const [envBaseline, setEnvBaseline] = useState<string | null>(null);

  // A different service is a different edit context — drop staged edits.
  useEffect(() => {
    setStagedTargets({});
    setEnvRows(null);
    setEnvBaseline(null);
  }, [svc.id]);

  const stagedTarget = env ? stagedTargets[env.id] : undefined;
  const setStagedTarget = (v: string | undefined) => {
    if (!env) return;
    setStagedTargets(prev => {
      const next = { ...prev };
      if (v === undefined) delete next[env.id];
      else next[env.id] = v;
      return next;
    });
  };

  return (
    <>
      <div className="row">
        <Link to={`/projects/${pid}/services`} className="back-btn" aria-label="Back to services" title="Back to services">
          <ArrowLeft size={18} />
        </Link>
        <ServiceIcon kind={svc.kind} size={21} />
        <h2 style={{ margin: 0 }}>{svc.name}</h2>
        <span className="service-header-endpoint">
          {svc.is_public ? (instance?.url ? (
              <a href={instance.url} target="_blank" rel="noopener">
                {instance.url.replace(/^https?:\/\//, "")} <ExternalLink size={11} />
              </a>
            ) : <span className="badge ok">Public</span>
          ) : <span className="badge muted plain">Internal</span>}
        </span>
        <HeaderSaveButton state={headerSave} />
      </div>
      <div className="tabs service-detail-tabs" role="tablist" ref={tabsRef}>
        <span className="tab-indicator" aria-hidden />
        {(["data", "monitoring", "deployment", "environment"] as const).map(key => (
          <button key={key} role="tab" aria-selected={tab === key}
            className={`tab ${tab === key ? "active" : ""}`} onClick={() => setTab(key)}>
            {key[0].toUpperCase() + key.slice(1)}
          </button>
        ))}
      </div>

      {tab === "data" && env && isData && <DataBrowser svc={svc} env={env} />}
      {tab === "data" && env && !isData && svc.is_public && <RequestMonitor pid={pid} svc={svc} env={env} />}
      {tab === "data" && env && !isData && !svc.is_public && (
        <div className="card"><span className="dim">This internal service has no browsable data endpoint.</span></div>
      )}
      {tab === "monitoring" && env && <Monitoring svc={svc} env={env} />}
      {tab === "deployment" && env && (
        <TargetSelector svc={svc} env={env} staged={stagedTarget} setStaged={setStagedTarget} onStatus={setHeaderSave} />
      )}
      {tab === "environment" && (
        <EnvVarsEditor svc={svc} projectId={pid} rows={envRows} setRows={setEnvRows}
          baseline={envBaseline} setBaseline={setEnvBaseline} onStatus={setHeaderSave} />
      )}
    </>
  );
}

// ─── Deployment target (per environment) ─────────────────────────────────────

const TARGET_LABELS: Record<string, string> = {
  homebox: "Homebox",
  cloudflare: "Cloudflare",
  aws: "AWS",
  gcp: "Google Cloud",
};
const TARGET_HINTS: Record<string, string> = {
  homebox: "Runs on your own hardware. No extra cost.",
  cloudflare: "Static → Pages (CDN, generous free tier); web/api → Containers (scale-to-zero, needs a Dockerfile; serves from workers.dev in v1).",
  aws: "Static → S3; web/api → App Runner (~$5+/mo); database → EC2 VM (~$15+/mo).",
  gcp: "Static → GCS; web/api → Cloud Run (scale-to-zero; serves from run.app until domain mapping lands); database → GCE VM (~$15+/mo).",
};
const TARGET_PROVIDER: Record<string, string> = {
  aws: "aws", gcp: "gcp", cloudflare: "cloudflare",
};

// Local shims (System.tsx-style) — the structured options shape from
// GET /api/services/{id}/targets after cluster-scoped homebox targets (D3).
interface TargetLocation {
  kind: "local" | "cluster" | "node";
  id: string | null;
  name: string;
  local: boolean;
}
interface TargetOption {
  value: string;
  label?: string;
  locations?: TargetLocation[];   // present on the "homebox" option
}
interface TargetsResponse {
  options: TargetOption[];
  targets: {
    id: number; environment_id: number | null; target: string;
    integration_id: number | null; config: Record<string, unknown>;
    status: string | null; endpoint: string | null; error: string | null;
    updated_at: string | null; inherited: boolean;
  }[];
}

// Select-value encoding: "homebox" (this homebox / absent location),
// "homebox:cluster:<id>", "homebox:node:<id>", or a cloud provider value.
const encodeTarget = (target: string, config: Record<string, unknown> | undefined): string => {
  if (target !== "homebox") return target;
  if (config?.cluster_id) return `homebox:cluster:${config.cluster_id}`;
  if (config?.node_id) return `homebox:node:${config.node_id}`;
  return "homebox";
};
const decodeTarget = (v: string): { target: string; config: Record<string, unknown> } => {
  if (v.startsWith("homebox:cluster:"))
    return { target: "homebox", config: { cluster_id: v.slice("homebox:cluster:".length) } };
  if (v.startsWith("homebox:node:"))
    return { target: "homebox", config: { node_id: v.slice("homebox:node:".length) } };
  return { target: v, config: {} };
};
const locationValue = (loc: TargetLocation): string =>
  loc.kind === "local" || loc.id === null ? "homebox" : `homebox:${loc.kind}:${loc.id}`;

function TargetSelector({ svc, env, staged, setStaged, onStatus }: {
  svc: ServiceItem; env: EnvironmentInfo;
  staged: string | undefined;
  setStaged: (v: string | undefined) => void;
  onStatus: (s: HeaderSave | null) => void;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const { data } = useQuery<TargetsResponse>({
    queryKey: ["service-targets", svc.id],
    queryFn: () => api.get<TargetsResponse>(`/api/services/${svc.id}/targets`),
    refetchInterval: 10000,
  });
  const { data: integrations } = useQuery<{ id: number; provider: string; name: string | null; account_login: string | null }[]>({
    queryKey: ["integrations"],
    queryFn: () => api.get("/api/integrations"),
  });

  const envRow = data?.targets.find(t => t.environment_id === env.id);
  const defaultRow = data?.targets.find(t => t.environment_id === null);
  const effective = envRow ?? defaultRow;
  const baseTarget = effective?.target ?? "homebox";
  const effectiveValue = encodeTarget(baseTarget, effective?.config);
  const current = !effective || effective.inherited ? "default" : effectiveValue;
  const options: TargetOption[] = data?.options ?? [{ value: "homebox" }];

  // Picking in the select only stages the choice; the header Save applies it.
  // The 10s poll refreshes `current`/badges freely but never touches `staged`,
  // so a background refetch can't clobber an unsaved pick — and dirtiness
  // compares against server truth, so re-picking the saved value un-dirties.
  const displayValue = staged ?? current;
  const dirty = staged !== undefined && staged !== current;

  // "Homebox" group: This Homebox (absent location) + every cluster /
  // standalone node the linked account knows about.
  const rawLocations = options.find(o => o.value === "homebox")?.locations ?? [];
  const locations: TargetLocation[] = rawLocations.some(l => l.kind === "local")
    ? rawLocations
    : [{ kind: "local", id: null, name: "This Homebox", local: true }, ...rawLocations];

  // Foreign homebox location → the service runs on ANOTHER cluster/node;
  // status here is synced, read-only.
  const currentLoc = effectiveValue.startsWith("homebox:")
    ? locations.find(l => locationValue(l) === effectiveValue)
    : undefined;
  const isForeign = effectiveValue.startsWith("homebox:") && !(currentLoc?.local ?? false);
  const foreignName = currentLoc?.name
    ?? String(effective?.config?.cluster_id ?? effective?.config?.node_id ?? "");
  const knownValues = new Set<string>([
    ...locations.map(locationValue),
    ...options.filter(o => o.value !== "homebox").map(o => o.value),
  ]);

  const save = useMutation({
    mutationFn: (encoded: string) => {
      if (encoded === "default") {
        if (envRow && !envRow.inherited) {
          return api.del(`/api/services/${svc.id}/target?environment_id=${env.id}`);
        }
        if (defaultRow && !defaultRow.inherited) {
          return api.del(`/api/services/${svc.id}/target`);
        }
        return Promise.resolve({ ok: true });
      }
      const { target, config } = decodeTarget(encoded);
      const provider = TARGET_PROVIDER[target];
      const integ = provider
        ? integrations?.find(i => i.provider === provider)
        : undefined;
      if (provider && provider !== "cloudflare" && !integ) {
        throw new Error(`Connect ${TARGET_LABELS[target]} in Integrations first.`);
      }
      return api.put(`/api/services/${svc.id}/target`, {
        environment_id: env.id,
        target,
        integration_id: integ?.id ?? null,
        config,
      });
    },
    onSuccess: () => {
      // Staged state resets to server truth; the invalidated query brings it.
      setStaged(undefined);
      qc.invalidateQueries({ queryKey: ["service-targets", svc.id] });
      toast.show("Target updated — redeploying affected environments", "ok");
    },
    onError: (e) => toast.show(String(e), "fail"),
  });

  useHeaderSave(onStatus, dirty, save.isPending, () => {
    if (staged !== undefined) save.mutate(staged);
  });

  // Badges/status above stay on SERVER state; only the select and its hint
  // preview the staged choice.
  const displayTarget = displayValue === "default" ? baseTarget : decodeTarget(displayValue).target;

  return (
    <>
      <div className="row" style={{ marginTop: "1.75rem" }}>
        <h2 style={{ margin: 0 }}>Deployment target</h2>
        <div className="spacer" />
        {isForeign && <span className="badge plain">Runs on {foreignName}</span>}
        {effective?.status === "live" && effective.endpoint && (
          <span className="badge ok">live · {effective.endpoint}</span>
        )}
        {effective?.status === "provisioning" && <span className="badge plain">provisioning…</span>}
        {effective?.status === "error" && <span className="badge bad">error</span>}
      </div>
      <div className="card" style={{ marginTop: "0.5rem" }}>
        <div className="field">
          <span className="lbl">Where <b>{svc.name}</b> deploys for <b>{env.name}</b></span>
          <select
            value={displayValue}
            disabled={save.isPending}
            onChange={(e) => setStaged(e.target.value === current ? undefined : e.target.value)}
          >
            <option value="default">Default ({TARGET_LABELS[baseTarget] ?? baseTarget})</option>
            <optgroup label="Homebox">
              {locations.map(loc => (
                <option key={locationValue(loc)} value={locationValue(loc)}>
                  {loc.kind === "local" ? loc.name : `${loc.name}${loc.local ? " (this " + loc.kind + ")" : ""}`}
                </option>
              ))}
              {!knownValues.has(effectiveValue) && effectiveValue.startsWith("homebox:") && (
                <option value={effectiveValue}>{foreignName || effectiveValue}</option>
              )}
            </optgroup>
            {options.filter(o => o.value !== "homebox").map(o => (
              <option key={o.value} value={o.value}>{TARGET_LABELS[o.value] ?? o.label ?? o.value}</option>
            ))}
          </select>
          <span className="hint">{TARGET_HINTS[displayTarget]}</span>
          {dirty && (
            <span className="hint">
              Not applied yet — Save applies the new target and redeploys this environment.
            </span>
          )}
          {(!effective || effective.inherited) && (
            <span className="hint">Inherited from the project deployment target.</span>
          )}
          {!envRow && defaultRow && !defaultRow.inherited && (
            <span className="hint">Inherited from the service-wide override.</span>
          )}
          {isForeign && (
            <span className="hint">
              This service runs on <b>{foreignName}</b> — status shown here is
              synced from the owning cluster and read-only.
            </span>
          )}
          {baseTarget !== "homebox" && (
            <span className="hint">
              Changing the target redeploys this environment; the previous
              target's resources are destroyed once the new one is live.
            </span>
          )}
          {effective?.status === "error" && effective.error && (
            <span className="hint" style={{ color: "var(--bad, #c00)" }}>{effective.error}</span>
          )}
        </div>
      </div>
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

interface ColumnMeta {
  name: string; type: string; udt: string; nullable: boolean;
  default: string | null; pk: boolean;
  fk: { table: string; column: string } | null;
}
interface TableSchema { table: string; columns: ColumnMeta[]; primary_key: string[]; }
interface Filter { col: string; op: string; val: string; }

const FILTER_OPS: { op: string; label: string; needsVal: boolean }[] = [
  { op: "eq", label: "=", needsVal: true },
  { op: "neq", label: "≠", needsVal: true },
  { op: "contains", label: "contains", needsVal: true },
  { op: "gt", label: ">", needsVal: true },
  { op: "gte", label: "≥", needsVal: true },
  { op: "lt", label: "<", needsVal: true },
  { op: "lte", label: "≤", needsVal: true },
  { op: "is_null", label: "is null", needsVal: false },
  { op: "not_null", label: "not null", needsVal: false },
];

const shortType = (c: ColumnMeta): string => {
  const t = c.udt || c.type;
  const map: Record<string, string> = {
    varchar: "text", bpchar: "text", int2: "int2", int4: "int4", int8: "int8",
    float4: "float", float8: "float", bool: "bool", timestamptz: "timestamptz",
    timestamp: "timestamp", jsonb: "jsonb", json: "json", uuid: "uuid",
  };
  return map[t] ?? t;
};

const isNumeric = (c: ColumnMeta) => /^(int|float|numeric|decimal|serial|int2|int4|int8|float4|float8)/.test(c.udt || c.type);
const isBool = (c: ColumnMeta) => (c.udt || c.type) === "bool" || c.type === "boolean";
const isJson = (c: ColumnMeta) => /json/.test(c.udt || c.type);

function PostgresBrowser({ svc, env, tables }: { svc: ServiceItem; env: EnvironmentInfo; tables: string[] }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [table, setTable] = useState(tables[0] ?? "");
  const [offset, setOffset] = useState(0);
  const [orderBy, setOrderBy] = useState<string | null>(null);
  const [dir, setDir] = useState<"asc" | "desc">("asc");
  const [filters, setFilters] = useState<Filter[]>([]);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [editing, setEditing] = useState<{ row: number; col: string } | null>(null);
  const [editValue, setEditValue] = useState("");
  const [editOrig, setEditOrig] = useState("");
  const [related, setRelated] = useState<{ table: string; column: string; value: string } | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const resetView = () => { setOffset(0); setOrderBy(null); setDir("asc"); setFilters([]); setSelected(new Set()); setEditing(null); };
  useEffect(() => { setTable(tables[0] ?? ""); resetView(); }, [env.id, tables.join(",")]);
  useEffect(() => { setSelected(new Set()); setEditing(null); }, [table, offset, orderBy, dir, JSON.stringify(filters)]);

  const { data: schema } = useQuery<TableSchema>({
    queryKey: ["svc-schema", svc.id, env.id, table],
    queryFn: () => api.get<TableSchema>(`/api/services/${svc.id}/data/schema?environment_id=${env.id}&table=${encodeURIComponent(table)}`),
    enabled: !!table,
  });

  const filtersParam = filters.length ? `&filters=${encodeURIComponent(JSON.stringify(filters))}` : "";
  const orderParam = orderBy ? `&order_by=${encodeURIComponent(orderBy)}&dir=${dir}` : "";
  const rowsKey = ["svc-rows", svc.id, env.id, table, offset, orderBy, dir, JSON.stringify(filters)];
  const { data, isFetching } = useQuery<RowsResponse>({
    queryKey: rowsKey,
    queryFn: () => api.get<RowsResponse>(
      `/api/services/${svc.id}/data/rows?environment_id=${env.id}&table=${encodeURIComponent(table)}&offset=${offset}${orderParam}${filtersParam}`),
    enabled: !!table,
  });

  // The pager lives inside the table view; if a page empties out (rows
  // deleted, total shrank) snap back to the first page so it stays reachable.
  useEffect(() => { if (data && data.rows.length === 0 && offset > 0) setOffset(0); }, [data]);

  const colMeta = (name: string): ColumnMeta | undefined => schema?.columns.find(c => c.name === name);
  const pkCols = schema?.primary_key ?? [];
  const canEdit = pkCols.length > 0;
  const pkOf = (r: Record<string, unknown>) => Object.fromEntries(pkCols.map(c => [c, r[c]]));

  const update = useMutation({
    mutationFn: (v: { row: Record<string, unknown>; col: string; value: unknown }) =>
      api.post(`/api/services/${svc.id}/data/update?environment_id=${env.id}`,
        { table, pk: pkOf(v.row), changes: { [v.col]: v.value } }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: rowsKey }); toast.show("Row updated", "ok"); setEditing(null); },
    onError: (e) => toast.show(String(e), "fail"),
  });

  const del = useMutation({
    mutationFn: () => api.post<{ deleted: number }>(`/api/services/${svc.id}/data/delete?environment_id=${env.id}`,
      { table, rows: [...selected].map(i => pkOf(data!.rows[i])) }),
    onSuccess: (r) => { qc.invalidateQueries({ queryKey: rowsKey }); toast.show(`Deleted ${r.deleted} row${r.deleted === 1 ? "" : "s"}`, "ok"); setSelected(new Set()); setConfirmDelete(false); },
    onError: (e) => toast.show(String(e), "fail"),
  });

  if (tables.length === 0) return <div className="card"><span className="dim">No tables yet in this database.</span></div>;

  const startEdit = (rowIdx: number, col: string, cur: unknown) => {
    if (!canEdit) { toast.show("This table has no primary key, so rows can't be edited here.", "info"); return; }
    if (pkCols.includes(col) || colMeta(col)?.default?.includes("gen_random_uuid")) {
      // Editing a PK/identity is a footgun — allow non-PK edits only.
      if (pkCols.includes(col)) { toast.show("Primary-key columns aren't editable.", "info"); return; }
    }
    const s = cur === null || cur === undefined ? "" : typeof cur === "object" ? JSON.stringify(cur) : String(cur);
    setEditing({ row: rowIdx, col });
    setEditValue(s);
    setEditOrig(s);
  };

  const commitEdit = (row: Record<string, unknown>, col: string) => {
    // No change → just close the editor. Don't hit the API or toast "saved".
    if (editValue === editOrig) { setEditing(null); return; }
    const meta = colMeta(col);
    let value: unknown = editValue;
    if (editValue === "" && meta?.nullable) value = null;
    else if (meta && isBool(meta)) value = editValue === "true" || editValue === "t" || editValue === "1";
    update.mutate({ row, col, value });
  };

  const allSelected = !!data && data.rows.length > 0 && selected.size === data.rows.length;
  const toggleAll = () => setSelected(allSelected ? new Set() : new Set(data!.rows.map((_, i) => i)));
  const toggleRow = (i: number) => setSelected(s => { const n = new Set(s); n.has(i) ? n.delete(i) : n.add(i); return n; });

  return (
    <>
      <div className="row" style={{ flexWrap: "wrap", gap: "0.5rem" }}>
        <h3 style={{ margin: 0 }}>Data</h3>
        <select value={table} onChange={e => { setTable(e.target.value); resetView(); }} style={{ width: "auto" }}>
          {tables.map(t => <option key={t} value={t}>{t}</option>)}
        </select>
        <FilterControl columns={schema?.columns ?? []} filters={filters} setFilters={(f) => { setFilters(f); setOffset(0); }} />
        {isFetching && <span className="spinner" />}
        <div className="spacer" />
      </div>

      {selected.size > 0 && (
        <div className="bulk-bar">
          <span>{selected.size} selected</span>
          <div className="spacer" />
          <button className="btn small ghost" onClick={() => setSelected(new Set())}>Clear</button>
          <button className="btn small danger" onClick={() => setConfirmDelete(true)}>Delete {selected.size}</button>
        </div>
      )}

      {data && (
        data.rows.length === 0 ? (
          <div className="card" style={{ marginTop: "0.5rem" }}>
            <span className="dim">{filters.length ? "No rows match the filters." : "Table is empty."}</span>
          </div>
        ) : (
          <div className="data-wrap">
            <div className="data-scroll">
              <table className="data-table editor">
                <thead>
                  <tr>
                    <th className="sel-col">
                      <input type="checkbox" checked={allSelected} onChange={toggleAll} aria-label="Select all" />
                    </th>
                    {data.columns.map((c, ci) => {
                      const m = colMeta(c);
                      const sorted = orderBy === c;
                      return (
                        <th key={c} onClick={() => { if (orderBy === c) setDir(d => d === "asc" ? "desc" : "asc"); else { setOrderBy(c); setDir("asc"); } setOffset(0); }}
                          className={`sortable${ci === 0 ? " pin-col" : ""}`}>
                          <div className="th-inner">
                            <span className="th-name">
                              {m?.pk && <span className="pk-dot" title="Primary key"><Key size={11} /></span>}
                              {formatColumnName(c)}
                              {sorted && <span className="sort-ind">{dir === "asc" ? <ChevronUp size={11} /> : <ChevronDown size={11} />}</span>}
                            </span>
                            <span className="th-type">{m ? shortType(m) : ""}{m?.fk ? ` → ${m.fk.table}` : ""}</span>
                          </div>
                        </th>
                      );
                    })}
                  </tr>
                </thead>
                <tbody>
                  {data.rows.map((r, i) => (
                    <tr key={i} className={selected.has(i) ? "row-selected" : ""}>
                      <td className="sel-col">
                        <input type="checkbox" checked={selected.has(i)} onChange={() => toggleRow(i)} aria-label="Select row" />
                      </td>
                      {data.columns.map((c, ci) => {
                        const m = colMeta(c);
                        const pin = ci === 0 ? " pin-col" : "";
                        const isEditing = editing?.row === i && editing?.col === c;
                        if (isEditing) {
                          return (
                            <td key={c} className={`editing-cell${pin}`}>
                              {m && isBool(m) ? (
                                <select autoFocus value={editValue} onChange={e => setEditValue(e.target.value)}
                                  onBlur={() => commitEdit(r, c)}
                                  onKeyDown={e => { if (e.key === "Enter") commitEdit(r, c); if (e.key === "Escape") setEditing(null); }}>
                                  <option value="true">true</option>
                                  <option value="false">false</option>
                                  {m.nullable && <option value="">∅ null</option>}
                                </select>
                              ) : (
                                <input autoFocus value={editValue}
                                  type={m && isNumeric(m) ? "text" : "text"}
                                  onChange={e => setEditValue(e.target.value)}
                                  onBlur={() => commitEdit(r, c)}
                                  onKeyDown={e => { if (e.key === "Enter") commitEdit(r, c); if (e.key === "Escape") setEditing(null); }} />
                              )}
                            </td>
                          );
                        }
                        const raw = r[c];
                        const display = fmtCell(raw);
                        return (
                          <td key={c} className={`data-cell${raw === null ? " null-cell" : ""}${pin}`}
                            onDoubleClick={() => startEdit(i, c, raw)} title={display}>
                            <span className="cell-val">{display}</span>
                            <FkGoto meta={m} raw={raw} onGoto={setRelated} />
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="data-pager">
              <div className="data-pager-inner">
                <button className="btn small" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>‹ Prev</button>
                <span className="dim pager-count">
                  {data.total === 0 ? "0 rows" : `${offset + 1}–${Math.min(offset + data.limit, data.total)} of ${data.total}`}
                </span>
                <button className="btn small" disabled={offset + data.limit >= data.total} onClick={() => setOffset(offset + 50)}>Next ›</button>
              </div>
            </div>
          </div>
        )
      )}
      {!canEdit && data && data.rows.length > 0 && (
        <div className="dim" style={{ marginTop: "0.35rem", fontSize: 12 }}>
          This table has no primary key — rows are read-only here.
        </div>
      )}

      {related && (
        <RelatedRowModal svc={svc} env={env} target={related} onNavigate={setRelated} onClose={() => setRelated(null)} />
      )}

      <Modal open={confirmDelete} title={`Delete ${selected.size} row${selected.size === 1 ? "" : "s"}?`}
        onClose={() => setConfirmDelete(false)}
        footer={<>
          <button className="btn ghost" onClick={() => setConfirmDelete(false)}>Cancel</button>
          <button className="btn danger" onClick={() => del.mutate()} disabled={del.isPending}>Delete</button>
        </>}>
        This permanently deletes the selected rows from <code>{table}</code>. This can't be undone.
      </Modal>
    </>
  );
}

/** A "go to related row" jump target — where a foreign key points. */
interface RelTarget { table: string; column: string; value: string }

/**
 * Shared relation affordance: renders the goto-related-row button next to a
 * value when (and only when) the column has a detected foreign key and the
 * value is non-null. Used by both the table cells and the detail row modal so
 * the detection logic and navigation behavior stay identical.
 */
function FkGoto({ meta, raw, onGoto }: { meta: ColumnMeta | undefined; raw: unknown; onGoto: (t: RelTarget) => void }) {
  if (!meta?.fk || raw === null || raw === undefined) return null;
  const fk = meta.fk;
  return (
    <button className="fk-arrow" title={`Open ${fk.table}`}
      onClick={(e) => { e.stopPropagation(); onGoto({ table: fk.table, column: fk.column, value: String(raw) }); }}>
      <ArrowRight size={12} />
    </button>
  );
}

/**
 * Filter icon + anchored popover + removable chips, all living inline in the
 * table toolbar row. Clicking a chip reopens the popover with that filter
 * loaded for editing.
 */
function FilterControl({ columns, filters, setFilters }: { columns: ColumnMeta[]; filters: Filter[]; setFilters: (f: Filter[]) => void }) {
  const [open, setOpen] = useState(false);
  const [editIdx, setEditIdx] = useState<number | null>(null);
  const [col, setCol] = useState("");
  const [op, setOp] = useState("contains");
  const [val, setVal] = useState("");
  const anchorRef = useRef<HTMLDivElement>(null);

  useEffect(() => { if (!col && columns.length) setCol(columns[0].name); }, [columns]);

  // Close on outside click / Escape while the popover is open.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (anchorRef.current && !anchorRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDown); document.removeEventListener("keydown", onKey); };
  }, [open]);

  const opMeta = FILTER_OPS.find(o => o.op === op);
  const openBlank = () => { setEditIdx(null); setVal(""); setOpen(true); };
  const openEdit = (i: number) => {
    const f = filters[i];
    setEditIdx(i); setCol(f.col); setOp(f.op); setVal(f.val); setOpen(true);
  };
  const apply = () => {
    if (!col) return;
    const f: Filter = { col, op, val: opMeta?.needsVal ? val : "" };
    setFilters(editIdx === null ? [...filters, f] : filters.map((x, i) => (i === editIdx ? f : x)));
    setVal(""); setEditIdx(null); setOpen(false);
  };

  return (
    <>
      <div className="filter-anchor" ref={anchorRef}>
        <button type="button" className={`icon-btn filter-btn${filters.length ? " active" : ""}`}
          title="Filters" aria-label="Filters" aria-expanded={open}
          onClick={() => (open ? setOpen(false) : openBlank())}>
          <FilterIcon size={14} />
          {filters.length > 0 && <span className="filter-count">{filters.length}</span>}
        </button>
        {open && (
          <div className="filter-pop">
            <div className="filter-pop-row">
              <select value={col} onChange={e => setCol(e.target.value)}>
                {columns.map(c => <option key={c.name} value={c.name}>{formatColumnName(c.name)}</option>)}
              </select>
              <select value={op} onChange={e => setOp(e.target.value)}>
                {FILTER_OPS.map(o => <option key={o.op} value={o.op}>{o.label}</option>)}
              </select>
            </div>
            {opMeta?.needsVal && (
              <input autoFocus value={val} onChange={e => setVal(e.target.value)} placeholder="value"
                onKeyDown={e => { if (e.key === "Enter") apply(); }} />
            )}
            <div className="filter-pop-actions">
              <button className="btn small ghost" onClick={() => setOpen(false)}>Cancel</button>
              <button className="btn small" onClick={apply}>{editIdx === null ? "Add filter" : "Update filter"}</button>
            </div>
          </div>
        )}
      </div>
      {filters.map((f, i) => (
        <span key={i} className="filter-chip editable" onClick={() => openEdit(i)} title="Edit filter">
          {formatColumnName(f.col)} {FILTER_OPS.find(o => o.op === f.op)?.label ?? f.op}
          {f.op !== "is_null" && f.op !== "not_null" ? ` ${f.val}` : ""}
          <button aria-label="Remove filter"
            onClick={(e) => { e.stopPropagation(); setFilters(filters.filter((_, j) => j !== i)); }}>
            <X size={11} />
          </button>
        </span>
      ))}
    </>
  );
}

function RelatedRowModal({ svc, env, target, onNavigate, onClose }: {
  svc: ServiceItem; env: EnvironmentInfo; target: RelTarget;
  onNavigate: (t: RelTarget) => void; onClose: () => void;
}) {
  const { data } = useQuery<{ table: string; columns: ColumnMeta[]; row: Record<string, unknown> | null }>({
    queryKey: ["svc-related", svc.id, env.id, target.table, target.column, target.value],
    queryFn: () => api.get(`/api/services/${svc.id}/data/related?environment_id=${env.id}&table=${encodeURIComponent(target.table)}&column=${encodeURIComponent(target.column)}&value=${encodeURIComponent(target.value)}`),
  });
  return (
    <Modal open title={`${target.table} · ${formatColumnName(target.column)} = ${target.value}`} onClose={onClose}>
      {!data ? <span className="spinner" /> : !data.row ? (
        <span className="dim">No matching row.</span>
      ) : (
        <div className="related-grid">
          {data.columns.map(c => {
            const raw = data.row![c.name];
            const json = jsonValueOf(c, raw);
            return (
              <div key={c.name} className="related-field">
                <div className="related-key">
                  {c.pk && <span className="pk-dot" title="Primary key"><Key size={11} /></span>}
                  {formatColumnName(c.name)} <span className="dim">{shortType(c)}</span>
                </div>
                <div className="related-val">
                  {json !== undefined ? <JsonValue value={json} /> : (
                    <>
                      {fmtCell(raw)}
                      <FkGoto meta={c} raw={raw} onGoto={onNavigate} />
                    </>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Modal>
  );
}

function fmtCell(v: unknown): string {
  if (v === null || v === undefined) return "∅";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

/**
 * Returns the structured (object/array) form of a JSON-ish field value for
 * the detail view, or undefined when the value should render as plain text.
 */
function jsonValueOf(c: ColumnMeta, raw: unknown): unknown {
  if (raw !== null && typeof raw === "object") return raw;
  if (isJson(c) && typeof raw === "string") {
    try {
      const parsed = JSON.parse(raw);
      if (parsed !== null && typeof parsed === "object") return parsed;
    } catch { /* not valid JSON — fall through to plain rendering */ }
  }
  return undefined;
}

// ─── Simplified-YAML rendering for JSON fields in the detail view ───────────
//
// {"x": "y"} renders as `x: y`; nested objects/arrays indent one step per
// level, each nested line prefixed with a depth marker instead of plain
// spaces: ▸ (depth 1), • (depth 2), · (depth 3), cycling deeper. A toggle
// shows the raw pretty-printed JSON, and a copy button preserves access to
// the exact original payload.

const YAML_BULLETS = ["▸", "•", "·"] as const;
const yamlBullet = (depth: number) => YAML_BULLETS[Math.max(0, depth - 1) % YAML_BULLETS.length];
const isScalar = (v: unknown) => v === null || typeof v !== "object";
const scalarText = (v: unknown): string => {
  if (v === null) return "null";
  if (typeof v === "string") return v === "" ? '""' : v;
  return JSON.stringify(v);
};

function YamlLine({ depth, bullet, yamlKey, value, muted }: {
  depth: number; bullet?: string; yamlKey?: string; value?: string; muted?: boolean;
}) {
  return (
    <div className="yaml-line" style={{ paddingLeft: depth * 16 }}>
      {bullet && <span className="yaml-bullet">{bullet}</span>}
      {yamlKey !== undefined && <span className="yaml-key">{yamlKey}:</span>}
      {value !== undefined && <span className={`yaml-val${muted ? " dim" : ""}`}>{value}</span>}
    </div>
  );
}

function YamlLines({ value, depth }: { value: unknown; depth: number }) {
  if (isScalar(value)) return <YamlLine depth={depth} bullet={depth > 0 ? yamlBullet(depth) : undefined} value={scalarText(value)} />;
  if (Array.isArray(value)) {
    if (value.length === 0) return <YamlLine depth={depth} bullet={depth > 0 ? yamlBullet(depth) : undefined} value="[]" muted />;
    return (
      <>
        {value.map((item, i) => isScalar(item) ? (
          <YamlLine key={i} depth={depth} bullet={yamlBullet(Math.max(depth, 1))} value={scalarText(item)} />
        ) : (
          <Fragment key={i}>
            <YamlLine depth={depth} bullet={yamlBullet(Math.max(depth, 1))} value={`#${i + 1}`} muted />
            <YamlLines value={item} depth={depth + 1} />
          </Fragment>
        ))}
      </>
    );
  }
  const entries = Object.entries(value as Record<string, unknown>);
  if (entries.length === 0) return <YamlLine depth={depth} bullet={depth > 0 ? yamlBullet(depth) : undefined} value="{}" muted />;
  return (
    <>
      {entries.map(([k, v]) => isScalar(v) ? (
        <YamlLine key={k} depth={depth} bullet={depth > 0 ? yamlBullet(depth) : undefined} yamlKey={k} value={scalarText(v)} />
      ) : (
        <Fragment key={k}>
          <YamlLine depth={depth} bullet={depth > 0 ? yamlBullet(depth) : undefined} yamlKey={k} />
          <YamlLines value={v} depth={depth + 1} />
        </Fragment>
      ))}
    </>
  );
}

/** JSON field in the detail view: simplified-YAML by default, raw toggle + copy. */
function JsonValue({ value }: { value: unknown }) {
  const [showRaw, setShowRaw] = useState(false);
  const [copied, setCopied] = useState(false);
  const rawText = JSON.stringify(value, null, 2);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(rawText);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch { /* clipboard unavailable (e.g. plain http) — button is best-effort */ }
  };
  return (
    <div className="json-view">
      <div className="json-tools">
        <button type="button" className={`json-tool${showRaw ? " active" : ""}`}
          title={showRaw ? "Show simplified view" : "Show raw JSON"}
          aria-label={showRaw ? "Show simplified view" : "Show raw JSON"}
          onClick={() => setShowRaw(s => !s)}>
          <Braces size={11} />
        </button>
        <button type="button" className="json-tool" title="Copy raw JSON" aria-label="Copy raw JSON" onClick={copy}>
          {copied ? <Check size={11} /> : <Copy size={11} />}
        </button>
      </div>
      {showRaw
        ? <pre className="json-raw">{rawText}</pre>
        : <div className="yaml-view"><YamlLines value={value} depth={0} /></div>}
    </div>
  );
}

interface KeyValue {
  key: string; type: string; ttl: number | null;
  value: string | { fields: { field: string; value: string }[] }
    | { items: string[] } | { members: string[] }
    | { members: { member: string; score: string }[] } | null;
}
const REDIS_TYPE_COLOR: Record<string, string> = {
  string: "green", hash: "blue", list: "amber", set: "purple", zset: "pink", stream: "gray",
};

function RedisBrowser({ svc, env, dbs }: { svc: ServiceItem; env: EnvironmentInfo; dbs: { index: number; keys: number }[] }) {
  const qc = useQueryClient();
  const toast = useToast();
  const list = dbs.length > 0 ? dbs : [{ index: 0, keys: 0 }];
  const [db, setDb] = useState(list[0].index);
  const [search, setSearch] = useState("");
  const [applied, setApplied] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [viewKey, setViewKey] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);

  useEffect(() => { setSelected(new Set()); }, [db, applied]);

  const matchParam = applied ? `&match=${encodeURIComponent(applied.includes("*") ? applied : `*${applied}*`)}` : "";
  const keysKey = ["svc-keys", svc.id, env.id, db, applied];
  const { data, isFetching } = useQuery<KeysResponse>({
    queryKey: keysKey,
    queryFn: () => api.get<KeysResponse>(`/api/services/${svc.id}/data/keys?environment_id=${env.id}&db=${db}${matchParam}`),
  });

  const del = useMutation({
    mutationFn: () => api.post<{ deleted: number }>(`/api/services/${svc.id}/data/key/delete?environment_id=${env.id}`,
      { db, keys: [...selected] }),
    onSuccess: (r) => { qc.invalidateQueries({ queryKey: keysKey }); toast.show(`Deleted ${r.deleted} key${r.deleted === 1 ? "" : "s"}`, "ok"); setSelected(new Set()); setConfirmDelete(false); },
    onError: (e) => toast.show(String(e), "fail"),
  });

  const keys = data?.keys ?? [];
  const allSelected = keys.length > 0 && selected.size === keys.length;
  const toggleAll = () => setSelected(allSelected ? new Set() : new Set(keys.map(k => k.key)));
  const toggleKey = (k: string) => setSelected(s => { const n = new Set(s); n.has(k) ? n.delete(k) : n.add(k); return n; });

  return (
    <>
      <div className="row" style={{ flexWrap: "wrap", gap: "0.5rem" }}>
        <h3 style={{ margin: 0 }}>Data</h3>
        <select value={db} onChange={e => { setDb(Number(e.target.value)); setApplied(""); setSearch(""); }} style={{ width: "auto" }}>
          {list.map(d => <option key={d.index} value={d.index}>db{d.index} ({d.keys} keys)</option>)}
        </select>
        <input value={search} onChange={e => setSearch(e.target.value)} placeholder="search keys (glob ok)"
          onKeyDown={e => { if (e.key === "Enter") setApplied(search.trim()); }} style={{ maxWidth: 220 }} />
        <button className="btn small" onClick={() => setApplied(search.trim())}>Search</button>
        {applied && <button className="btn small ghost" onClick={() => { setApplied(""); setSearch(""); }}>Clear</button>}
        {isFetching && <span className="spinner" />}
        <div className="spacer" />
        <span className="dim">{keys.length}{keys.length >= 300 ? "+" : ""} key{keys.length === 1 ? "" : "s"}</span>
      </div>

      {selected.size > 0 && (
        <div className="bulk-bar">
          <span>{selected.size} selected</span>
          <div className="spacer" />
          <button className="btn small ghost" onClick={() => setSelected(new Set())}>Clear</button>
          <button className="btn small danger" onClick={() => setConfirmDelete(true)}>Delete {selected.size}</button>
        </div>
      )}

      {data && (
        keys.length === 0 ? (
          <div className="card" style={{ marginTop: "0.5rem" }}>
            <span className="dim">{applied ? `No keys match “${applied}”.` : `No keys in db${db}.`}</span>
          </div>
        ) : (
          <div className="data-scroll">
            <table className="data-table editor">
              <thead><tr>
                <th className="sel-col"><input type="checkbox" checked={allSelected} onChange={toggleAll} aria-label="Select all" /></th>
                <th className="pin-col"><div className="th-inner"><span className="th-name">Key</span></div></th>
                <th><div className="th-inner"><span className="th-name">Type</span></div></th>
                <th><div className="th-inner"><span className="th-name">TTL</span></div></th>
              </tr></thead>
              <tbody>
                {keys.map(k => (
                  <tr key={k.key} className={selected.has(k.key) ? "row-selected" : ""}>
                    <td className="sel-col"><input type="checkbox" checked={selected.has(k.key)} onChange={() => toggleKey(k.key)} aria-label="Select key" /></td>
                    <td className="data-cell pin-col" style={{ cursor: "pointer" }} onClick={() => setViewKey(k.key)} title={k.key}>
                      <code>{k.key}</code>
                    </td>
                    <td><span className={`badge ${REDIS_TYPE_COLOR[k.type] ?? "plain"}`}>{k.type}</span></td>
                    <td className="dim">{k.ttl === null || k.ttl < 0 ? "∞" : `${k.ttl}s`}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      )}

      {viewKey !== null && (
        <RedisKeyModal svc={svc} env={env} db={db} keyName={viewKey} onClose={() => setViewKey(null)}
          onChanged={() => qc.invalidateQueries({ queryKey: keysKey })} />
      )}

      <Modal open={confirmDelete} title={`Delete ${selected.size} key${selected.size === 1 ? "" : "s"}?`}
        onClose={() => setConfirmDelete(false)}
        footer={<>
          <button className="btn ghost" onClick={() => setConfirmDelete(false)}>Cancel</button>
          <button className="btn danger" onClick={() => del.mutate()} disabled={del.isPending}>Delete</button>
        </>}>
        This permanently removes the selected keys from db{db}.
      </Modal>
    </>
  );
}

function RedisKeyModal({ svc, env, db, keyName, onClose, onChanged }: {
  svc: ServiceItem; env: EnvironmentInfo; db: number; keyName: string; onClose: () => void; onChanged: () => void;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const [edit, setEdit] = useState<string | null>(null);
  const kkey = ["svc-key", svc.id, env.id, db, keyName];
  const { data } = useQuery<KeyValue>({
    queryKey: kkey,
    queryFn: () => api.get<KeyValue>(`/api/services/${svc.id}/data/key?environment_id=${env.id}&db=${db}&key=${encodeURIComponent(keyName)}`),
  });
  const save = useMutation({
    mutationFn: () => api.post(`/api/services/${svc.id}/data/key/set?environment_id=${env.id}`, { db, key: keyName, value: edit ?? "" }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: kkey }); onChanged(); toast.show("Value saved", "ok"); setEdit(null); },
    onError: (e) => toast.show(String(e), "fail"),
  });

  const ttl = data?.ttl;
  return (
    <Modal open title={keyName} onClose={onClose}
      footer={data?.type === "string" ? (
        edit === null
          ? <button className="btn" onClick={() => setEdit(typeof data.value === "string" ? data.value : "")}>Edit value</button>
          : <>
              <button className="btn ghost" onClick={() => setEdit(null)}>Cancel</button>
              <button className="btn" onClick={() => save.mutate()} disabled={save.isPending}>Save</button>
            </>
      ) : undefined}>
      {!data ? <span className="spinner" /> : (
        <>
          <div className="row" style={{ gap: "0.5rem", marginBottom: "0.7rem" }}>
            <span className={`badge ${REDIS_TYPE_COLOR[data.type] ?? "plain"}`}>{data.type}</span>
            <span className="dim">TTL {ttl === null || ttl === undefined || ttl < 0 ? "∞ (no expiry)" : `${ttl}s`}</span>
          </div>
          {renderRedisValue(data, edit, setEdit)}
        </>
      )}
    </Modal>
  );
}

function renderRedisValue(data: KeyValue, edit: string | null, setEdit: (v: string) => void) {
  const v = data.value;
  if (data.type === "string") {
    return edit !== null ? (
      <textarea value={edit} onChange={e => setEdit(e.target.value)} rows={8}
        style={{ width: "100%", fontFamily: "monospace", fontSize: 13 }} />
    ) : (
      <pre className="redis-value">{typeof v === "string" ? v : ""}</pre>
    );
  }
  if (v && typeof v === "object" && "fields" in v) {
    return (
      <div className="related-grid">
        {v.fields.map((f, i) => (
          <div key={i} className="related-field">
            <div className="related-key">{f.field}</div>
            <div className="related-val">{f.value}</div>
          </div>
        ))}
      </div>
    );
  }
  if (v && typeof v === "object" && "items" in v) {
    return <ol className="redis-list">{v.items.map((it, i) => <li key={i}>{it}</li>)}</ol>;
  }
  if (v && typeof v === "object" && "members" in v) {
    const members = v.members;
    if (members.length && typeof members[0] === "object") {
      return (
        <div className="related-grid">
          {(members as { member: string; score: string }[]).map((m, i) => (
            <div key={i} className="related-field"><div className="related-key">{m.member}</div><div className="related-val">{m.score}</div></div>
          ))}
        </div>
      );
    }
    return <ul className="redis-list">{(members as string[]).map((m, i) => <li key={i}>{m}</li>)}</ul>;
  }
  return <span className="dim">Empty.</span>;
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

/** Dirty/compare form: empty-key rows are placeholders, not real changes. */
const serializeEnvRows = (rows: EnvRow[]): string =>
  JSON.stringify(rows.filter(r => r.key.trim()).map(r => [r.key, r.value, r.is_secret]));

function EnvVarsEditor({ svc, projectId, rows: stagedRows, setRows: setStagedRows, baseline, setBaseline, onStatus }: {
  svc: ServiceItem; projectId: number;
  rows: EnvRow[] | null;
  setRows: (r: EnvRow[] | null) => void;
  baseline: string | null;
  setBaseline: (b: string | null) => void;
  onStatus: (s: HeaderSave | null) => void;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const auto = svc.env_vars.filter(v => v.source === "auto");
  const serverRows: EnvRow[] = svc.env_vars
    .filter(v => v.source === "user")
    .map(v => ({ key: v.key, value: v.value, is_secret: v.is_secret }));
  const serverSer = serializeEnvRows(serverRows);

  // stagedRows === null → no local edits, the editor mirrors server truth.
  const rows = stagedRows ?? serverRows;
  const setRows = (next: EnvRow[]) => setStagedRows(next);

  // Dirty = staged content differs from the last-known saved truth. Right
  // after a save the project query lags one refetch behind, so the just-saved
  // serialization (baseline) stands in for the server until it catches up.
  const dirty = serializeEnvRows(rows) !== (baseline ?? serverSer);

  // Background refetch brought new server truth: adopt it only when nothing
  // is unsaved — staged edits are never clobbered by polling.
  useEffect(() => {
    if (!dirty) {
      setStagedRows(null);
      setBaseline(null);
    }
    // Deps are server truth only: an edit flipping `dirty` must not reseed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverSer]);

  const save = useMutation({
    mutationFn: async () => {
      const sent = rows.filter(r => r.key.trim());
      const res = await api.put<{ redeployed?: { environment: string }[] }>(
        `/api/services/${svc.id}/env-vars`, { vars: sent });
      return { res, sent };
    },
    onSuccess: ({ res, sent }) => {
      const envs = res?.redeployed ?? [];
      toast.show(
        envs.length
          ? `Env vars saved — redeploying ${envs.map(e => e.environment).join(", ")}`
          : "Env vars saved",
        "ok",
      );
      qc.invalidateQueries({ queryKey: ["project", projectId] });
      // The server echoes secrets masked — mirror that locally so the saved
      // state compares clean, unless the user kept editing while in flight.
      const normalized = sent.map(r => (r.is_secret ? { ...r, value: SECRET_MASK } : r));
      setBaseline(serializeEnvRows(normalized));
      if (serializeEnvRows(rows) === serializeEnvRows(sent)) setStagedRows(normalized);
    },
    onError: (e) => toast.show(String(e), "fail"),
  });

  useHeaderSave(onStatus, dirty, save.isPending, () => save.mutate());

  return (
    <>
      <div className="row" style={{ marginTop: "1.75rem" }}>
        <h2 style={{ margin: 0 }}>Environment</h2>
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
              <input type="checkbox" checked={r.is_secret} onChange={e => setRows(rows.map((x, j) => j === i ? { ...x, is_secret: e.target.checked } : x))} /><Lock size={13} />
            </label>
            <button className="btn small ghost" onClick={() => setRows(rows.filter((_, j) => j !== i))}>✕</button>
          </div>
        ))}
        <div><button className="btn small" onClick={() => setRows([...rows, { key: "", value: "", is_secret: false }])}>+ Add variable</button></div>
      </div>
    </>
  );
}
