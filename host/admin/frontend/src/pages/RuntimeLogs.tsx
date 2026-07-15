import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import { api } from "../lib/api";
import { LogView } from "./DeploymentDetail";
import type { EnvironmentInfo } from "../lib/types";

/**
 * Runtime logs for an environment's stack — reached by clicking the status
 * badge on the environment card. Deploys only track up to container START,
 * so a service that crash-loops right after looks "Succeeded" in history;
 * this view shows live docker state per container with the log tail that
 * explains a Down/crashing app. Renders inside the ProjectDetail chrome.
 */

interface RuntimeContainer {
  name: string;
  service: string;
  status: string;                            // raw docker status line
  state: "running" | "restarting" | "exited";
  logs: string;
}
interface RuntimeLogsData {
  stack: string;
  containers: RuntimeContainer[];
}

function stateBadge(c: RuntimeContainer) {
  if (c.state === "restarting") return <span className="badge fail">Crash-looping</span>;
  if (c.state === "running") return <span className="badge ok">Up</span>;
  return <span className="badge muted">Exited</span>;
}

export function RuntimeLogsPanel({ projectId, env }: { projectId: number; env: EnvironmentInfo }) {
  const { data, isError, error } = useQuery<RuntimeLogsData>({
    queryKey: ["runtime-logs", projectId, env.id],
    queryFn: () => api.get<RuntimeLogsData>(`/api/projects/${projectId}/environments/${env.id}/runtime-logs`),
    refetchInterval: 4000,
  });

  return (
    <>
      <div className="row">
        <Link
          to={`/projects/${projectId}?env=${env.id}`}
          className="back-btn" aria-label="Back to overview" title="Back to overview"
        >
          <ArrowLeft size={18} />
        </Link>
        <h2 style={{ margin: 0 }}>Runtime logs</h2>
        <span className="dim" style={{ textTransform: "capitalize" }}>{env.name}</span>
        {data && <span className="dim">· {data.stack}</span>}
      </div>

      {isError ? (
        <div className="card" style={{ marginTop: "0.75rem" }}>
          <span className="dim">{String(error)}</span>
        </div>
      ) : !data ? (
        <span className="spinner" />
      ) : data.containers.length === 0 ? (
        <div className="card" style={{ marginTop: "0.75rem" }}>
          <span className="dim">No containers for this environment — deploy it first.</span>
        </div>
      ) : (
        data.containers.map(c => (
          <div key={c.name} className="card" style={{ marginTop: "0.75rem" }}>
            <div className="row" style={{ gap: "0.5rem", flexWrap: "wrap" }}>
              {stateBadge(c)}
              <strong>{c.service}</strong>
              <span className="dim">{c.name}</span>
              <span className="spacer" />
              <span className="dim">{c.status}</span>
            </div>
            {c.logs.trim()
              ? <LogView text={c.logs} maxHeight="34vh" />
              : <p className="dim" style={{ margin: "0.5rem 0 0" }}>No log output.</p>}
          </div>
        ))
      )}
    </>
  );
}
