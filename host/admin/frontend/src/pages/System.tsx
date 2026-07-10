import { FormEvent, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Cloud, Copy, Crown, ExternalLink, Gauge, LogIn, Network, Plus, Power, PowerOff,
  RefreshCw, Send, ShieldAlert, Ticket, Unplug, UserMinus,
} from "lucide-react";
import { api, ApiError } from "../lib/api";
import { Modal } from "../components/Modal";
import { useToast } from "../lib/toast";
import type { UptimeReport, UptimeStatus } from "../lib/types";

const PRICING_URL = "https://homebox.sh/pricing";

/**
 * System: health of the Homebox infrastructure, plus clustering. Not
 * clustered → this node's own uptime monitoring, with an option to join a
 * cluster (via a homebox.sh account or a manual join token). Clustered →
 * the health of every node in the cluster, with this node's own uptime
 * detail kept below it.
 */

type NodeRole = "peer" | "mirror";

type ClusterNode = {
  node_id: string;
  name: string;
  peer_url: string;
  version: string;
  ordinal?: number;
  online: boolean;
  serving?: boolean;
  role?: NodeRole;
};

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

/** "Homebox Premium" upgrade callout — used for both cluster and cloud-mirror gating. */
function PremiumCallout({ pitch, onUpgrade, pending }: { pitch: string; onUpgrade: () => void; pending: boolean }) {
  return (
    <div className="card premium-callout">
      <h3><Crown size={16} /> Homebox Premium</h3>
      <p className="dim" style={{ marginTop: "0.4rem" }}>{pitch}</p>
      <div className="row" style={{ marginTop: "0.85rem" }}>
        <button className="btn primary" onClick={onUpgrade} disabled={pending}>
          {pending ? <span className="spinner" /> : <>Upgrade</>}
        </button>
        <a className="btn ghost" href={PRICING_URL} target="_blank" rel="noreferrer">
          See pricing <ExternalLink size={13} />
        </a>
      </div>
    </div>
  );
}

/** Cloud Mirror section on the System page — visible whenever a cluster is active. */
function CloudMirrorCard({
  mirror, locked, onEnable, enablePending, onUpgrade, upgradePending, onOpenDisableConfirm,
}: {
  mirror?: MirrorStatus;
  locked: boolean;
  onEnable: () => void;
  enablePending: boolean;
  onUpgrade: () => void;
  upgradePending: boolean;
  onOpenDisableConfirm: () => void;
}) {
  const state: MirrorState = mirror?.status ?? "none";

  if (state === "none" || state === "decommissioned") {
    if (locked) {
      return (
        <PremiumCallout
          pitch="Add a homebox.sh cloud standby that stays in sync with your cluster and automatically serves your apps if every local node goes down."
          onUpgrade={onUpgrade}
          pending={upgradePending}
        />
      );
    }
    return (
      <div className="card">
        <h3 style={{ marginTop: 0 }}><Cloud size={15} /> Cloud Mirror</h3>
        <p className="dim">
          A homebox.sh cloud standby that stays in sync with this cluster and automatically serves
          your apps if every local node goes down.
        </p>
        <button className="btn primary" onClick={onEnable} disabled={enablePending}>
          {enablePending ? <span className="spinner" /> : <><Cloud size={14} /> Enable Cloud Mirror</>}
        </button>
      </div>
    );
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

type AccountNode = { node_id: string; name: string; peer_url: string; online: boolean };
type AccountCluster = { cluster_id: string; name: string; nodes: ClusterNode[] };
type AccountStatus = {
  linked: boolean;
  node_name?: string;
  peer_url?: string;
  control_plane_url?: string;
  overview?: { nodes?: AccountNode[]; clusters?: AccountCluster[]; polled_at?: string };
};

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

/** This node's own infrastructure monitoring (uptime, latency per component). */
function NodeHealth() {
  const [window, setWindow] = useState("24h");
  const { data } = useQuery<UptimeReport>({
    queryKey: ["tunnel-uptime", window],
    queryFn: () => api.get<UptimeReport>(`/api/tunnel/uptime?window=${window}`),
    refetchInterval: 5000,
  });

  return (
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
  const [billingNotice, setBillingNotice] = useState<string | null>(null);
  const [confirmDisableMirror, setConfirmDisableMirror] = useState(false);
  const accountSectionRef = useRef<HTMLDivElement>(null);
  // account link form
  const [accountToken, setAccountToken] = useState("");
  const [nodeName, setNodeName] = useState("");
  const [peerUrl, setPeerUrl] = useState("");
  const [cpUrl, setCpUrl] = useState("");
  // create / manual join
  const [newClusterName, setNewClusterName] = useState("home");
  const [joinToken, setJoinToken] = useState("");

  const { data: status } = useQuery<ClusterStatus>({
    queryKey: ["cluster"],
    queryFn: () => api.get<ClusterStatus>("/api/cluster"),
    // Poll faster while a mirror provision/teardown is in flight so the card
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

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["cluster"] });
    qc.invalidateQueries({ queryKey: ["cluster-account"] });
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
    onSuccess: () => { invalidate(); toast.show("Signed in — this node is now linked", "ok"); setAccountToken(""); },
    onError: onErr,
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
    mutationFn: () => api.post("/api/cluster/account/create-cluster", { name: newClusterName }),
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
  const upgrade = useMutation({
    mutationFn: () => api.post<{ url: string }>("/api/cluster/upgrade"),
    onSuccess: (d) => { window.open(d.url, "_blank"); },
    onError: (e: unknown) => {
      if (e instanceof ApiError && e.status === 409) {
        toast.show("Link your homebox.sh account first", "fail");
        accountSectionRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
        return;
      }
      if (e instanceof ApiError && e.status === 503) {
        setBillingNotice("Billing isn't configured on this control plane");
        return;
      }
      onErr(e);
    },
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

  const overview = account?.overview ?? {};
  const rosterIds = new Set((status?.roster ?? []).map(n => n.node_id));
  const invitableNodes = (overview.nodes ?? []).filter(n => !rosterIds.has(n.node_id));

  const license = status?.license;
  const clusterLocked = clusterIsLocked(license);
  const mirrorLocked = mirrorIsLocked(license);
  const mirror = status?.mirror ?? undefined;
  const mirrorServingRoster = (status?.roster ?? []).find(n => n.role === "mirror" && n.serving !== false);

  function submitLink(e: FormEvent) {
    e.preventDefault();
    if (!peerUrl.trim()) { toast.show("Set this node's LAN address", "fail"); return; }
    link.mutate();
  }

  // "Join a cluster" — sign in to homebox.sh, then create or join a cluster
  // with the linked nodes it reports. Also used (in a smaller role) to invite
  // other linked nodes once this node is already clustered.
  const accountSection = account?.linked ? (
    <div className="card">
      <div className="card-row">
        <div className="row">
          <span className="badge ok"><LogIn size={12} /> homebox.sh account linked</span>
          <span className="dim">as {account.node_name || "unnamed node"} · {account.peer_url}</span>
        </div>
        <div className="row" style={{ gap: "0.5rem" }}>
          <button className="btn ghost" onClick={() => refresh.mutate()} disabled={refresh.isPending}>
            <RefreshCw size={14} /> Refresh
          </button>
          <button className="btn ghost" onClick={() => unlink.mutate()}>Unlink</button>
        </div>
      </div>

      {status?.active ? (
        // Already in a cluster — the cluster and its nodes are shown in the card
        // above, so here we only surface OTHER linked nodes available to invite.
        // Nothing to invite → render nothing.
        invitableNodes.length > 0 && (
          <>
            <h3 style={{ marginTop: "0.9rem" }}>Other linked nodes</h3>
            {invitableNodes.map(n => (
              <div key={n.node_id} className="row" style={{ justifyContent: "space-between", padding: "0.35rem 0", flexWrap: "wrap", gap: "0.5rem" }}>
                <div className="row" style={{ gap: "0.5rem" }}>
                  <span className={`badge ${n.online ? "ok" : "fail"}`}>{n.online ? "online" : "offline"}</span>
                  <strong>{n.name || n.node_id}</strong>
                  <span className="dim">{n.peer_url}</span>
                </div>
                <button className="btn ghost" onClick={() => inviteNode.mutate(n.node_id)} disabled={inviteNode.isPending}>
                  <Send size={14} /> Invite to this cluster
                </button>
              </div>
            ))}
          </>
        )
      ) : (
        // Not in a cluster yet — full picker: join an existing cluster, or
        // create one with this node as the seed.
        <>
          {(overview.clusters ?? []).length > 0 && (
            <>
              <h3 style={{ marginTop: "0.9rem" }}>Your clusters</h3>
              {(overview.clusters ?? []).map(cl => (
                <div key={cl.cluster_id} className="row" style={{ justifyContent: "space-between", padding: "0.35rem 0", flexWrap: "wrap", gap: "0.5rem" }}>
                  <div className="row" style={{ gap: "0.5rem" }}>
                    <strong>{cl.name}</strong>
                    <span className="dim">{cl.cluster_id}</span>
                    <span className="badge">{cl.nodes.length} node{cl.nodes.length === 1 ? "" : "s"}</span>
                  </div>
                  <button className="btn primary" onClick={() => joinCluster.mutate(cl.cluster_id)}
                    disabled={joinCluster.isPending || clusterLocked}
                    title={clusterLocked ? "Upgrade to Homebox Premium to join a cluster" : undefined}>
                    Join this cluster
                  </button>
                </div>
              ))}
            </>
          )}

          <h3 style={{ marginTop: "0.9rem" }}>Linked nodes</h3>
          {(overview.nodes ?? []).map(n => (
            <div key={n.node_id} className="row" style={{ justifyContent: "space-between", padding: "0.35rem 0", flexWrap: "wrap", gap: "0.5rem" }}>
              <div className="row" style={{ gap: "0.5rem" }}>
                <span className={`badge ${n.online ? "ok" : "fail"}`}>{n.online ? "online" : "offline"}</span>
                <strong>{n.name || n.node_id}</strong>
                {n.node_id === status?.node_id && <span className="chip active">this node</span>}
                <span className="dim">{n.peer_url}</span>
              </div>
            </div>
          ))}
          {(overview.nodes ?? []).length <= 1 && (
            <div className="dim" style={{ marginTop: "0.4rem" }}>
              Sign in on your other homebox nodes with the same account token — they'll appear here,
              ready to invite.
            </div>
          )}

          <div className="row" style={{ marginTop: "0.9rem", gap: "0.5rem" }}>
            <input value={newClusterName} onChange={e => setNewClusterName(e.target.value)}
                   placeholder="cluster name" style={{ maxWidth: 200 }} />
            <button className="btn primary" onClick={() => createCluster.mutate()}
              disabled={createCluster.isPending || clusterLocked}
              title={clusterLocked ? "Upgrade to Homebox Premium to create a cluster" : undefined}>
              <Plus size={14} /> Create cluster with this node
            </button>
          </div>
        </>
      )}
    </div>
  ) : (
    <div className="card">
      <h3><LogIn size={15} /> Sign in to homebox.sh</h3>
      <p className="dim">
        Link this node to your account to see all your nodes and clusters from anywhere,
        create clusters, and add nodes with one click.
      </p>
      <form onSubmit={submitLink} style={{ display: "grid", gap: "0.7rem", maxWidth: 560 }}>
        <label>Account token
          <input value={accountToken} onChange={e => setAccountToken(e.target.value)}
                 placeholder="any token in dev mode" />
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
        <div><button className="btn primary" type="submit" disabled={link.isPending}>Sign in & link node</button></div>
      </form>
    </div>
  );

  return (
    <>
      <h1>System</h1>
      <p className="lede">
        {status?.active
          ? "Health of every node in the cluster — active-active, with replicated databases and automatic failover."
          : "Health of this node's infrastructure, monitored every 30s and self-healing. Join a cluster to add more nodes."}
      </p>

      {joining && (
        <div className="card"><span className="spinner" /> Restarting onto the cluster keys — hang tight…</div>
      )}

      {upgradeNotice && (
        <div className="banner danger">
          <span><ShieldAlert size={14} style={{ verticalAlign: "-2px", marginRight: "0.35rem" }} />{upgradeNotice}</span>
          <div className="row">
            <button className="btn primary small" onClick={() => upgrade.mutate()} disabled={upgrade.isPending}>
              {upgrade.isPending ? <span className="spinner" /> : <>Upgrade</>}
            </button>
            <button className="btn ghost small" onClick={() => setUpgradeNotice(null)}>Dismiss</button>
          </div>
        </div>
      )}
      {billingNotice && (
        <div className="banner warn">
          <span>{billingNotice}</span>
          <button className="btn ghost small" onClick={() => setBillingNotice(null)}>Dismiss</button>
        </div>
      )}
      {status?.node_role === "mirror" && (
        <div className="banner info">
          <span><Cloud size={14} style={{ verticalAlign: "-2px", marginRight: "0.35rem" }} />This node is a cloud mirror (standby)</span>
        </div>
      )}

      {status?.active && (
        <div className="card">
          <div className="card-row">
            <div className="row">
              <span className="badge ok"><Network size={12} /> {status.name} · {status.cluster_id}</span>
              {license && (
                <span className={`badge ${license.expired ? "fail" : license.in_grace ? "warn" : "plain"}`}>
                  {license.node_count}/{license.max_nodes} nodes · {planLabel(license.plan)}
                </span>
              )}
              {!status.initial_sync_done && <span className="badge warn">initial sync pending…</span>}
            </div>
            <div className="row" style={{ gap: "0.5rem" }}>
              <button className="btn ghost" onClick={() => sync.mutate()} disabled={sync.isPending}>
                <RefreshCw size={14} /> Sync now
              </button>
              <button className="btn ghost" onClick={() => mint.mutate()} disabled={mint.isPending}>
                <Ticket size={14} /> Join token
              </button>
              <button className="btn ghost danger" onClick={() => setConfirmLeave(true)}>
                <Unplug size={14} /> Leave…
              </button>
            </div>
          </div>

          {license?.expired ? (
            <div className="banner danger">
              <span>Premium features paused — existing services keep running. Renew to restore clustering.</span>
              <button className="btn primary small" onClick={() => upgrade.mutate()} disabled={upgrade.isPending}>
                {upgrade.isPending ? <span className="spinner" /> : <>Renew</>}
              </button>
            </div>
          ) : license?.in_grace ? (
            <div className="banner warn">
              <span>License expired — running in a 14-day grace period. Renew soon to keep clustering.</span>
              <button className="btn primary small" onClick={() => upgrade.mutate()} disabled={upgrade.isPending}>
                {upgrade.isPending ? <span className="spinner" /> : <>Renew</>}
              </button>
            </div>
          ) : null}

          {mirrorServingRoster && (
            <div className="banner info" style={{ marginTop: "0.75rem" }}>
              <span><Cloud size={14} style={{ verticalAlign: "-2px", marginRight: "0.35rem" }} />
                Cloud mirror is serving your traffic
              </span>
            </div>
          )}

          {mintedToken && (
            <div className="card-row" style={{ marginTop: "0.75rem" }}>
              <code style={{ wordBreak: "break-all", userSelect: "all" }}>{mintedToken}</code>
              <button className="btn ghost"
                onClick={() => { navigator.clipboard.writeText(mintedToken); toast.show("Copied", "ok"); }}>
                <Copy size={14} /> Copy
              </button>
            </div>
          )}

          <h3 style={{ marginTop: "0.9rem" }}>Nodes</h3>
          {(status.roster ?? []).map(n => {
            const isMirror = n.role === "mirror";
            const serving = n.serving !== false;
            const isSelf = n.node_id === status.node_id;
            const servingCount = (status.roster ?? []).filter(x => x.serving !== false).length;
            const isLastServing = serving && servingCount <= 1;
            return (
            <div key={n.node_id} className="row" style={{ justifyContent: "space-between", gap: "0.75rem", flexWrap: "wrap", padding: "0.4rem 0" }}>
              <div className="row" style={{ gap: "0.5rem", minWidth: 0 }}>
                <span className={`badge ${n.online ? "ok" : "fail"}`}>{n.online ? "online" : "offline"}</span>
                {isMirror
                  ? <span className="badge info"><Cloud size={12} /> Cloud Mirror</span>
                  : <strong>{n.name || n.node_id}</strong>}
                {n.ordinal != null && <span className="dim">n{n.ordinal}</span>}
                {isSelf && <span className="chip active">this node</span>}
                {!isMirror && !serving && <span className="badge warn">disabled</span>}
              </div>
              <div className="row" style={{ gap: "0.75rem" }}>
                <span className="dim">{n.peer_url || "no peer URL"}</span>
                {isMirror ? (
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
                )}
              </div>
            </div>
            );
          })}
          <div className="dim" style={{ marginTop: "0.6rem" }}>
            Last heartbeat {status.last_heartbeat ?? "never"} · last config sync {status.last_sync_at ?? "never"}
          </div>
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
        />
      )}

      {status?.active && <h3 style={{ marginTop: "1.75rem" }}>This node</h3>}
      <NodeHealth />

      {!status?.active && clusterLocked && (
        <PremiumCallout
          pitch="Connect multiple homeboxes into one cluster with automatic failover, plus an optional cloud mirror standby."
          onUpgrade={() => upgrade.mutate()}
          pending={upgrade.isPending}
        />
      )}

      <div ref={accountSectionRef}>{accountSection}</div>

      {!status?.active && (
        <div className="card">
          <div className="row" style={{ justifyContent: "space-between" }}>
            <h3 style={{ margin: 0 }}>Manual join (token fallback)</h3>
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
    </>
  );
}
