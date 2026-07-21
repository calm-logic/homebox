import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Activity, Boxes, ChevronRight, Globe, Plug, Rocket,
} from "lucide-react";
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, Legend, Line, LineChart,
  Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { api } from "../lib/api";
import type { ActivitySummary, MetricsSummary } from "../lib/types";

// Charts inherit the app's theme via CSS vars, so they track light/dark and
// the user's accent automatically (same approach as ServiceDetail's charts).
const ACCENT = "var(--accent)";
const DANGER = "var(--danger)";
const WARN = "var(--warn)";
const MUTED = "var(--muted)";

const WINDOWS = ["1h", "6h", "24h", "7d", "30d"] as const;
type TimeWindow = (typeof WINDOWS)[number];
// Windows up to a day label the axis by clock; longer ones by calendar day.
const INTRADAY: Set<TimeWindow> = new Set(["1h", "6h", "24h"]);

const tooltipStyle = {
  background: "var(--panel)", border: "1px solid var(--border)",
  borderRadius: 8, fontSize: 12, color: "var(--text)",
} as const;

function fmtBytes(n: number | null): string {
  if (!n) return "0";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
}

export function Overview() {
  const [win, setWin] = useState<TimeWindow>("6h");

  const { data } = useQuery<MetricsSummary>({
    queryKey: ["summary"],
    queryFn: () => api.get<MetricsSummary>("/api/summary"),
  });
  const { data: activity } = useQuery<ActivitySummary>({
    queryKey: ["activity", win],
    queryFn: () => api.get<ActivitySummary>(`/api/activity?window=${win}`),
    refetchInterval: 15000,
  });

  if (!data) {
    return <div><span className="spinner" /></div>;
  }

  const steps: { text: string; done: boolean; href: string }[] = [
    { text: "Add a domain", done: data.domain_count > 0, href: "/domains" },
    { text: "Connect the Cloudflare tunnel", done: data.domain_count > 0, href: "/integrations" },
    { text: "Connect a GitHub organization", done: data.integration_count > 0, href: "/integrations" },
    { text: "Adopt a repository", done: data.managed_count > 0, href: "/projects" },
  ];
  const onboardingDone = steps.every(s => s.done);

  return (
    <>
      <h1>Overview</h1>

      <div className="metric-grid">
        <Metric label="Projects" value={`${data.managed_count}/${data.project_count}`}
                sub="managed" icon={<Boxes size={18} />} link="/projects" />
        <Metric label="Integrations" value={data.integration_count}
                icon={<Plug size={18} />} link="/integrations" />
        <Metric label="Domains" value={data.domain_count}
                icon={<Globe size={18} />} link="/domains" />
        <Metric label="Running" value={activity?.totals.running_envs ?? "—"}
                sub="environments" icon={<Activity size={18} />} link="/projects" />
        <Metric label="Deploys" value={activity?.totals.deploys_7d ?? "—"}
                sub="last 7 days" icon={<Rocket size={18} />} link="/projects" />
      </div>

      {!onboardingDone && (
        <>
          <h2>Get started</h2>
          <ol className="stepper">
            {steps.map((s, i) => (
              <li key={i} className={s.done ? "done" : (steps.findIndex(x => !x.done) === i ? "active" : "")}>
                <Link to={s.href} className="step-link" aria-label={s.text}>
                  <span className="step-text">{s.text}</span>
                  <ChevronRight size={18} className="step-chevron" aria-hidden="true" />
                </Link>
              </li>
            ))}
          </ol>
        </>
      )}

      {data.managed_count > 0 && (
        <>
          <div className="row" style={{ alignItems: "center", marginTop: "1.75rem" }}>
            <h2 style={{ margin: 0 }}>Project activity</h2>
            <div className="spacer" />
            <div className="mode-chips">
              {WINDOWS.map(w => (
                <span key={w} className={`chip ${w === win ? "active" : ""}`} onClick={() => setWin(w)}>{w}</span>
              ))}
            </div>
          </div>
          {activity ? <ActivityCharts activity={activity} /> : <div className="card"><span className="spinner" /></div>}
        </>
      )}
    </>
  );
}

function ActivityCharts({ activity }: { activity: ActivitySummary }) {
  const { buckets, status_breakdown, top_projects } = activity;
  const win = activity.window as TimeWindow;
  const intraday = INTRADAY.has(win);
  const fmtAxis = (ts: string) =>
    intraday
      ? new Date(ts).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
      : new Date(ts).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const fmtTip = (ts: string) =>
    new Date(ts).toLocaleString(undefined, {
      month: "short", day: "numeric",
      ...(intraday ? { hour: "2-digit", minute: "2-digit" } : {}),
    });

  const anyDeploys = buckets.some(b => b.succeeded + b.failed > 0);
  const anyResources = buckets.some(b => b.cpu_pct != null);
  const statusData = [
    { name: "Running", value: status_breakdown.running, color: ACCENT },
    { name: "Building", value: status_breakdown.building, color: WARN },
    { name: "Failed", value: status_breakdown.failed, color: DANGER },
    { name: "Idle", value: status_breakdown.idle, color: MUTED },
  ].filter(d => d.value > 0);

  return (
    <>
      <ChartCard title="Deploys">
        {anyDeploys ? (
          <ResponsiveContainer width="100%" height={200}>
            <BarChart data={buckets} barCategoryGap="12%">
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
              <XAxis dataKey="ts" tickFormatter={fmtAxis} stroke={MUTED} fontSize={11} minTickGap={40} />
              <YAxis allowDecimals={false} stroke={MUTED} fontSize={11} width={28} />
              <Tooltip contentStyle={tooltipStyle} cursor={{ fill: "var(--panel-2)" }} labelFormatter={(l: any) => fmtTip(l as string)} />
              <Bar dataKey="succeeded" name="Succeeded" stackId="a" fill={ACCENT} isAnimationActive={false} />
              <Bar dataKey="failed" name="Failed" stackId="a" fill={DANGER} radius={[3, 3, 0, 0]} isAnimationActive={false} />
            </BarChart>
          </ResponsiveContainer>
        ) : <Empty>No deploys in this window.</Empty>}
      </ChartCard>

      <div className="chart-grid">
        <ChartCard title="Fleet CPU">
          {anyResources ? (
            <ResponsiveContainer width="100%" height={180}>
              <LineChart data={buckets}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                <XAxis dataKey="ts" tickFormatter={fmtAxis} stroke={MUTED} fontSize={11} minTickGap={40} />
                <YAxis unit="%" stroke={MUTED} fontSize={11} width={36} />
                <Tooltip contentStyle={tooltipStyle} labelFormatter={(l: any) => fmtTip(l as string)}
                         formatter={(v: any) => [`${v}%`, "CPU"]} />
                <Line type="monotone" dataKey="cpu_pct" stroke={ACCENT} strokeWidth={2}
                      dot={false} connectNulls isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          ) : <Empty>No samples in this window — metrics appear within a minute of a running deploy.</Empty>}
        </ChartCard>

        <ChartCard title="Fleet memory">
          {anyResources ? (
            <ResponsiveContainer width="100%" height={180}>
              <AreaChart data={buckets}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                <XAxis dataKey="ts" tickFormatter={fmtAxis} stroke={MUTED} fontSize={11} minTickGap={40} />
                <YAxis tickFormatter={fmtBytes} stroke={MUTED} fontSize={11} width={56} />
                <Tooltip contentStyle={tooltipStyle} labelFormatter={(l: any) => fmtTip(l as string)}
                         formatter={(v: any) => [fmtBytes(v), "Memory"]} />
                <Area type="monotone" dataKey="mem_used" stroke={ACCENT} strokeWidth={2}
                      fill="var(--accent-glow)" connectNulls isAnimationActive={false} />
              </AreaChart>
            </ResponsiveContainer>
          ) : <Empty>No samples in this window.</Empty>}
        </ChartCard>
      </div>

      <div className="chart-grid">
        <ChartCard title="Environment status" hint="now">
          {statusData.length > 0 ? (
            <ResponsiveContainer width="100%" height={180}>
              <PieChart>
                <Pie data={statusData} dataKey="value" nameKey="name" innerRadius={44} outerRadius={70}
                     paddingAngle={2} stroke="var(--panel)" strokeWidth={2} isAnimationActive={false}>
                  {statusData.map((d) => <Cell key={d.name} fill={d.color} />)}
                </Pie>
                <Tooltip contentStyle={tooltipStyle} />
                <Legend iconType="circle" formatter={(v) => <span style={{ color: "var(--text-dim)", fontSize: 12 }}>{v}</span>} />
              </PieChart>
            </ResponsiveContainer>
          ) : <Empty>No environments deployed yet.</Empty>}
        </ChartCard>

        <ChartCard title="Most active projects" hint="deploys in window">
          {top_projects.length > 0 ? (
            <ResponsiveContainer width="100%" height={180}>
              <BarChart data={top_projects} layout="vertical" margin={{ left: 8, right: 12 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" horizontal={false} />
                <XAxis type="number" allowDecimals={false} stroke={MUTED} fontSize={11} />
                <YAxis type="category" dataKey="name" stroke={MUTED} fontSize={11} width={90}
                       tickFormatter={(v: string) => (v.length > 12 ? `${v.slice(0, 11)}…` : v)} />
                <Tooltip contentStyle={tooltipStyle} cursor={{ fill: "var(--panel-2)" }}
                         formatter={(v: any) => [v, "Deploys"]} />
                <Bar dataKey="deploys" fill={ACCENT} radius={[0, 3, 3, 0]} barSize={16} isAnimationActive={false} />
              </BarChart>
            </ResponsiveContainer>
          ) : <Empty>No deploys in this window.</Empty>}
        </ChartCard>
      </div>
    </>
  );
}

function ChartCard({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) {
  return (
    <div className="card chart-card">
      <div className="chart-card-head">
        <h3>{title}</h3>
        {hint && <span className="dim">{hint}</span>}
      </div>
      {children}
    </div>
  );
}

function Empty({ children }: { children: ReactNode }) {
  return <div className="chart-empty dim">{children}</div>;
}

function Metric({ label, value, sub, icon, link }: {
  label: string; value: ReactNode; sub?: string; icon: ReactNode; link: string;
}) {
  return (
    <Link to={link} className="metric">
      <div className="metric-top">
        <span className="label">{label}</span>
        <span className="metric-icon">{icon}</span>
      </div>
      <span className="value">{value}</span>
      {sub && <span className="metric-sub">{sub}</span>}
    </Link>
  );
}
