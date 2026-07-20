import {
  Box, Braces, CircleCheck, CircleHelp, CircleX, Cog, Database,
  FileCode2, HardDrive, Monitor, RefreshCw, ServerCog,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

const KIND_ICONS: Record<string, LucideIcon> = {
  database: Database,
  cache: HardDrive,
  api: Braces,
  web: Monitor,
  ui: Monitor,
  static: FileCode2,
  worker: Cog,
  other: Box,
};

export function ServiceIcon({ kind, size = 17 }: { kind: string; size?: number }) {
  const Icon = KIND_ICONS[kind] ?? ServerCog;
  return <Icon size={size} className="service-kind-icon" aria-label={`${kind} service`} />;
}

export function ServiceStatus({ status }: { status: string | null | undefined }) {
  const normalized = (status || "not deployed").toLowerCase();
  if (["running", "up", "healthy"].includes(normalized)) {
    return <span className="service-status up" title="Up"><CircleCheck size={15} /> Up</span>;
  }
  if (normalized.includes("restart") || normalized.includes("start")) {
    return <span className="service-status restarting" title="Restarting"><RefreshCw size={15} /> Restarting</span>;
  }
  if (["unreachable", "down", "failed", "exited", "dead"].some(s => normalized.includes(s))) {
    return <span className="service-status down" title={status || "Down"}><CircleX size={15} /> Down</span>;
  }
  return <span className="service-status unknown" title={status || "Not deployed"}><CircleHelp size={15} /> {status || "Not deployed"}</span>;
}
