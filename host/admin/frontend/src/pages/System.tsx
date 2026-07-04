import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Gauge } from "lucide-react";
import { api } from "../lib/api";
import type { UptimeReport, UptimeStatus } from "../lib/types";

/** System health: infrastructure uptime, latency, and the self-healing
 * monitor's view of the stack. Grows over time (logs, disk, versions…). */

const UPTIME_COLORS: Record<UptimeStatus, string> = {
  up: "var(--accent)",
  degraded: "var(--warn)",
  down: "var(--danger)",
  unknown: "var(--border)",
};
const UPTIME_BADGE: Record<UptimeStatus, string> = {
  up: "ok", degraded: "warn", down: "fail", unknown: "warn",
};
const COMPONENT_LABEL: Record<string, string> = {
  admin_url: "Public URL (end-to-end)",
  tunnel: "Tunnel at Cloudflare edge",
  cloudflared: "Connector (cloudflared)",
  traefik: "Traefik router",
  docker_proxy: "Docker socket proxy",
  dns: "DNS records (hourly check)",
};
const UPTIME_WINDOWS = ["6h", "24h", "7d", "14d"];

function latencyColor(ms: number): string {
  if (ms < 300) return "var(--accent)";
  if (ms < 1000) return "var(--warn)";
  return "var(--danger)";
}

function Sparkline({ points }: { points: { status: UptimeStatus }[] }) {
  if (points.length === 0) return <span className="dim">collecting…</span>;
  return (
    <div style={{ display: "flex", gap: 1, alignItems: "stretch", height: 16 }}>
      {points.map((p, i) => (
        <div
          key={i}
          title={p.status}
          style={{ width: 4, borderRadius: 1, background: UPTIME_COLORS[p.status] ?? UPTIME_COLORS.unknown }}
        />
      ))}
    </div>
  );
}

export function System() {
  const [window, setWindow] = useState("24h");
  const { data } = useQuery<UptimeReport>({
    queryKey: ["tunnel-uptime", window],
    queryFn: () => api.get<UptimeReport>(`/api/tunnel/uptime?window=${window}`),
    refetchInterval: 5000,
  });

  return (
    <>
      <h1>System</h1>
      <p className="lede">Health of the Homebox infrastructure — monitored every 30s, self-healing.</p>

      <div className="card">
        <div className="card-row">
          <div className="grow">
            <div className="row">
              <span className="badge ok"><Gauge size={12} /> Uptime</span>
            </div>
          </div>
          <div className="mode-chips">
            {UPTIME_WINDOWS.map((w) => (
              <span key={w} className={`chip ${w === window ? "active" : ""}`} onClick={() => setWindow(w)}>
                {w}
              </span>
            ))}
          </div>
        </div>

        <div style={{ marginTop: "0.75rem", display: "flex", flexDirection: "column", gap: "0.6rem" }}>
          {!data ? (
            <span className="spinner" />
          ) : (
            data.components.map((c) => (
              <div key={c.component} className="row" style={{ justifyContent: "space-between", gap: "0.75rem", flexWrap: "wrap" }}>
                <div className="row" style={{ gap: "0.5rem", minWidth: 0 }}>
                  <span className={`badge ${UPTIME_BADGE[c.current]}`}>{c.current}</span>
                  <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>
                    {COMPONENT_LABEL[c.component] ?? c.component}
                  </span>
                </div>
                <div className="row" style={{ gap: "0.75rem" }}>
                  {c.latency_ms != null && (
                    <span style={{ color: latencyColor(c.latency_ms), fontSize: "0.85rem", minWidth: "3.5rem", textAlign: "right" }}>
                      {c.latency_ms} ms
                    </span>
                  )}
                  <Sparkline points={c.timeline} />
                  <strong style={{ minWidth: "3.5rem", textAlign: "right" }}>
                    {c.uptime_pct == null ? "—" : `${c.uptime_pct}%`}
                  </strong>
                </div>
              </div>
            ))
          )}
        </div>

        {data && data.components.every((c) => c.sample_count === 0) && (
          <div className="dim" style={{ marginTop: "0.6rem" }}>
            No samples yet — the monitor records one per component every 30 seconds.
          </div>
        )}
      </div>
    </>
  );
}
