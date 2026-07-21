import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, ExternalLink, Globe } from "lucide-react";
import { api } from "../lib/api";

/**
 * Domain drilldown — everything served under one domain. Data comes from
 * GET /api/domains/:id: one connection per (public service, environment)
 * whose effective domain resolves here, with the derived hostname, where the
 * service runs (this install / a named cluster or node / a cloud provider)
 * and its latest deploy status; plus any per-host DNS override records this
 * install wrote on the domain.
 */

interface DomainLocation {
  kind: "local" | "cluster" | "node" | "cloud";
  id: string | null;
  name: string;
}

interface DomainConnection {
  hostname: string;
  path: string | null;
  url: string;
  project_id: number;
  project_name: string;
  project_icon: string | null;
  environment_id: number;
  environment_name: string;
  service_id: number;
  service_name: string;
  service_kind: string;
  target: string;
  location: DomainLocation;
  status: string | null;
  deploy_status: string | null;
}

interface DomainOverride {
  hostname: string;
  cname_target: string | null;
  project: string | null;
  env: string | null;
  service: string | null;
  created_at: string | null;
}

interface SystemServedBy {
  kind: "cluster" | "node" | "local";
  name: string;
}

interface SystemTunnel {
  apex: string;
  wildcard: string;
  cname_target: string;
  tunnel_id: string;
  tunnel_name: string | null;
  served_by: SystemServedBy;
}

interface SystemHostname {
  hostname: string;
  kind: string;
  label: string;
  served_by: SystemServedBy;
}

interface SystemBlock {
  tunnel: SystemTunnel | null;
  hostnames: SystemHostname[];
}

interface DomainUsage {
  id: number;
  name: string;
  is_primary: boolean;
  cloudflare_routed: boolean;
  zone_status: string;
  name_servers: string[];
  connections: DomainConnection[];
  dns_overrides: DomainOverride[];
  system?: SystemBlock;
}

function statusBadge(c: DomainConnection) {
  const s = c.status ?? c.deploy_status;
  if (!s) return <span className="badge muted plain">not deployed</span>;
  if (s === "running") return <span className="badge ok">running</span>;
  if (["failed", "error", "unreachable", "blocked"].includes(s))
    return <span className="badge fail">{s}</span>;
  if (["stopped", "superseded"].includes(s))
    return <span className="badge muted">{s}</span>;
  return <span className="badge info">{s}</span>;
}

function locationBadge(loc: DomainLocation) {
  if (loc.kind === "cloud") return <span className="badge info plain">{loc.name}</span>;
  if (loc.kind === "local") return <span className="badge plain">{loc.name}</span>;
  return <span className="badge plain" title={loc.id ?? undefined}>{loc.name}</span>;
}

function servedByBadge(s: SystemServedBy) {
  return <span className="badge plain">{s.name}</span>;
}

export function DomainDetail() {
  const { domainId } = useParams();

  const { data: d } = useQuery<DomainUsage>({
    queryKey: ["domain-usage", domainId],
    queryFn: () => api.get<DomainUsage>(`/api/domains/${domainId}`),
    refetchInterval: 15000,
  });

  if (!d) return <span className="spinner" />;

  const pending = d.zone_status === "pending";

  return (
    <>
      <div className="row">
        <Link to="/domains" className="back-btn" aria-label="Back to domains" title="Back to domains">
          <ArrowLeft size={18} />
        </Link>
        <h1 style={{ margin: 0 }}>{d.name}</h1>
        {d.is_primary && <span className="badge ok plain">Primary</span>}
        {pending
          ? <span className="badge warn">Pending nameservers</span>
          : d.cloudflare_routed
            ? <span className="badge ok">Routed</span>
            : <span className="badge muted plain">Manual DNS</span>}
        <div className="spacer" />
        <a className="btn small ghost" href={`https://${d.name}`} target="_blank" rel="noopener">
          <ExternalLink size={13} /> Open
        </a>
      </div>

      {pending && d.name_servers.length > 0 && (
        <div className="card" style={{ marginTop: "1rem" }}>
          <div className="row">
            <span className="dim">
              Waiting on registrar nameservers. Set these at your registrar and routing
              finishes automatically:
            </span>
            {d.name_servers.map(ns => <code key={ns}>{ns}</code>)}
          </div>
        </div>
      )}

      {d.system && (d.system.tunnel || d.system.hostnames.length > 0) && (
        <>
          <h2 style={{ marginTop: "1.5rem" }}>Homebox routing</h2>
          <table className="data-table">
            <thead>
              <tr><th>Route</th><th>Serves</th><th>Runs on</th></tr>
            </thead>
            <tbody>
              {d.system.tunnel && (
                <tr>
                  <td>
                    <strong>{d.system.tunnel.apex}</strong>
                    {" "}and{" "}
                    <strong>{d.system.tunnel.wildcard}</strong>
                    <div className="dim">→ <code>{d.system.tunnel.cname_target}</code></div>
                  </td>
                  <td>
                    Cloudflare Tunnel
                    {d.system.tunnel.tunnel_name && <> <code>{d.system.tunnel.tunnel_name}</code></>}
                  </td>
                  <td>{servedByBadge(d.system.tunnel.served_by)}</td>
                </tr>
              )}
              {d.system.hostnames.map(h => (
                <tr key={h.hostname}>
                  <td>
                    <a href={`https://${h.hostname}`} target="_blank" rel="noopener">
                      <strong>{h.hostname}</strong>
                    </a>
                  </td>
                  <td>{h.label}</td>
                  <td>{servedByBadge(h.served_by)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      <h2 style={{ marginTop: "1.5rem" }}>Served on this domain</h2>
      {d.connections.length === 0 ? (
        <div className="empty-state">
          <Globe size={22} className="dim" aria-hidden />
          <h3>Nothing routed here yet</h3>
          <p>
            {d.is_primary
              ? "Public services of projects without their own domain will appear here once deployed."
              : "Assign this domain to a project or environment to serve it here."}
          </p>
        </div>
      ) : (
        <table className="data-table">
          <thead>
            <tr><th>Hostname</th><th>Service</th><th>Environment</th><th>Runs on</th><th>Status</th></tr>
          </thead>
          <tbody>
            {d.connections.map(c => (
              <tr key={`${c.hostname}${c.path ?? ""}-${c.service_id}-${c.environment_id}`}>
                <td>
                  <a href={c.url} target="_blank" rel="noopener">
                    <strong>{c.hostname}</strong>{c.path && <span className="dim">{c.path}</span>}
                  </a>
                </td>
                <td>
                  <Link to={`/projects/${c.project_id}/services/${c.service_id}`}>
                    {c.project_name} / {c.service_name}
                  </Link>{" "}
                  <span className="badge muted plain">{c.service_kind}</span>
                </td>
                <td>
                  <Link to={`/projects/${c.project_id}?env=${c.environment_id}`}>
                    {c.environment_name}
                  </Link>
                </td>
                <td>{locationBadge(c.location)}</td>
                <td>{statusBadge(c)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {d.dns_overrides.length > 0 && (
        <>
          <h2 style={{ marginTop: "1.5rem" }}>Per-host DNS records</h2>
          <p className="dim" style={{ marginTop: "0.25rem" }}>
            This install wrote these specific-host records because the domain's wildcard
            points at another cluster's tunnel. They are cleaned up automatically on
            teardown or retarget.
          </p>
          <table className="data-table">
            <thead><tr><th>Hostname</th><th>Points at</th><th>For</th></tr></thead>
            <tbody>
              {d.dns_overrides.map(o => (
                <tr key={o.hostname}>
                  <td><code>{o.hostname}</code></td>
                  <td><code>{o.cname_target ?? "—"}</code></td>
                  <td className="dim">
                    {[o.project, o.env, o.service].filter(Boolean).join(" / ") || "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </>
  );
}
