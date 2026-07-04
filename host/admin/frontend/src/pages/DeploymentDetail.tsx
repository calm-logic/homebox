import { useEffect, useRef } from "react";
import { Link, Navigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import { api } from "../lib/api";
import { BUSY, historyBadge, utcDate } from "./ProjectDetail";
import type { DeploymentDetailData } from "../lib/types";

/**
 * One deployment: metadata + the full log. Polls every 2s while the deploy is
 * still active so the log tails in near-realtime; stops once it's terminal.
 */

// docker/compose runs without a TTY, so there's no ANSI color in the log —
// classify lines by content instead. Strip any stray escape codes first.
const ANSI_RE = /\x1b\[[0-9;]*m/g;

function logLineClass(line: string): string {
  if (/^\$ /.test(line)) return "log-cmd";
  if (/\b(error|failed|failure|fatal|panic|traceback|exit code: [1-9])\b/i.test(line)
      && !/\b0 errors?\b/i.test(line)) return "log-err";
  if (/\b(warn|warning|deprecated)\b/i.test(line)) return "log-warn";
  if (/\b(DONE|CACHED|FINISHED)\b/.test(line)
      || /\b(Started|Created|Built|Healthy|Running|Pulled)\s*$/.test(line.trimEnd())
      || /✓/.test(line)) return "log-ok";
  if (/^#\d+ /.test(line) || /^Step \d+\/\d+/.test(line) || /^\s*=>/.test(line)
      || /^\s*(Container|Network|Volume|Image)\s/.test(line)) return "log-step";
  return "";
}

function LogView({ text, innerRef }: { text: string; innerRef: React.RefObject<HTMLPreElement> }) {
  const lines = text.replace(ANSI_RE, "").split("\n");
  return (
    <pre ref={innerRef} className="log-view" style={{ maxHeight: "60vh", overflow: "auto", marginTop: "0.25rem" }}>
      {lines.map((l, i) => {
        const cls = logLineClass(l);
        const content = l + (i < lines.length - 1 ? "\n" : "");
        return cls ? <span key={i} className={cls}>{content}</span> : content;
      })}
    </pre>
  );
}
export function DeploymentDetail() {
  const { projectId, deploymentId } = useParams();
  const pid = Number(projectId);
  const did = Number(deploymentId);
  const logRef = useRef<HTMLPreElement>(null);

  const { data: dep, isError } = useQuery<DeploymentDetailData>({
    queryKey: ["deployment", did],
    queryFn: () => api.get<DeploymentDetailData>(`/api/projects/${pid}/deployments/${did}`),
    refetchInterval: (q) => {
      const s = q.state.data?.status;
      if (!s) return false;
      if (BUSY.includes(s)) return 2000;             // live build → tail the log
      if (s.startsWith("pending")) return 5000;      // waiting on checks/promotion
      return false;
    },
  });

  const busy = !!dep && BUSY.includes(dep.status);

  // Keep the log pinned to the bottom while it's streaming.
  useEffect(() => {
    if (busy && logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [dep?.log_tail, busy]);

  if (isError) return <Navigate to={`/projects/${pid}`} replace />;
  if (!dep) return <span className="spinner" />;

  return (
    <>
      <Link to={`/projects/${pid}`} className="dim" style={{ display: "inline-flex", alignItems: "center", gap: "0.3rem" }}>
        <ArrowLeft size={14} /> Project
      </Link>

      <div className="row" style={{ marginTop: "0.5rem" }}>
        <h1 style={{ margin: 0 }}>Deploy #{dep.id}</h1>
        {historyBadge(dep.status)}
        {busy && <span className="spinner" />}
      </div>
      <p className="dim" style={{ marginTop: "0.35rem" }}>
        <span style={{ textTransform: "capitalize" }}>{dep.environment.name}</span>
        {dep.commit_sha && <> · commit <code>{dep.commit_sha.slice(0, 7)}</code></>}
        {" "}· <span style={{ textTransform: "capitalize" }}>{dep.trigger}</span>
        {dep.created_at && <> · started {utcDate(dep.created_at).toLocaleString()}</>}
        {dep.updated_at && !busy && <> · last update {utcDate(dep.updated_at).toLocaleString()}</>}
      </p>

      {dep.error && (
        <div className="card" style={{ marginTop: "1rem", borderColor: "var(--danger)" }}>
          <div className="lbl" style={{ color: "var(--danger)", marginBottom: "0.4rem" }}>Error</div>
          <pre style={{ margin: 0, border: "none", padding: 0, background: "transparent" }}>{dep.error}</pre>
        </div>
      )}

      <h3>Log</h3>
      {dep.log_tail ? (
        <LogView text={dep.log_tail} innerRef={logRef} />
      ) : (
        <div className="card" style={{ marginTop: "0.25rem" }}>
          <span className="dim">
            {busy ? "Waiting for build output…" : "No log was captured for this deployment."}
          </span>
        </div>
      )}
    </>
  );
}
