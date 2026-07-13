import { Boxes, Cloud, Plus, Send } from "lucide-react";
import type { AccountOverview } from "../lib/types";
import { timeAgo } from "../lib/time";

/**
 * "Your homeboxes" — the cloud registry rendered whenever a homebox.sh
 * account is linked. Shows every cluster (with per-node live status) and
 * every standalone node on the account, from any homebox:
 *
 *  - clustered here  → standalone nodes get an "Invite to this cluster" action
 *  - standalone here → cluster blocks get a "Join this cluster" action, and a
 *    "Start a new cluster" affordance sits below (an intentional either/or)
 */
export function CloudRegistry({
  overview,
  thisNodeId,
  thisClusterId,
  clustered,
  clusterLocked,
  onJoin,
  joinPending,
  onInvite,
  invitePending,
  newClusterName,
  onNewClusterName,
  onCreateCluster,
  createPending,
}: {
  overview: AccountOverview;
  thisNodeId?: string;
  thisClusterId?: string;
  /** Whether THIS node is currently in a cluster. */
  clustered: boolean;
  clusterLocked: boolean;
  onJoin: (cluster_id: string) => void;
  joinPending: boolean;
  onInvite: (node_id: string) => void;
  invitePending: boolean;
  newClusterName: string;
  onNewClusterName: (v: string) => void;
  onCreateCluster: () => void;
  createPending: boolean;
}) {
  const clusters = overview.clusters ?? [];
  const nodes = overview.nodes ?? [];
  // Clustered nodes are already shown inside their cluster block — the flat
  // node list only contributes the standalone ones. Old control planes omit
  // cluster_id entirely, so also drop any node visible in a cluster roster.
  const rosterIds = new Set(clusters.flatMap(cl => cl.nodes.map(n => n.node_id)));
  const standalone = nodes.filter(n => !n.cluster_id && !rosterIds.has(n.node_id));
  const lonely =
    clusters.length === 0 && nodes.every(n => n.node_id === thisNodeId);

  return (
    <div className="cloud-registry">
      <div className="row" style={{ marginTop: "1.1rem", justifyContent: "space-between" }}>
        <h3 style={{ margin: 0, display: "inline-flex", alignItems: "center", gap: "0.4rem" }}>
          <Boxes size={14} /> Your homeboxes
        </h3>
        {overview.polled_at && <span className="dim">updated {timeAgo(overview.polled_at)}</span>}
      </div>

      {clusters.map(cl => {
        const isThisCluster = clustered && cl.cluster_id === thisClusterId;
        return (
          <div key={cl.cluster_id} className={`cluster-block ${isThisCluster ? "current" : ""}`}>
            <div className="cluster-block-head">
              <div className="row" style={{ gap: "0.5rem", minWidth: 0 }}>
                <strong>{cl.name}</strong>
                <span className="dim">{cl.cluster_id}</span>
                <span className="badge plain">{cl.nodes.length} node{cl.nodes.length === 1 ? "" : "s"}</span>
                {isThisCluster && <span className="chip active">this cluster</span>}
              </div>
              {!clustered && (
                <button className="btn primary small" onClick={() => onJoin(cl.cluster_id)}
                  disabled={joinPending || clusterLocked}
                  title={clusterLocked ? "Upgrade to Homebox Premium to join a cluster" : undefined}>
                  Join this cluster
                </button>
              )}
            </div>
            <div className="cluster-block-nodes">
              {cl.nodes.map(n => (
                <div key={n.node_id} className="registry-node">
                  <span className={`badge ${n.online ? "ok" : "fail"}`}>{n.online ? "online" : "offline"}</span>
                  {n.role === "mirror"
                    ? <span className="badge info"><Cloud size={12} /> Cloud Mirror</span>
                    : <strong>{n.name || n.node_id}</strong>}
                  {n.node_id === thisNodeId && <span className="chip active">this node</span>}
                  {n.role !== "mirror" && n.serving === false && <span className="badge warn">disabled</span>}
                  <span className="spacer" />
                  {n.version && <span className="dim">{n.version}</span>}
                  {n.peer_url && <span className="dim">{n.peer_url}</span>}
                </div>
              ))}
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
            {standalone.map(n => {
              const backup = timeAgo(n.backup_updated_at);
              return (
                <div key={n.node_id} className="registry-node">
                  <span className={`badge ${n.online ? "ok" : "fail"}`}>{n.online ? "online" : "offline"}</span>
                  <strong>{n.name || n.node_id}</strong>
                  {n.node_id === thisNodeId && <span className="chip active">this node</span>}
                  <span className="dim">{n.peer_url}</span>
                  {backup && <span className="dim">backup {backup}</span>}
                  <span className="spacer" />
                  {clustered && n.node_id !== thisNodeId && (
                    <button className="btn ghost small" onClick={() => onInvite(n.node_id)} disabled={invitePending}>
                      <Send size={14} /> Invite to this cluster
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {lonely && (
        <p className="dim" style={{ marginTop: "0.6rem", maxWidth: "58ch" }}>
          This is your only homebox — install Homebox on another machine and link the same
          account to grow a cluster.
        </p>
      )}

      {!clustered && (
        <>
          {clusters.length > 0 && <div className="section-divider"><span>or</span></div>}
          <div className="row" style={{ marginTop: clusters.length > 0 ? 0 : "0.9rem", gap: "0.5rem" }}>
            <input value={newClusterName} onChange={e => onNewClusterName(e.target.value)}
                   placeholder="cluster name" style={{ maxWidth: 200 }} />
            <button className="btn primary" onClick={onCreateCluster}
              disabled={createPending || clusterLocked}
              title={clusterLocked ? "Upgrade to Homebox Premium to create a cluster" : undefined}>
              {createPending ? <span className="spinner" /> : <><Plus size={14} /> Start a new cluster</>}
            </button>
          </div>
          <div className="dim" style={{ marginTop: "0.35rem" }}>
            This node becomes the seed of the new cluster.
          </div>
        </>
      )}
    </div>
  );
}
