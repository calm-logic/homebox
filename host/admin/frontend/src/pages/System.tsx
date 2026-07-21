import { FormEvent, ReactNode, useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import {
  ChevronRight, Cloud, Copy, Crown, ExternalLink, Github, LogIn, Network, Power, PowerOff,
  RefreshCw, ShieldAlert, Split, Ticket, Unplug, UserMinus,
} from "lucide-react";
import { api, ApiError } from "../lib/api";
import { AccountAuthModal } from "../components/AccountAuthModal";
import { Modal } from "../components/Modal";
import PageHelp from "../components/PageHelp";
import { Topology } from "../components/Topology";
import { useToast } from "../lib/toast";
import { timeAgo } from "../lib/time";
import type {
  AccountStatus, AccountTopology, ClusterNode, IntegrationItem, NodeRole, UptimeReport, UptimeStatus,
} from "../lib/types";

const PRICING_URL = "https://homebox.sh/cloud";

/** Small inline Google "G" mark — mirrors the one used on the login screen,
 *  duplicated locally since it isn't exported from Login.tsx. */
function GoogleIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 18 18" aria-hidden focusable="false">
      <path fill="#4285F4" d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.92c1.7-1.57 2.68-3.88 2.68-6.62z" />
      <path fill="#34A853" d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.92-2.26c-.8.54-1.84.86-3.04.86-2.34 0-4.32-1.58-5.02-3.7H.96v2.34A9 9 0 0 0 9 18z" />
      <path fill="#FBBC05" d="M3.98 10.72a5.4 5.4 0 0 1 0-3.44V4.94H.96a9 9 0 0 0 0 8.12l3.02-2.34z" />
      <path fill="#EA4335" d="M9 3.58c1.32 0 2.5.46 3.44 1.35l2.58-2.58A9 9 0 0 0 .96 4.94l3.02 2.34C4.68 5.16 6.66 3.58 9 3.58z" />
    </svg>
  );
}

/**
 * System: health of the Homebox infrastructure, plus clustering. Not
 * clustered → this node's own uptime monitoring, with an option to join a
 * cluster (via a homebox.sh account or a manual join token). Clustered →
 * the health of every node in the cluster, with this node's own uptime
 * detail kept below it.
 */

type ClusterLicense = {
  valid: boolean;
  plan: string;              // "free" | "premium" | "dev" (or others, defensively rendered as-is)
  max_nodes: number;
  node_count: number;
  features?: string[];       // e.g. ["cluster", "cloud-mirror"]
  expires_at?: string | null;
  in_grace?: boolean;
  expired?: boolean;
  verified?: boolean;
};

type MirrorState = "none" | "pending" | "provisioning" | "active" | "failed" | "decommissioning" | "decommissioned";
const MIRROR_TRANSITIONAL: MirrorState[] = ["pending", "provisioning", "decommissioning"];

type MirrorStatus = {
  status: MirrorState;
  node_id?: string;
};

type ClusterStatus = {
  active: boolean;
  node_id: string;
  cluster_id?: string;
  name?: string;
  roster?: ClusterNode[];
  license?: ClusterLicense;
  initial_sync_done?: boolean;
  last_heartbeat?: string | null;
  last_sync_at?: string | null;
  control_plane_url?: string;
  node_role?: NodeRole;
  account_linked?: boolean;
  mirror?: MirrorStatus | null;
};

function planLabel(plan?: string): string {
  if (!plan) return "Free";
  return plan.charAt(0).toUpperCase() + plan.slice(1);
}

// Free plan, or a license that reports feature flags without "cluster",
// blocks creating/joining a cluster. Missing `features` entirely (older
// control planes) is treated as unrestricted so this degrades gracefully.
function clusterIsLocked(license?: ClusterLicense): boolean {
  if (!license) return false;
  if (license.plan === "free") return true;
  if (license.features && !license.features.includes("cluster")) return true;
  return false;
}

function mirrorIsLocked(license?: ClusterLicense): boolean {
  if (!license?.features) return false;
  return !license.features.includes("cloud-mirror");
}

/** "Homebox Premium" upgrade callout — used for both cluster and cloud-mirror gating.
 *  Its primary action depends on whether a homebox.sh account is linked yet:
 *  unlinked accounts need to connect first, linked accounts can upgrade directly. */
function PremiumCallout({
  pitch, onUpgrade, pending, accountLinked, onConnectAccount,
}: {
  pitch: string;
  onUpgrade: () => void;
  pending: boolean;
  accountLinked: boolean;
  onConnectAccount: () => void;
}) {
  return (
    <div className="card premium-callout">
      <h3><Crown size={16} /> Homebox Premium</h3>
      <p className="dim" style={{ marginTop: "0.4rem" }}>{pitch}</p>
      <div className="row" style={{ marginTop: "0.85rem" }}>
        {accountLinked ? (
          <button className="btn primary" onClick={onUpgrade} disabled={pending}>
            {pending ? <span className="spinner" /> : <>Upgrade at homebox.sh</>}
          </button>
        ) : (
          <button className="btn primary" onClick={onConnectAccount}>
            <LogIn size={14} /> Connect account
          </button>
        )}
        <a className="btn ghost" href={PRICING_URL} target="_blank" rel="noreferrer">
          See cloud <ExternalLink size={13} />
        </a>
      </div>
    </div>
  );
}

/** Cloud Mirror section on the System page — visible whenever a cluster is active. */
function CloudMirrorCard({
  mirror, locked, onEnable, enablePending, onUpgrade, upgradePending, onOpenDisableConfirm,
  accountLinked, onConnectAccount,
}: {
  mirror?: MirrorStatus;
  locked: boolean;
  onEnable: () => void;
  enablePending: boolean;
  onUpgrade: () => void;
  upgradePending: boolean;
  onOpenDisableConfirm: () => void;
  accountLinked: boolean;
  onConnectAccount: () => void;
}) {
  const state: MirrorState = mirror?.status ?? "none";

  if (state === "none" || state === "decommissioned") {
    // Cloud Mirror is being reworked to run off a destination target
    // (destination = homebox cloud) instead of this just-in-time "enable a
    // standby" flow, so there's no enable affordance here anymore. Clusters
    // with an existing mirror still surface its status via the states below.
    return null;
  }

  if (state === "pending" || state === "provisioning") {
    return (
      <div className="card">
        <div className="row"><span className="spinner" /> Provisioning your cloud mirror…</div>
      </div>
    );
  }

  if (state === "decommissioning") {
    return (
      <div className="card">
        <div className="row"><span className="spinner" /> Removing your cloud mirror…</div>
      </div>
    );
  }

  if (state === "failed") {
    return (
      <div className="card">
        <div className="card-row">
          <span className="badge fail">Cloud mirror failed to provision</span>
          <button className="btn ghost" onClick={onEnable} disabled={enablePending}>
            {enablePending ? <span className="spinner" /> : <><RefreshCw size={14} /> Retry</>}
          </button>
        </div>
      </div>
    );
  }

  // active
  return (
    <div className="card">
      <div className="card-row">
        <div className="row">
          <span className="badge ok"><Cloud size={12} /> Cloud mirror active</span>
          {mirror?.node_id && <span className="dim">{mirror.node_id}</span>}
        </div>
        <button className="btn ghost danger" onClick={onOpenDisableConfirm}>Disable</button>
      </div>
    </div>
  );
}

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

/** One node's infrastructure health (uptime + latency per component). Only
 *  this node exposes a local uptime endpoint; a remote node's health is
 *  reported through the control plane, so its panel shows a short note until
 *  that lands. */
function NodeHealthDetail({ isSelf }: { isSelf: boolean }) {
  const [window, setWindow] = useState("24h");
  const { data } = useQuery<UptimeReport>({
    queryKey: ["tunnel-uptime", window],
    queryFn: () => api.get<UptimeReport>(`/api/tunnel/uptime?window=${window}`),
    refetchInterval: 5000,
    enabled: isSelf,
  });

  if (!isSelf) {
    return (
      <p className="dim" style={{ margin: "0.5rem 0 0.2rem" }}>
        Live health for this node is reported through the control plane.
      </p>
    );
  }

  return (
    <div style={{ marginTop: "0.5rem" }}>
      <div className="mode-chips" style={{ marginBottom: "0.65rem" }}>
        {UPTIME_WINDOWS.map((w) => (
          <span key={w} className={`chip ${w === window ? "active" : ""}`} onClick={() => setWindow(w)}>
            {w}
          </span>
        ))}
      </div>
      {!data ? (
        <span className="spinner" />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: "0.6rem" }}>
          {data.components.map((c) => (
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
          ))}
        </div>
      )}
      {data && data.components.every((c) => c.sample_count === 0) && (
        <div className="dim" style={{ marginTop: "0.6rem" }}>
          No samples yet — the monitor records one per component every 30 seconds.
        </div>
      )}
    </div>
  );
}

/** A node row whose infrastructure health is tucked into a collapsible panel.
 *  Used both for cluster roster rows and for the lone node when unclustered.
 *  `right` holds any per-node actions (Disable/Evict, or a mirror note). */
function NodeRow({ n, isSelf, right }: { n: ClusterNode; isSelf: boolean; right?: ReactNode }) {
  const [open, setOpen] = useState(false);
  const isMirror = n.role === "mirror";
  const serving = n.serving !== false;
  return (
    <div style={{ borderTop: "1px solid var(--border)" }}>
      <div
        className="row"
        style={{ justifyContent: "space-between", gap: "0.75rem", flexWrap: "wrap", padding: "0.55rem 0", cursor: "pointer" }}
        onClick={() => setOpen(o => !o)}
      >
        <div className="row" style={{ gap: "0.5rem", minWidth: 0 }}>
          <ChevronRight
            size={15}
            className="dim"
            style={{ transition: "transform 120ms", transform: open ? "rotate(90deg)" : "none", flexShrink: 0 }}
            aria-hidden
          />
          <span className={`badge ${n.online ? "ok" : "fail"}`}>{n.online ? "online" : "offline"}</span>
          {isMirror
            ? <span className="badge info"><Cloud size={12} /> Cloud Mirror</span>
            : <strong>{n.name || n.node_id}</strong>}
          {isSelf && <span className="chip active">this node</span>}
          {!isMirror && !serving && <span className="badge warn">disabled</span>}
        </div>
        {right && (
          <div className="row" style={{ gap: "0.75rem" }} onClick={e => e.stopPropagation()}>
            {right}
          </div>
        )}
      </div>
      {open && (
        <div style={{ padding: "0 0 0.75rem 1.4rem" }}>
          {n.peer_url && <div className="dim" style={{ fontSize: "0.85rem" }}>{n.peer_url}</div>}
          <NodeHealthDetail isSelf={isSelf} />
        </div>
      )}
    </div>
  );
}

export function System() {
  const qc = useQueryClient();
  const toast = useToast();
  const [confirmLeave, setConfirmLeave] = useState(false);
  const [stopTunnel, setStopTunnel] = useState(true);
  const [teardownStacks, setTeardownStacks] = useState(false);
  const [mintedToken, setMintedToken] = useState<string | null>(null);
  const [joining, setJoining] = useState(false);
  const [showManualJoin, setShowManualJoin] = useState(false);
  const [upgradeNotice, setUpgradeNotice] = useState<string | null>(null);
  const [confirmDisableMirror, setConfirmDisableMirror] = useState(false);
  // "Split off" — this node leaves its cluster and immediately founds a new one.
  const [confirmSplit, setConfirmSplit] = useState(false);
  const [splitName, setSplitName] = useState("home");
  // Inline account-auth modal (AccountAuthModal): the fallback when the
  // silent link (stored provider token) isn't possible — provider popup
  // buttons + a manual account-token paste.
  const [connectModalOpen, setConnectModalOpen] = useState(false);
  const [connectError, setConnectError] = useState<string | null>(null);
  // Account modal behind the one-word Linked/Unlinked pill in the page header.
  const [accountModalOpen, setAccountModalOpen] = useState(false);
  // account link form
  const [accountToken, setAccountToken] = useState("");
  const [nodeName, setNodeName] = useState("");
  const [peerUrl, setPeerUrl] = useState("");
  const [cpUrl, setCpUrl] = useState("");
  // manual join
  const [joinToken, setJoinToken] = useState("");
  // "Advanced" disclosure on the unlinked hero card (manual token flows).
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [params] = useSearchParams();

  const { data: status } = useQuery<ClusterStatus>({
    queryKey: ["cluster"],
    queryFn: () => api.get<ClusterStatus>("/api/cluster"),
    // Poll faster while a mirror provision/teardown is in flight so the UI
    // updates without a manual refresh.
    refetchInterval: (q) => {
      if (joining) return 3000;
      const mirrorStatus = q.state.data?.mirror?.status;
      if (mirrorStatus && MIRROR_TRANSITIONAL.includes(mirrorStatus)) return 10000;
      return 15000;
    },
    retry: true,
  });
  const { data: account } = useQuery<AccountStatus>({
    queryKey: ["cluster-account"],
    queryFn: () => api.get<AccountStatus>("/api/cluster/account"),
    refetchInterval: 20000,
    retry: true,
  });
  // The fleet god view — every cluster and node on the account, plus pending
  // directives/provisions. 412 = not linked (the query is gated off then).
  const { data: topology } = useQuery<AccountTopology>({
    queryKey: ["account-topology"],
    queryFn: () => api.get<AccountTopology>("/api/cluster/account/topology"),
    enabled: !!account?.linked,
    refetchInterval: 10000,
    retry: (count, err) =>
      !(err instanceof ApiError && (err.status === 412 || err.status === 402)) && count < 2,
  });
  // Linked integrations, for the "Add node on AWS/GCP" provider picker.
  const { data: integrations } = useQuery<IntegrationItem[]>({
    queryKey: ["integrations"],
    queryFn: () => api.get<IntegrationItem[]>("/api/integrations"),
    enabled: !!account?.linked,
    staleTime: 60000,
  });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["cluster"] });
    qc.invalidateQueries({ queryKey: ["cluster-account"] });
    qc.invalidateQueries({ queryKey: ["account-topology"] });
  };
  const onErr = (e: unknown) => toast.show(String(e), "fail");
  // create/join/join-token/invite mutations can 402 once a plan/node-count
  // limit is hit — surface the server's own detail inline with an Upgrade
  // button instead of a generic toast.
  const onErrOrUpgrade = (e: unknown) => {
    if (e instanceof ApiError && e.status === 402) { setUpgradeNotice(e.message); return; }
    onErr(e);
  };

  const link = useMutation({
    mutationFn: () => api.post("/api/cluster/account/link", {
      account_token: accountToken, node_name: nodeName, peer_url: peerUrl,
      control_plane_url: cpUrl.trim() || null,
    }),
    onSuccess: () => {
      invalidate();
      toast.show("Signed in — this node is now linked", "ok");
      setAccountToken("");
      setConnectModalOpen(false);
    },
    onError: onErr,
  });
  // Silent account link (G4): re-auth from a provider token stored during an
  // earlier OAuth login/link — no popup, no paste. 412 = nothing usable is
  // stored → fall back to the inline AccountAuthModal (G4b).
  const linkSilent = useMutation({
    mutationFn: (provider?: "github" | "google") =>
      api.post("/api/cluster/account/link-silent", provider ? { provider } : {}),
    onSuccess: () => {
      invalidate();
      toast.show("Account linked — syncing this box from your cloud vault", "ok");
    },
    onError: (e) => {
      // Nothing stored to re-auth silently — hand off to the provider modal
      // (and close the account modal so the two don't stack).
      if (e instanceof ApiError && e.status === 412) {
        setAccountModalOpen(false);
        setConnectModalOpen(true);
        return;
      }
      onErr(e);
    },
  });
  const unlink = useMutation({
    mutationFn: () => api.del("/api/cluster/account"),
    onSuccess: () => { invalidate(); toast.show("Unlinked from account", "ok"); },
    onError: onErr,
  });
  const refresh = useMutation({
    mutationFn: () => api.post("/api/cluster/account/refresh"),
    onSuccess: () => { invalidate(); },
    onError: onErr,
  });
  const createCluster = useMutation({
    mutationFn: (name: string) => api.post("/api/cluster/account/create-cluster", { name }),
    onSuccess: () => { invalidate(); toast.show("Cluster created — this node is the seed", "ok"); },
    onError: onErrOrUpgrade,
  });
  const joinCluster = useMutation({
    mutationFn: (cluster_id: string) => api.post("/api/cluster/account/join", { cluster_id }),
    onSuccess: () => { setJoining(true); toast.show("Joining — restarting onto the cluster keys…", "ok"); },
    onError: onErrOrUpgrade,
  });
  const inviteNode = useMutation({
    mutationFn: (node_id: string) => api.post("/api/cluster/account/invite", { node_id }),
    onSuccess: (_d, node_id) => toast.show(`Invited ${node_id} — it joins automatically within a minute`, "ok"),
    onError: onErrOrUpgrade,
  });
  const evict = useMutation({
    mutationFn: (node_id: string) => api.post("/api/cluster/evict", { node_id }),
    onSuccess: () => { invalidate(); toast.show("Node evicted — replication links are being cleaned up", "ok"); },
    onError: onErr,
  });
  const setServing = useMutation({
    mutationFn: (v: { node_id: string; serving: boolean }) => api.post("/api/cluster/node/serving", v),
    // Optimistic so the row reflects the change the instant you click — the
    // button flips and the "disabled" badge appears without a reload — then
    // onSettled reconciles with the server's real roster state.
    onMutate: async (v) => {
      await qc.cancelQueries({ queryKey: ["cluster"] });
      const prev = qc.getQueryData<ClusterStatus>(["cluster"]);
      qc.setQueryData<ClusterStatus>(["cluster"], (old) =>
        old
          ? { ...old, roster: (old.roster ?? []).map(n => n.node_id === v.node_id ? { ...n, serving: v.serving } : n) }
          : old,
      );
      return { prev };
    },
    onSuccess: (_d, v) => {
      toast.show(
        v.serving ? "Node enabled — resuming app traffic" : "Node disabled — app traffic draining to peers",
        "ok",
      );
    },
    onError: (e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(["cluster"], ctx.prev);  // roll the optimistic flip back (e.g. last-node guard 409)
      onErr(e);
    },
    onSettled: () => invalidate(),
  });
  const manualJoin = useMutation({
    mutationFn: () => api.post("/api/cluster/join", {
      join_token: joinToken, peer_url: peerUrl, node_name: nodeName,
      control_plane_url: cpUrl.trim() || null,
    }),
    onSuccess: () => { setJoining(true); toast.show("Joined — restarting onto the cluster keys…", "ok"); },
    onError: onErrOrUpgrade,
  });
  const mint = useMutation({
    mutationFn: () => api.post<{ join_token: string }>("/api/cluster/join-token"),
    onSuccess: (d) => setMintedToken(d.join_token),
    onError: onErrOrUpgrade,
  });
  const sync = useMutation({
    mutationFn: () => api.post("/api/cluster/sync"),
    onSuccess: () => { invalidate(); toast.show("Sync triggered", "ok"); },
    onError: onErr,
  });
  const leave = useMutation({
    mutationFn: () => api.post("/api/cluster/leave", { stop_tunnel: stopTunnel, teardown_stacks: teardownStacks }),
    onSuccess: () => { invalidate(); setConfirmLeave(false); toast.show("Left the cluster (disconnected)", "ok"); },
    onError: onErr,
  });
  const split = useMutation({
    mutationFn: (name: string) =>
      api.post<{ ok: boolean; cluster_id: string; name: string }>("/api/cluster/split", { name: name.trim() }),
    onSuccess: (d) => {
      setConfirmSplit(false);
      invalidate();
      toast.show(`Split off — this node now founds the "${d.name}" cluster`, "ok");
    },
    onError: (e) => {
      // Plan-limit 402s surface in the upgrade banner behind the modal — close
      // it so the banner is readable; other errors keep the modal open to retry.
      if (e instanceof ApiError && e.status === 402) setConfirmSplit(false);
      onErrOrUpgrade(e);
    },
  });
  const upgrade = useMutation({
    mutationFn: () => api.post<{ url: string }>("/api/cluster/upgrade"),
    onSuccess: (d) => { window.open(d.url, "_blank"); },
    onError: onErr,
  });
  const mirrorEnable = useMutation({
    mutationFn: () => api.post("/api/cluster/mirror"),
    onSuccess: () => { invalidate(); toast.show("Cloud mirror requested — provisioning…", "ok"); },
    onError: onErrOrUpgrade,
  });
  const mirrorDisable = useMutation({
    mutationFn: () => api.del("/api/cluster/mirror"),
    onSuccess: () => { invalidate(); setConfirmDisableMirror(false); toast.show("Cloud mirror disabled — tearing down the standby", "ok"); },
    onError: onErr,
  });
  // Remote node ops from the god view ride the control plane's directive
  // queue — the target node polls, executes locally, and acks.
  const directive = useMutation({
    mutationFn: (v: { node_id: string; type: "set_serving" | "split_off" | "split_cluster"; payload?: Record<string, unknown> }) =>
      api.post("/api/cluster/account/directives", { node_id: v.node_id, type: v.type, payload: v.payload ?? {} }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["account-topology"] });
      toast.show("Queued — the node applies it within a minute", "ok");
    },
    onError: onErrOrUpgrade,
  });
  // "Add node on AWS/GCP" — boots a VM on the user's own cloud account that
  // installs Homebox and joins this cluster automatically.
  const provision = useMutation({
    mutationFn: (v: { name: string; provider: "aws" | "gcp"; integration_id: number; region: string; machine?: string }) =>
      api.post("/api/cluster/account/nodes/provision", v),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["account-topology"] });
      toast.show("Provisioning a cloud node — it joins automatically when ready", "ok");
    },
    onError: onErrOrUpgrade,
  });
  const cancelProvision = useMutation({
    mutationFn: (id: string | number) => api.del(`/api/cluster/account/nodes/provision/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["account-topology"] });
      toast.show("Provision removed", "ok");
    },
    onError: onErr,
  });
  // Inline cluster rename (click the name in the god view). Optimistic: the
  // topology cache is patched immediately so the title never flickers, then
  // reverted with a toast if the control plane rejects it.
  const renameCluster = useMutation({
    mutationFn: (v: { cluster_id: string; name: string }) =>
      api.patch<{ ok: boolean; name: string }>(
        `/api/cluster/account/clusters/${encodeURIComponent(v.cluster_id)}/name`,
        { name: v.name },
      ),
    onMutate: async (v) => {
      await qc.cancelQueries({ queryKey: ["account-topology"] });
      const prev = qc.getQueryData<AccountTopology>(["account-topology"]);
      qc.setQueryData<AccountTopology>(["account-topology"], (old) =>
        old
          ? { ...old, clusters: (old.clusters ?? []).map(c => c.cluster_id === v.cluster_id ? { ...c, name: v.name } : c) }
          : old,
      );
      return { prev };
    },
    onError: (e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(["account-topology"], ctx.prev);
      onErr(e);
    },
    // Reconcile with the server-trimmed name, and refresh this node's own
    // cluster status (its name mirrors there).
    onSettled: () => invalidate(),
  });

  // The OAuth popup posts a message here right before it closes itself (see
  // the mount effect below) so we refetch immediately instead of waiting for
  // the next poll, and so we can surface an error inline. (AccountAuthModal
  // has its own listener while it's open; this one covers the page itself.)
  useEffect(() => {
    function onMessage(e: MessageEvent) {
      if (e.origin !== window.location.origin) return;
      if (!e.data || e.data.type !== "homebox-account") return;
      invalidate();
      if (e.data.error) setConnectError(String(e.data.error));
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // On mount: if this window is the OAuth popup landing on
  // /system?account=linked|account_error, hand off to the opener and close.
  // If there's no opener (e.g. the link was opened directly), show the
  // result in place instead and strip the query params.
  useEffect(() => {
    const linked = params.get("account") === "linked";
    const err = params.get("account_error");
    if (!linked && !err) return;
    if (window.opener) {
      try {
        window.opener.postMessage({ type: "homebox-account", linked, error: err || undefined }, window.location.origin);
      } catch { /* opener gone or cross-origin — nothing more to do */ }
      window.close();
      return;
    }
    const url = new URL(window.location.href);
    url.searchParams.delete("account");
    url.searchParams.delete("account_error");
    window.history.replaceState({}, "", url.pathname + url.search + url.hash);
    if (linked) {
      invalidate();
      toast.show("Account connected", "ok");
    } else if (err) {
      setConnectError(err);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const license = status?.license;
  const clusterLocked = clusterIsLocked(license);
  const mirrorLocked = mirrorIsLocked(license);
  const mirror = status?.mirror ?? undefined;
  const mirrorServingRoster = (status?.roster ?? []).find(n => n.role === "mirror" && n.serving !== false);

  // When the account is linked and the topology god view is up AND shows this
  // node's cluster, Topology is the single cluster surface — the legacy roster
  // card would repeat the same cluster/license/nodes, so it hides and its
  // header actions/banners fold into the cluster block instead. Unlinked (or
  // topology still loading / 412ing / lagging behind a fresh cluster), the
  // legacy card renders exactly as before so token-joined setups lose nothing.
  const clusterInTopology =
    !!status?.active && (topology?.clusters ?? []).some(c => c.cluster_id === status.cluster_id);
  const showLegacyClusterCard = !!status?.active && !(account?.linked && clusterInTopology);

  // Shared fragments — rendered by the legacy card when it's visible, or
  // passed into Topology's this-cluster block when it isn't. One JSX source,
  // one set of mutations.
  const licenseBanner = license?.expired ? (
    <div className="banner danger">
      <span>Premium features paused — existing services keep running. Manage your plan to restore clustering.</span>
      <button className="btn primary small" onClick={() => upgrade.mutate()} disabled={upgrade.isPending}>
        {upgrade.isPending ? <span className="spinner" /> : <>Manage plan at homebox.sh</>}
      </button>
    </div>
  ) : license?.in_grace ? (
    <div className="banner warn">
      <span>License expired — running in a 14-day grace period. Manage your plan soon to keep clustering.</span>
      <button className="btn primary small" onClick={() => upgrade.mutate()} disabled={upgrade.isPending}>
        {upgrade.isPending ? <span className="spinner" /> : <>Manage plan at homebox.sh</>}
      </button>
    </div>
  ) : null;
  const mirrorServingBanner = mirrorServingRoster ? (
    <div className="banner info">
      <span><Cloud size={14} style={{ verticalAlign: "-2px", marginRight: "0.35rem" }} />
        Cloud mirror is serving your traffic
      </span>
    </div>
  ) : null;
  const mintedTokenRow = mintedToken ? (
    <div className="card-row">
      <code style={{ wordBreak: "break-all", userSelect: "all" }}>{mintedToken}</code>
      <button className="btn ghost"
        onClick={() => { navigator.clipboard.writeText(mintedToken); toast.show("Copied", "ok"); }}>
        <Copy size={14} /> Copy
      </button>
    </div>
  ) : null;

  // What the hidden legacy card contributed, re-homed inside Topology's
  // this-cluster block: license/mirror banners, a freshly minted join token,
  // and heartbeat/sync freshness (initial-sync pending included).
  const freshnessLine = status?.active &&
    (!status.initial_sync_done || status.last_heartbeat || status.last_sync_at) ? (
    <div className="row" style={{ gap: "0.75rem", flexWrap: "wrap" }}>
      {!status.initial_sync_done && <span className="badge warn">initial sync pending…</span>}
      {status.last_heartbeat && <span className="dim">heartbeat {timeAgo(status.last_heartbeat)}</span>}
      {status.last_sync_at && <span className="dim">synced {timeAgo(status.last_sync_at)}</span>}
    </div>
  ) : null;
  const thisClusterExtras =
    status?.active && !showLegacyClusterCard &&
    (licenseBanner || mirrorServingBanner || mintedTokenRow || freshnessLine) ? (
      <>
        {licenseBanner}
        {mirrorServingBanner}
        {mintedTokenRow}
        {freshnessLine}
      </>
    ) : undefined;

  function submitLink(e: FormEvent) {
    e.preventDefault();
    if (!peerUrl.trim()) { toast.show("Set this node's LAN address", "fail"); return; }
    link.mutate();
  }

  // Vault-sync freshness — a quiet reassurance line on the linked-account
  // card (ok = synced recently, warn = last push/pull failed). Falls back to
  // the legacy backup timestamps against an older backend.
  const vaultLine = (() => {
    const v = account?.vault;
    const err = v?.error ?? account?.backup?.error;
    if (err) {
      return (
        <div className="row" style={{ marginTop: "0.6rem", gap: "0.5rem" }}>
          <span className="badge warn">sync issue</span>
          <span className="dim">Cloud sync: {err}</span>
        </div>
      );
    }
    const synced = timeAgo(v?.pushed_at ?? v?.pulled_at ?? account?.backup?.pushed_at);
    return (
      <div className="row" style={{ marginTop: "0.6rem", gap: "0.5rem" }}>
        <span className={`badge ${synced ? "ok" : "plain"}`}>{synced ? "synced" : "pending"}</span>
        <span className="dim">{synced ? `Synced to cloud ${synced}` : "Cloud sync pending — first push happens within a minute"}</span>
      </div>
    );
  })();

  // The account modal behind the header pill handles BOTH states.
  // Linked → identity details (email · plan · node · vault freshness) with
  // Refresh/Unlink. Unlinked → the link-account flow that used to be the hero
  // card: every click tries the SILENT link first (a provider token stored
  // from an earlier OAuth login/link); only a 412 (nothing stored) opens the
  // inline AccountAuthModal with the popup/token fallbacks. Manual token
  // flows stay collapsed under "Advanced".
  const accountModalBody = !account ? (
    <span className="spinner" />
  ) : account.linked ? (
    <>
      <div className="row" style={{ gap: "0.5rem", flexWrap: "wrap" }}>
        <span className="badge success"><LogIn size={12} /> linked</span>
        {account.email && <strong>{account.email}</strong>}
        <span className="badge plain">{planLabel(account.plan ?? undefined)}</span>
      </div>
      <div className="dim" style={{ marginTop: "0.5rem" }}>
        as {account.node_name || "unnamed node"} · {account.peer_url}
      </div>
      {vaultLine}
      <div className="row" style={{ gap: "0.5rem", marginTop: "0.9rem" }}>
        <button className="btn ghost" onClick={() => refresh.mutate()} disabled={refresh.isPending}>
          <RefreshCw size={14} /> Refresh
        </button>
        <button className="btn ghost danger" onClick={() => unlink.mutate()}>Unlink</button>
      </div>
    </>
  ) : (
    <>
      <p className="dim" style={{ marginTop: 0 }}>
        Linking backs up and restores this node from your encrypted cloud vault, keeps every
        homebox on your account in sync, unlocks clustering, and gives you one view of your
        whole fleet — from anywhere.
      </p>
      {account.suggested ? (
        // The control plane recognized the admin's login identity — one-click
        // link that re-auths silently when possible (modal fallback on 412).
        <div className="row" style={{ marginTop: "0.85rem", gap: "0.5rem" }}>
          <button className="btn primary" onClick={() => linkSilent.mutate(account.suggested!.provider)}
            disabled={linkSilent.isPending}>
            {linkSilent.isPending
              ? <span className="spinner" />
              : account.suggested.provider === "github" ? <Github size={15} /> : <GoogleIcon size={15} />}
            Continue with {account.suggested.provider === "github" ? "GitHub" : "Google"} as {account.suggested.email}
          </button>
          <button className="btn ghost" onClick={() => { setAccountModalOpen(false); setConnectModalOpen(true); }}>
            Use a different account
          </button>
        </div>
      ) : (
        <div className="row" style={{ marginTop: "0.85rem", gap: "0.5rem" }}>
          <button className="btn primary" onClick={() => linkSilent.mutate(undefined)}
            disabled={linkSilent.isPending}>
            {linkSilent.isPending ? <span className="spinner" /> : <LogIn size={15} />}
            Link my Account
          </button>
        </div>
      )}

      <div style={{ marginTop: "1.1rem" }}>
        <button type="button" className="btn ghost small" onClick={() => setShowAdvanced(s => !s)}>
          {showAdvanced ? "Hide advanced" : "Advanced"}
        </button>
        {showAdvanced && (
          <div style={{ display: "grid", gap: "1rem", marginTop: "0.8rem" }}>
            <form onSubmit={submitLink} style={{ display: "grid", gap: "0.7rem", maxWidth: 560 }}>
              <strong>Link with an account token</strong>
              <label>Account token
                <input value={accountToken} onChange={e => setAccountToken(e.target.value)}
                       placeholder="hba.…" />
              </label>
              <label>Node name
                <input value={nodeName} onChange={e => setNodeName(e.target.value)} placeholder="living-room-mini" />
              </label>
              <label>This node's LAN address (peers connect here, port 80)
                <input value={peerUrl} onChange={e => setPeerUrl(e.target.value)} placeholder="http://192.168.1.10" />
              </label>
              <label>Control plane URL (leave empty for control.homebox.sh)
                <input value={cpUrl} onChange={e => setCpUrl(e.target.value)}
                       placeholder={account?.control_plane_url || "https://control.homebox.sh"} />
              </label>
              <div>
                <button className="btn primary" type="submit" disabled={link.isPending}>
                  {link.isPending ? <span className="spinner" /> : <>Link account</>}
                </button>
              </div>
            </form>
            {!status?.active && (
              <form onSubmit={(e) => { e.preventDefault(); manualJoin.mutate(); }}
                    style={{ display: "grid", gap: "0.7rem", maxWidth: 560 }}>
                <strong>Manual cluster join (token fallback)</strong>
                <label>Join token (minted on an existing node)
                  <input value={joinToken} onChange={e => setJoinToken(e.target.value)} placeholder="hbj.hbc_…" />
                </label>
                <label>This node's LAN address
                  <input value={peerUrl} onChange={e => setPeerUrl(e.target.value)} placeholder="http://192.168.1.10" />
                </label>
                <label>Node name
                  <input value={nodeName} onChange={e => setNodeName(e.target.value)} placeholder="basement-mini" />
                </label>
                <label>Control plane URL (empty = control.homebox.sh)
                  <input value={cpUrl} onChange={e => setCpUrl(e.target.value)} placeholder="https://control.homebox.sh" />
                </label>
                <div>
                  <button className="btn primary" type="submit" disabled={manualJoin.isPending || clusterLocked}
                    title={clusterLocked ? "Upgrade to Homebox Premium to join a cluster" : undefined}>
                    Join cluster
                  </button>
                </div>
              </form>
            )}
          </div>
        )}
      </div>
    </>
  );

  // The god view, wired to this node's account endpoints. Remote node ops go
  // through control-plane directives; cluster-local flows (mirror, evict,
  // join token) reuse the existing endpoints.
  const topologySection = account?.linked && (
    <Topology
      topology={topology}
      thisNodeId={topology?.this_node_id ?? status?.node_id}
      thisClusterId={status?.cluster_id}
      clustered={!!status?.active}
      clusterLocked={clusterLocked}
      integrations={integrations}
      busy={
        directive.isPending || provision.isPending || cancelProvision.isPending ||
        evict.isPending || mirrorEnable.isPending || mint.isPending ||
        joinCluster.isPending || inviteNode.isPending || createCluster.isPending ||
        sync.isPending || setServing.isPending || split.isPending
      }
      thisClusterExtras={thisClusterExtras}
      thisNodeHealth={<NodeHealthDetail isSelf />}
      actions={{
        // Nodes in THIS cluster use the direct local endpoints (instant,
        // optimistic); nodes in other clusters ride the directive queue.
        setServing: (node_id, serving) =>
          (status?.roster ?? []).some(n => n.node_id === node_id)
            ? setServing.mutate({ node_id, serving })
            : directive.mutate({ node_id, type: "set_serving", payload: { serving } }),
        evict: (_clusterId, node_id) => evict.mutate(node_id),
        splitOff: (node_id, name) =>
          node_id === status?.node_id
            ? split.mutate(name)
            : directive.mutate({ node_id, type: "split_off", payload: { name } }),
        splitCluster: (cluster_id, node_ids, name) =>
          directive.mutate({ node_id: node_ids[0], type: "split_cluster", payload: { name, cluster_id, node_ids } }),
        addMirror: () => mirrorEnable.mutate(),
        provision: (v) => provision.mutate(v),
        cancelProvision: (id) => cancelProvision.mutate(id),
        mintToken: () => mint.mutate(),
        syncNow: () => sync.mutate(),
        leave: () => setConfirmLeave(true),
        joinCluster: (cluster_id) => joinCluster.mutate(cluster_id),
        inviteNode: (node_id) => inviteNode.mutate(node_id),
        createCluster: (name) => createCluster.mutate(name),
        renameCluster: (cluster_id, name) => renameCluster.mutate({ cluster_id, name }),
      }}
    />
  );

  return (
    <>
      <div className="page-head">
        <div className="row" style={{ gap: "0.4rem", minWidth: 0 }}>
          <h1>System</h1>
          <PageHelp title="System">
            <p>
              This page is the health and topology view of your Homebox setup. With a homebox.sh
              account linked, it shows every cluster and standalone node on your account — the
              node you are connected to is marked "you are here". Without an account it shows
              this homebox and, if you joined a cluster with a token, its full roster.
            </p>
            <p>
              The pill next to the title shows whether this node is linked to a homebox.sh
              account. Click it to link (silently when possible, or via GitHub/Google or an
              account token) or to see account details, refresh, and unlink. While linked, this
              node's configuration is continuously backed up to your encrypted cloud vault and
              kept in sync across every homebox on your account.
            </p>
            <p>
              Click a cluster's name to rename it. The "this cluster" pill opens cluster
              actions: trigger an immediate sync, mint a join token for adding a node manually,
              add a cloud node on AWS/GCP, split the cluster, or leave it. Each node's status
              pill opens node actions — drain or resume app traffic, split a node off into its
              own cluster, or evict a dead node. The last serving node can't be drained unless
              a standby mirror is online, so app traffic always has somewhere to go.
            </p>
            <p>
              Expand the row of the node you're connected to for its live infrastructure
              health — public URL, tunnel, router, and DNS checks with uptime and latency,
              sampled every 30 seconds.
            </p>
          </PageHelp>
        </div>
        {account && (
          <button type="button"
            className={`badge ${account.linked ? "success" : "plain"} account-pill`}
            title={account.linked ? "homebox.sh account details" : "Link your homebox.sh account"}
            onClick={() => setAccountModalOpen(true)}>
            {account.linked ? "Linked" : "Unlinked"}
          </button>
        )}
      </div>

      {joining && (
        <div className="card"><span className="spinner" /> Restarting onto the cluster keys — hang tight…</div>
      )}

      {upgradeNotice && (
        <div className="banner danger">
          <span><ShieldAlert size={14} style={{ verticalAlign: "-2px", marginRight: "0.35rem" }} />{upgradeNotice}</span>
          <div className="row">
            <button className="btn primary small" onClick={() => upgrade.mutate()} disabled={upgrade.isPending}>
              {upgrade.isPending ? <span className="spinner" /> : <>Upgrade at homebox.sh</>}
            </button>
            <button className="btn ghost small" onClick={() => setUpgradeNotice(null)}>Dismiss</button>
          </div>
        </div>
      )}
      {connectError && (
        <div className="banner danger">
          <span><ShieldAlert size={14} style={{ verticalAlign: "-2px", marginRight: "0.35rem" }} />Couldn't connect your account: {connectError}</span>
          <button className="btn ghost small" onClick={() => setConnectError(null)}>Dismiss</button>
        </div>
      )}
      {status?.node_role === "mirror" && (
        <div className="banner info">
          <span><Cloud size={14} style={{ verticalAlign: "-2px", marginRight: "0.35rem" }} />This node is a cloud mirror (standby)</span>
        </div>
      )}

      {topologySection}

      {status?.active && showLegacyClusterCard && (
        <div className="card">
          <div className="card-row">
            <div className="row">
              <span className="badge ok"><Network size={12} /> {status.name?.trim() || "home"} · {status.cluster_id}</span>
              {!status.initial_sync_done && <span className="badge warn">initial sync pending…</span>}
            </div>
            <div className="row" style={{ gap: "0.5rem" }}>
              <button className="btn ghost" onClick={() => sync.mutate()} disabled={sync.isPending}>
                <RefreshCw size={14} /> Sync now
              </button>
              <button className="btn ghost" onClick={() => mint.mutate()} disabled={mint.isPending}>
                <Ticket size={14} /> Join token
              </button>
              <button className="btn ghost danger"
                title="Leave this cluster and immediately found a new one with this node"
                onClick={() => {
                  const selfName = (status.roster ?? []).find(n => n.node_id === status.node_id)?.name;
                  setSplitName(selfName || account?.node_name || "home");
                  setConfirmSplit(true);
                }}>
                <Split size={14} /> Split off…
              </button>
              <button className="btn ghost danger" onClick={() => setConfirmLeave(true)}>
                <Unplug size={14} /> Leave…
              </button>
            </div>
          </div>

          {licenseBanner}

          {mirrorServingBanner}

          {mintedTokenRow && <div style={{ marginTop: "0.75rem" }}>{mintedTokenRow}</div>}

          <h3 style={{ marginTop: "0.9rem" }}>Nodes</h3>
          {(status.roster ?? []).map(n => {
            const isMirror = n.role === "mirror";
            const serving = n.serving !== false;
            const isSelf = n.node_id === status.node_id;
            // Standby mirrors (serving=false by design) don't count toward the
            // last-serving guard, and an ONLINE mirror lifts it entirely — the
            // backend allows draining the last peer when a standby can
            // auto-promote, so the button must too.
            const servingPeers = (status.roster ?? []).filter(x => x.role !== "mirror" && x.serving !== false).length;
            const mirrorStandby = (status.roster ?? []).some(x => x.role === "mirror" && x.online);
            const isLastServing = serving && !isMirror && servingPeers <= 1 && !mirrorStandby;
            return (
              <NodeRow key={n.node_id} n={n} isSelf={isSelf} right={
                isMirror ? (
                  <span className="dim">automatic failover</span>
                ) : (
                  <>
                    <button className="btn ghost"
                      title={isLastServing
                        ? "Can't disable the last serving node — enable another node first so app traffic has somewhere to go"
                        : serving
                          ? (isSelf
                              ? "Drain app traffic from this node. The control plane stays connected — re-enable from this admin on the LAN."
                              : "Drain app traffic from this node so the shared tunnel routes to healthy peers. Control plane stays connected.")
                          : "Resume app traffic on this node"}
                      onClick={() => setServing.mutate({ node_id: n.node_id, serving: !serving })}
                      disabled={setServing.isPending || isLastServing}>
                      {serving ? <><PowerOff size={14} /> Disable</> : <><Power size={14} /> Enable</>}
                    </button>
                    {!isSelf && (
                      <button className="btn ghost danger" title="Remove this node from the cluster (for dead/unreachable nodes — use Leave on the node itself when possible)"
                        onClick={() => evict.mutate(n.node_id)} disabled={evict.isPending}>
                        <UserMinus size={14} /> Evict
                      </button>
                    )}
                  </>
                )
              } />
            );
          })}
        </div>
      )}

      {status?.active && (
        <CloudMirrorCard
          mirror={mirror}
          locked={mirrorLocked}
          onEnable={() => mirrorEnable.mutate()}
          enablePending={mirrorEnable.isPending}
          onUpgrade={() => upgrade.mutate()}
          upgradePending={upgrade.isPending}
          onOpenDisableConfirm={() => setConfirmDisableMirror(true)}
          accountLinked={!!status?.account_linked}
          onConnectAccount={() => setConnectModalOpen(true)}
        />
      )}

      {/* This node's infrastructure health (uptime sparklines). Linked, it
          lives behind the expandable self row inside the topology god view;
          clustered-but-unlinked it lives in the legacy roster rows. Only the
          unlinked/unclustered layout (or a linked node whose topology hasn't
          loaded) keeps this standalone card — its row IS the same expandable
          chevron pattern. */}
      {status?.node_id && !status.active && !(account?.linked && topology) && (
        <div className="card">
          <h3 style={{ marginTop: 0 }}>This node</h3>
          <NodeRow
            isSelf
            n={{
              node_id: status.node_id,
              name: account?.node_name || "This node",
              peer_url: account?.peer_url || "",
              version: "",
              online: true,
              role: "peer",
              serving: true,
            }}
          />
        </div>
      )}

      {account?.linked && !status?.active && clusterLocked && (
        <PremiumCallout
          pitch="Connect multiple homeboxes into one cluster with automatic failover, plus an optional cloud mirror standby."
          onUpgrade={() => upgrade.mutate()}
          pending={upgrade.isPending}
          accountLinked={!!status?.account_linked}
          onConnectAccount={() => setConnectModalOpen(true)}
        />
      )}

      {account?.linked && !status?.active && (
        <div className="card">
          <div className="row" style={{ justifyContent: "space-between" }}>
            <h3 style={{ margin: 0 }}>Advanced — manual join (token fallback)</h3>
            <button className="btn ghost" onClick={() => setShowManualJoin(s => !s)}>
              {showManualJoin ? "Hide" : "Show"}
            </button>
          </div>
          {showManualJoin && (
            <form onSubmit={(e) => { e.preventDefault(); manualJoin.mutate(); }}
                  style={{ display: "grid", gap: "0.7rem", maxWidth: 560, marginTop: "0.7rem" }}>
              <label>Join token (minted on an existing node)
                <input value={joinToken} onChange={e => setJoinToken(e.target.value)} placeholder="hbj.hbc_…" />
              </label>
              <label>This node's LAN address
                <input value={peerUrl} onChange={e => setPeerUrl(e.target.value)} placeholder="http://192.168.1.10" />
              </label>
              <label>Node name
                <input value={nodeName} onChange={e => setNodeName(e.target.value)} placeholder="basement-mini" />
              </label>
              <label>Control plane URL (empty = control.homebox.sh)
                <input value={cpUrl} onChange={e => setCpUrl(e.target.value)} placeholder="https://control.homebox.sh" />
              </label>
              <div>
                <button className="btn primary" type="submit" disabled={manualJoin.isPending || clusterLocked}
                  title={clusterLocked ? "Upgrade to Homebox Premium to join a cluster" : undefined}>
                  Join cluster
                </button>
              </div>
            </form>
          )}
        </div>
      )}

      <Modal
        open={confirmLeave}
        title="Leave & disconnect?"
        onClose={() => setConfirmLeave(false)}
        footer={
          <>
            <button className="btn ghost" onClick={() => setConfirmLeave(false)}>Cancel</button>
            <button className="btn danger" onClick={() => leave.mutate()} disabled={leave.isPending}>
              {leave.isPending ? <span className="spinner" /> : <>Leave cluster</>}
            </button>
          </>
        }
      >
        <p>Peers stop replicating to and from this node (their WAL slots for it are released), and
           config/deploy sync ends. Cluster keys are not rotated.</p>
        <label className="row" style={{ gap: "0.5rem", marginTop: "0.6rem" }}>
          <input type="checkbox" checked={stopTunnel} onChange={e => setStopTunnel(e.target.checked)} />
          Stop serving the shared Cloudflare tunnel from this node
        </label>
        <label className="row" style={{ gap: "0.5rem", marginTop: "0.4rem" }}>
          <input type="checkbox" checked={teardownStacks} onChange={e => setTeardownStacks(e.target.checked)} />
          Also tear down cluster-replicated app stacks on this node
        </label>
      </Modal>

      <Modal
        open={confirmSplit}
        title="Split off into a new cluster"
        onClose={() => { if (!split.isPending) setConfirmSplit(false); }}
        footer={
          <>
            <button className="btn ghost" onClick={() => setConfirmSplit(false)} disabled={split.isPending}>
              Cancel
            </button>
            <button className="btn danger" onClick={() => split.mutate(splitName)}
              disabled={split.isPending || !splitName.trim()}>
              {split.isPending ? <span className="spinner" /> : <><Split size={14} /> Split off</>}
            </button>
          </>
        }
      >
        <label>New cluster name
          <input value={splitName} onChange={e => setSplitName(e.target.value)}
                 placeholder="home" autoFocus />
        </label>
        <ul className="dim" style={{ margin: "0.85rem 0 0", paddingLeft: "1.15rem", display: "grid", gap: "0.35rem" }}>
          <li>This node leaves the <strong>{status?.name || "current"}</strong> cluster and immediately
              founds a new one.</li>
          <li>Projects and data on this node are kept.</li>
          <li>Other nodes are unaffected and keep serving.</li>
          {(status?.roster ?? []).length > 1 && (
            <li>This node disconnects from the shared tunnel — you may need to reconnect a tunnel
                for public routing.</li>
          )}
        </ul>
      </Modal>

      <Modal
        open={confirmDisableMirror}
        title="Disable cloud mirror?"
        onClose={() => { if (!mirrorDisable.isPending) setConfirmDisableMirror(false); }}
        footer={
          <>
            <button className="btn ghost" onClick={() => setConfirmDisableMirror(false)} disabled={mirrorDisable.isPending}>
              Cancel
            </button>
            <button className="btn danger" onClick={() => mirrorDisable.mutate()} disabled={mirrorDisable.isPending}>
              {mirrorDisable.isPending ? <span className="spinner" /> : <>Disable</>}
            </button>
          </>
        }
      >
        <p>The cloud standby is torn down — local nodes are unaffected and keep serving traffic as usual.</p>
      </Modal>

      <Modal
        open={accountModalOpen}
        title={account?.linked ? "homebox.sh account" : "Link your homebox.sh account"}
        onClose={() => setAccountModalOpen(false)}
      >
        {accountModalBody}
      </Modal>

      <AccountAuthModal
        open={connectModalOpen}
        onClose={() => setConnectModalOpen(false)}
        onLinked={() => {
          setConnectModalOpen(false);
          invalidate();
          toast.show("Account connected", "ok");
        }}
      />
    </>
  );
}
