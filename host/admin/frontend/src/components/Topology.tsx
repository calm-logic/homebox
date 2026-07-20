import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  Boxes, ChevronDown, Cloud, CloudCog, Plus, Power, PowerOff, Send, Split, Ticket, UserMinus,
} from "lucide-react";
import { Modal } from "./Modal";
import { timeAgo } from "../lib/time";
import type {
  AccountDirective, AccountTopology, IntegrationItem, NodeProvision, TopologyCluster, TopologyNode,
} from "../lib/types";

/**
 * Topology — the account-wide fleet "god view". Renders every cluster (with
 * license + mirror state and per-node rows) and every standalone node on the
 * linked homebox.sh account, with pending directives/provisions inline.
 *
 * The component is action-agnostic: each action renders only when its
 * callback is passed, so the same shape works from a node's admin (full
 * actions, "you are connected here" marker) and — re-implemented leanly —
 * from the homebox.sh portal (directives/evict/mirror only, no marker).
 */

export interface TopologyActions {
  /** Queue a set_serving directive for any node in any cluster. */
  setServing?: (nodeId: string, serving: boolean) => void;
  /** Evict a node from its cluster (direct call — works on dead nodes). */
  evict?: (clusterId: string, nodeId: string) => void;
  /** Queue a split_off directive: the node leaves and founds a new cluster. */
  splitOff?: (nodeId: string, name: string) => void;
  /** Queue a split_cluster directive for a picked node subset. */
  splitCluster?: (clusterId: string, nodeIds: string[], name: string) => void;
  /** Existing cloud-mirror flow for a cluster. */
  addMirror?: (clusterId: string) => void;
  /** Provision a cloud node on a linked AWS/GCP integration. */
  provision?: (v: { name: string; provider: "aws" | "gcp"; integration_id: number; region: string; machine?: string }) => void;
  cancelProvision?: (id: NodeProvision["id"]) => void;
  /** Existing mint-join-token flow (this cluster). */
  mintToken?: () => void;
  /** Join an existing cluster (only when this node is standalone). */
  joinCluster?: (clusterId: string) => void;
  /** Invite a standalone node into this cluster. */
  inviteNode?: (nodeId: string) => void;
  /** Found a new cluster with this node as the seed. */
  createCluster?: (name: string) => void;
}

const DIRECTIVE_TERMINAL = new Set(["done", "acked", "completed", "failed", "error", "cancelled"]);
const PROVISION_DONE = new Set(["active", "done", "joined", "completed"]);

function directiveLabel(d: AccountDirective): string {
  switch (d.type) {
    case "set_serving":
      return d.payload && d.payload["serving"] === false ? "disable" : "enable";
    case "split_off": return "split off";
    case "split_cluster": return "split cluster";
    case "join": return "join";
    default: return d.type;
  }
}

/** Subtle inline status row for a pending/failed directive. */
function DirectiveRow({ d }: { d: AccountDirective }) {
  if (DIRECTIVE_TERMINAL.has(d.status) && d.status !== "failed" && d.status !== "error") return null;
  const failed = d.status === "failed" || d.status === "error";
  return (
    <div className="topo-pending">
      {failed ? <span className="badge fail">failed</span> : <span className="spinner" />}
      <span>{directiveLabel(d)} directive {failed ? "failed" : "pending"}{d.created_at ? ` · queued ${timeAgo(d.created_at) ?? ""}` : ""}</span>
    </div>
  );
}

/** "Add node" dropdown for a cluster — Cloud mirror | On AWS/GCP… | Manual join token. */
function AddNodeMenu({
  items, busy,
}: {
  items: { key: string; label: ReactNode; onClick: () => void; disabled?: boolean; title?: string }[];
  busy?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);
  if (items.length === 0) return null;
  return (
    <div className="menu-wrap" ref={wrapRef}>
      <button className="btn ghost small" aria-expanded={open} onClick={() => setOpen(o => !o)} disabled={busy}>
        <Plus size={14} /> Add node <ChevronDown size={13} />
      </button>
      {open && (
        <div className="menu" role="menu">
          {items.map(it => (
            <button key={it.key} className="menu-item" role="menuitem" disabled={it.disabled}
              title={it.title}
              onClick={() => { setOpen(false); it.onClick(); }}>
              {it.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function Topology({
  topology, thisNodeId, thisClusterId, clustered, clusterLocked, integrations, actions = {}, busy,
}: {
  topology?: AccountTopology;
  /** This node's id — omit (null) on the portal, which has no local node. */
  thisNodeId?: string | null;
  thisClusterId?: string | null;
  /** Whether THIS node is currently in a cluster (gates join/invite/create). */
  clustered?: boolean;
  clusterLocked?: boolean;
  /** Linked integrations, for the AWS/GCP provision picker. */
  integrations?: IntegrationItem[];
  actions?: TopologyActions;
  busy?: boolean;
}) {
  // Modals: evict confirm, split-off (name), split-cluster (node picker + name),
  // cloud-node provision form.
  const [confirmEvict, setConfirmEvict] = useState<{ clusterId: string; node: TopologyNode } | null>(null);
  const [splitOffNode, setSplitOffNode] = useState<TopologyNode | null>(null);
  const [splitOffName, setSplitOffName] = useState("home");
  const [splitCluster, setSplitCluster] = useState<TopologyCluster | null>(null);
  const [splitPicked, setSplitPicked] = useState<Set<string>>(new Set());
  const [splitClusterName, setSplitClusterName] = useState("home");
  const [provisionOpen, setProvisionOpen] = useState(false);
  const [provName, setProvName] = useState("");
  const [provProvider, setProvProvider] = useState<"aws" | "gcp">("aws");
  const [provIntegrationId, setProvIntegrationId] = useState<number | "">("");
  const [provRegion, setProvRegion] = useState("");
  const [provMachine, setProvMachine] = useState("");
  const [newClusterName, setNewClusterName] = useState("home");

  if (!topology) {
    return (
      <div className="card">
        <div className="row"><span className="spinner" /> Loading your fleet…</div>
      </div>
    );
  }

  // Whether this render has a local node ("node admin") vs. the portal.
  const local = thisNodeId != null;
  const clusters = topology.clusters ?? [];
  const standalone = topology.standalone_nodes ?? [];
  const directives = topology.directives ?? [];
  const provisions = topology.provisions ?? [];
  const byNode = new Map<string, AccountDirective[]>();
  for (const d of directives) {
    if (!byNode.has(d.node_id)) byNode.set(d.node_id, []);
    byNode.get(d.node_id)!.push(d);
  }
  const activeProvisions = provisions.filter(p => !PROVISION_DONE.has(p.status));
  const lonely =
    clusters.length === 0 && standalone.every(n => n.node_id === thisNodeId);

  const cloudIntegrations = (integrations ?? []).filter(i => i.provider === "aws" || i.provider === "gcp");
  const provIntegrations = cloudIntegrations.filter(i => i.provider === provProvider);

  function openProvision() {
    setProvName("");
    setProvRegion("");
    setProvMachine("");
    const first = cloudIntegrations[0];
    if (first) {
      setProvProvider(first.provider as "aws" | "gcp");
      setProvIntegrationId(first.id);
    } else {
      setProvIntegrationId("");
    }
    setProvisionOpen(true);
  }

  function pickProvider(p: "aws" | "gcp") {
    setProvProvider(p);
    const first = cloudIntegrations.find(i => i.provider === p);
    setProvIntegrationId(first ? first.id : "");
  }

  const provisionValid =
    provName.trim() !== "" && provRegion.trim() !== "" && provIntegrationId !== "";

  function nodeRow(cl: TopologyCluster | null, n: TopologyNode) {
    const isMirror = n.role === "mirror";
    const serving = n.serving !== false;
    const isSelf = local && n.node_id === thisNodeId;
    const nodes = cl?.nodes ?? [];
    // Last-serving guard, mirroring the roster card: an online standby mirror
    // lifts it (the backend can auto-promote), otherwise the last serving
    // peer can't be disabled.
    const servingPeers = nodes.filter(x => x.role !== "mirror" && x.serving !== false).length;
    const mirrorStandby = nodes.some(x => x.role === "mirror" && x.online);
    const isLastServing = cl != null && serving && !isMirror && servingPeers <= 1 && !mirrorStandby;
    const isThisCluster = cl != null && cl.cluster_id === thisClusterId;
    const nodeDirectives = byNode.get(n.node_id) ?? [];
    const hasPending = nodeDirectives.some(d => !DIRECTIVE_TERMINAL.has(d.status));
    const lastSeen = !n.online ? timeAgo(n.last_seen ?? null) : null;
    const backup = cl == null ? timeAgo(n.backup_updated_at ?? null) : null;

    return (
      <div key={n.node_id}>
        <div className="registry-node">
          <span className={`badge ${n.online ? "ok" : "fail"}`}>{n.online ? "online" : "offline"}</span>
          {isMirror
            ? <span className="badge info"><Cloud size={12} /> Cloud Mirror</span>
            : <strong>{n.name || n.node_id}</strong>}
          {n.ordinal != null && <span className="dim">n{n.ordinal}</span>}
          {isSelf && <span className="chip active" title="You are connected to this node">you are here</span>}
          {!isMirror && !serving && <span className="badge warn">disabled</span>}
          <span className="spacer" />
          {n.version && <span className="dim">{n.version}</span>}
          {n.peer_url && <span className="dim">{n.peer_url}</span>}
          {lastSeen && <span className="dim">seen {lastSeen}</span>}
          {backup && <span className="dim">backup {backup}</span>}
          {!isMirror && cl != null && (
            <>
              {actions.setServing && (
                <button className="btn ghost small"
                  title={isLastServing
                    ? "Can't disable the last serving node — enable another node first so app traffic has somewhere to go"
                    : serving ? "Drain app traffic from this node" : "Resume app traffic on this node"}
                  onClick={() => actions.setServing!(n.node_id, !serving)}
                  disabled={busy || hasPending || isLastServing}>
                  {serving ? <><PowerOff size={13} /> Disable</> : <><Power size={13} /> Enable</>}
                </button>
              )}
              {actions.splitOff && nodes.filter(x => x.role !== "mirror").length > 1 && (
                <button className="btn ghost small" title="This node leaves the cluster and founds a new one"
                  onClick={() => { setSplitOffName(n.name || "home"); setSplitOffNode(n); }}
                  disabled={busy || hasPending}>
                  <Split size={13} /> Split off
                </button>
              )}
              {actions.evict && !isSelf && (!local || isThisCluster) && (
                <button className="btn ghost small danger"
                  title="Remove this node from the cluster (for dead/unreachable nodes)"
                  onClick={() => setConfirmEvict({ clusterId: cl.cluster_id, node: n })}
                  disabled={busy}>
                  <UserMinus size={13} /> Evict
                </button>
              )}
            </>
          )}
          {cl == null && actions.inviteNode && clustered && n.node_id !== thisNodeId && (
            <button className="btn ghost small" onClick={() => actions.inviteNode!(n.node_id)} disabled={busy}>
              <Send size={13} /> Invite to this cluster
            </button>
          )}
        </div>
        {nodeDirectives.map(d => <DirectiveRow key={String(d.id)} d={d} />)}
      </div>
    );
  }

  function provisionRow(p: NodeProvision) {
    const failed = p.status === "failed" || p.status === "error" || !!p.error;
    return (
      <div key={String(p.id)} className="topo-pending">
        {failed ? <span className="badge fail">failed</span> : <span className="spinner" />}
        <span>
          {p.provider === "aws" ? "AWS" : p.provider === "gcp" ? "GCP" : p.provider} node
          {" "}<strong>{p.name}</strong> in {p.region} — {failed ? (p.error || "provisioning failed") : p.status}
          {p.created_at ? ` · started ${timeAgo(p.created_at) ?? ""}` : ""}
        </span>
        {actions.cancelProvision && (
          <button className="btn ghost small danger" onClick={() => actions.cancelProvision!(p.id)} disabled={busy}>
            {failed ? "Remove" : "Cancel"}
          </button>
        )}
      </div>
    );
  }

  return (
    <div className="card topology">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h3 style={{ margin: 0, display: "inline-flex", alignItems: "center", gap: "0.4rem" }}>
          <Boxes size={14} /> Your homeboxes
        </h3>
        {topology.account?.plan && <span className="badge plain">{topology.account.plan}</span>}
      </div>

      {clusters.map(cl => {
        const isThis = local && cl.cluster_id === thisClusterId;
        const nodes = cl.nodes ?? [];
        const lic = cl.license;
        const mirror = cl.mirror;
        const mirrorActive = mirror?.status === "active";
        const nonMirror = nodes.filter(n => n.role !== "mirror");

        // "Add node" menu items — cloud VM / manual token. The "Cloud mirror"
        // just-in-time enable item was removed while mirrors are reworked to
        // run off a destination target (destination = homebox cloud); existing
        // mirror status still renders as a badge below.
        const menuItems: { key: string; label: ReactNode; onClick: () => void; disabled?: boolean; title?: string }[] = [];
        if (actions.provision && (!local || isThis)) {
          menuItems.push({
            key: "cloud-vm",
            label: <><CloudCog size={13} /> On AWS/GCP…</>,
            onClick: openProvision,
            disabled: cloudIntegrations.length === 0,
            title: cloudIntegrations.length === 0 ? "Link an AWS or GCP integration first" : undefined,
          });
        }
        if (actions.mintToken && (!local || isThis)) {
          menuItems.push({
            key: "token",
            label: <><Ticket size={13} /> Manual join token</>,
            onClick: () => actions.mintToken!(),
          });
        }

        return (
          <div key={cl.cluster_id} className={`cluster-block ${isThis ? "current" : ""}`}>
            <div className="cluster-block-head">
              <div className="row" style={{ gap: "0.5rem", minWidth: 0 }}>
                <strong>{cl.name}</strong>
                <span className="dim">{cl.cluster_id}</span>
                {lic ? (
                  <span className={`badge ${lic.expired ? "fail" : lic.in_grace ? "warn" : "plain"}`}>
                    {lic.node_count != null && lic.max_nodes != null
                      ? `${lic.node_count}/${lic.max_nodes} nodes · ` : ""}
                    {(lic.plan ?? "free").charAt(0).toUpperCase() + (lic.plan ?? "free").slice(1)}
                  </span>
                ) : (
                  <span className="badge plain">{nodes.length} node{nodes.length === 1 ? "" : "s"}</span>
                )}
                {mirror && mirror.status !== "none" && mirror.status !== "decommissioned" && (
                  <span className={`badge ${mirrorActive ? "info" : mirror.status === "failed" ? "fail" : "warn"}`}>
                    <Cloud size={12} /> mirror {mirror.status}
                  </span>
                )}
                {isThis && <span className="chip active">this cluster</span>}
              </div>
              <div className="row" style={{ gap: "0.5rem" }}>
                {actions.joinCluster && local && !clustered && (
                  <button className="btn primary small" onClick={() => actions.joinCluster!(cl.cluster_id)}
                    disabled={busy || clusterLocked}
                    title={clusterLocked ? "Upgrade to Homebox Premium to join a cluster" : undefined}>
                    Join this cluster
                  </button>
                )}
                {actions.splitCluster && nonMirror.length > 1 && (
                  <button className="btn ghost small"
                    title="Move a subset of nodes into a new cluster"
                    onClick={() => {
                      setSplitPicked(new Set());
                      setSplitClusterName("home");
                      setSplitCluster(cl);
                    }}
                    disabled={busy}>
                    <Split size={13} /> Split cluster…
                  </button>
                )}
                <AddNodeMenu items={menuItems} busy={busy} />
              </div>
            </div>
            <div className="cluster-block-nodes">
              {nodes.map(n => nodeRow(cl, n))}
              {isThis && activeProvisions.map(provisionRow)}
            </div>
          </div>
        );
      })}

      {standalone.length > 0 && (
        <div className="cluster-block">
          <div className="cluster-block-head">
            <div className="row" style={{ gap: "0.5rem", minWidth: 0 }}>
              <strong>Standalone homeboxes</strong>
              <span className="badge plain">{standalone.length} node{standalone.length === 1 ? "" : "s"}</span>
            </div>
          </div>
          <div className="cluster-block-nodes">
            {standalone.map(n => nodeRow(null, n))}
          </div>
        </div>
      )}

      {/* Provisions that can't be pinned to a cluster block (no cluster in view). */}
      {activeProvisions.length > 0 && !clusters.some(cl => local && cl.cluster_id === thisClusterId) && (
        <div className="cluster-block">
          <div className="cluster-block-head">
            <div className="row" style={{ gap: "0.5rem" }}>
              <strong>Provisioning</strong>
            </div>
          </div>
          <div className="cluster-block-nodes">
            {activeProvisions.map(provisionRow)}
          </div>
        </div>
      )}

      {lonely && (
        <p className="dim" style={{ marginTop: "0.6rem", maxWidth: "58ch" }}>
          This is your only homebox — install Homebox on another machine and link the same
          account to grow a cluster.
        </p>
      )}

      {actions.createCluster && local && !clustered && (
        <>
          {clusters.length > 0 && <div className="section-divider"><span>or</span></div>}
          <div className="row" style={{ marginTop: clusters.length > 0 ? 0 : "0.9rem", gap: "0.5rem" }}>
            <input value={newClusterName} onChange={e => setNewClusterName(e.target.value)}
                   placeholder="cluster name" style={{ maxWidth: 200 }} />
            <button className="btn primary" onClick={() => actions.createCluster!(newClusterName.trim())}
              disabled={busy || clusterLocked || !newClusterName.trim()}
              title={clusterLocked ? "Upgrade to Homebox Premium to create a cluster" : undefined}>
              <Plus size={14} /> Start a new cluster
            </button>
          </div>
          <div className="dim" style={{ marginTop: "0.35rem" }}>
            This node becomes the seed of the new cluster.
          </div>
        </>
      )}

      {/* ── Evict confirm ── */}
      <Modal
        open={confirmEvict != null}
        title="Evict node?"
        onClose={() => setConfirmEvict(null)}
        footer={
          <>
            <button className="btn ghost" onClick={() => setConfirmEvict(null)}>Cancel</button>
            <button className="btn danger"
              onClick={() => {
                if (confirmEvict) actions.evict?.(confirmEvict.clusterId, confirmEvict.node.node_id);
                setConfirmEvict(null);
              }}>
              <UserMinus size={14} /> Evict
            </button>
          </>
        }
      >
        <p>
          <strong>{confirmEvict?.node.name || confirmEvict?.node.node_id}</strong> is removed from its
          cluster and its replication links are cleaned up. Use this for dead or unreachable nodes —
          prefer Leave on the node itself when it's healthy.
        </p>
      </Modal>

      {/* ── Split off confirm ── */}
      <Modal
        open={splitOffNode != null}
        title="Split off into a new cluster"
        onClose={() => setSplitOffNode(null)}
        footer={
          <>
            <button className="btn ghost" onClick={() => setSplitOffNode(null)}>Cancel</button>
            <button className="btn danger"
              disabled={!splitOffName.trim()}
              onClick={() => {
                if (splitOffNode) actions.splitOff?.(splitOffNode.node_id, splitOffName.trim());
                setSplitOffNode(null);
              }}>
              <Split size={14} /> Split off
            </button>
          </>
        }
      >
        <label>New cluster name
          <input value={splitOffName} onChange={e => setSplitOffName(e.target.value)} placeholder="home" autoFocus />
        </label>
        <p className="dim" style={{ marginTop: "0.85rem" }}>
          <strong>{splitOffNode?.name || splitOffNode?.node_id}</strong> leaves its cluster and
          immediately founds a new one, keeping its projects and data. Other nodes are unaffected.
          The node applies this within a minute of its next check-in.
        </p>
      </Modal>

      {/* ── Split cluster (node picker) ── */}
      <Modal
        open={splitCluster != null}
        title={`Split ${splitCluster?.name ?? "cluster"}`}
        onClose={() => setSplitCluster(null)}
        footer={
          <>
            <button className="btn ghost" onClick={() => setSplitCluster(null)}>Cancel</button>
            <button className="btn danger"
              disabled={
                !splitClusterName.trim() ||
                splitPicked.size === 0 ||
                splitPicked.size >= (splitCluster?.nodes.filter(n => n.role !== "mirror").length ?? 0)
              }
              onClick={() => {
                if (splitCluster) {
                  actions.splitCluster?.(splitCluster.cluster_id, [...splitPicked], splitClusterName.trim());
                }
                setSplitCluster(null);
              }}>
              <Split size={14} /> Split cluster
            </button>
          </>
        }
      >
        <p className="dim">
          Pick the nodes that move into a new cluster. At least one node must stay behind.
        </p>
        <div className="node-picker">
          {(splitCluster?.nodes ?? []).filter(n => n.role !== "mirror").map(n => (
            <label key={n.node_id} className="row" style={{ gap: "0.5rem" }}>
              <input type="checkbox" checked={splitPicked.has(n.node_id)}
                onChange={e => {
                  const next = new Set(splitPicked);
                  if (e.target.checked) next.add(n.node_id); else next.delete(n.node_id);
                  setSplitPicked(next);
                }} />
              <strong>{n.name || n.node_id}</strong>
              {n.node_id === thisNodeId && <span className="chip active">you are here</span>}
              <span className="dim">{n.peer_url}</span>
            </label>
          ))}
        </div>
        <label style={{ display: "block", marginTop: "0.85rem" }}>New cluster name
          <input value={splitClusterName} onChange={e => setSplitClusterName(e.target.value)} placeholder="home" />
        </label>
      </Modal>

      {/* ── Cloud node provision (AWS/GCP) ── */}
      <Modal
        open={provisionOpen}
        title="Add a cloud node"
        onClose={() => setProvisionOpen(false)}
        footer={
          <>
            <button className="btn ghost" onClick={() => setProvisionOpen(false)}>Cancel</button>
            <button className="btn primary" disabled={!provisionValid || busy}
              onClick={() => {
                actions.provision?.({
                  name: provName.trim(),
                  provider: provProvider,
                  integration_id: provIntegrationId as number,
                  region: provRegion.trim(),
                  ...(provMachine.trim() ? { machine: provMachine.trim() } : {}),
                });
                setProvisionOpen(false);
              }}>
              <CloudCog size={14} /> Provision node
            </button>
          </>
        }
      >
        <p className="dim">
          Boots a small VM on your own cloud account, installs Homebox, and joins it to this
          cluster automatically. You pay the cloud provider directly for the VM.
        </p>
        <div style={{ display: "grid", gap: "0.7rem", marginTop: "0.7rem" }}>
          <label>Node name
            <input value={provName} onChange={e => setProvName(e.target.value)} placeholder="cloud-1" autoFocus />
          </label>
          <label>Provider
            <select value={provProvider} onChange={e => pickProvider(e.target.value as "aws" | "gcp")}>
              <option value="aws">AWS (EC2)</option>
              <option value="gcp">GCP (Compute Engine)</option>
            </select>
          </label>
          <label>Integration
            <select value={provIntegrationId === "" ? "" : String(provIntegrationId)}
              onChange={e => setProvIntegrationId(e.target.value === "" ? "" : Number(e.target.value))}>
              {provIntegrations.length === 0 && <option value="">No {provProvider.toUpperCase()} integrations linked</option>}
              {provIntegrations.map(i => (
                <option key={i.id} value={String(i.id)}>
                  {i.name || i.account_login || `${i.provider} #${i.id}`}
                </option>
              ))}
            </select>
          </label>
          <label>Region
            <input value={provRegion} onChange={e => setProvRegion(e.target.value)}
              placeholder={provProvider === "aws" ? "us-east-1" : "us-central1"} />
          </label>
          <label>Machine type (optional — a small default is used)
            <input value={provMachine} onChange={e => setProvMachine(e.target.value)}
              placeholder={provProvider === "aws" ? "t3.small" : "e2-small"} />
          </label>
        </div>
      </Modal>
    </div>
  );
}
