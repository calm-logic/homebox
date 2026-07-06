import { FormEvent, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, LogIn, Network, Plus, Power, PowerOff, RefreshCw, Send, Ticket, Unplug, UserMinus } from "lucide-react";
import { api } from "../lib/api";
import { Modal } from "../components/Modal";
import { useToast } from "../lib/toast";

type ClusterNode = {
  node_id: string;
  name: string;
  peer_url: string;
  version: string;
  ordinal?: number;
  online: boolean;
  serving?: boolean;
};

type ClusterStatus = {
  active: boolean;
  node_id: string;
  cluster_id?: string;
  name?: string;
  roster?: ClusterNode[];
  license?: { valid: boolean; plan: string; max_nodes: number; node_count: number };
  initial_sync_done?: boolean;
  last_heartbeat?: string | null;
  last_sync_at?: string | null;
  control_plane_url?: string;
};

type AccountNode = { node_id: string; name: string; peer_url: string; online: boolean };
type AccountCluster = { cluster_id: string; name: string; nodes: ClusterNode[] };
type AccountStatus = {
  linked: boolean;
  node_name?: string;
  peer_url?: string;
  control_plane_url?: string;
  overview?: { nodes?: AccountNode[]; clusters?: AccountCluster[]; polled_at?: string };
};

export function Cluster() {
  const qc = useQueryClient();
  const toast = useToast();
  const [confirmLeave, setConfirmLeave] = useState(false);
  const [stopTunnel, setStopTunnel] = useState(true);
  const [teardownStacks, setTeardownStacks] = useState(false);
  const [mintedToken, setMintedToken] = useState<string | null>(null);
  const [joining, setJoining] = useState(false);
  const [showManualJoin, setShowManualJoin] = useState(false);
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
    refetchInterval: joining ? 3000 : 15000,
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
    onError: onErr,
  });
  const joinCluster = useMutation({
    mutationFn: (cluster_id: string) => api.post("/api/cluster/account/join", { cluster_id }),
    onSuccess: () => { setJoining(true); toast.show("Joining — restarting onto the cluster keys…", "ok"); },
    onError: onErr,
  });
  const inviteNode = useMutation({
    mutationFn: (node_id: string) => api.post("/api/cluster/account/invite", { node_id }),
    onSuccess: (_d, node_id) => toast.show(`Invited ${node_id} — it joins automatically within a minute`, "ok"),
    onError: onErr,
  });
  const evict = useMutation({
    mutationFn: (node_id: string) => api.post("/api/cluster/evict", { node_id }),
    onSuccess: () => { invalidate(); toast.show("Node evicted — replication links are being cleaned up", "ok"); },
    onError: onErr,
  });
  const setServing = useMutation({
    mutationFn: (v: { node_id: string; serving: boolean }) => api.post("/api/cluster/node/serving", v),
    onSuccess: (_d, v) => {
      invalidate();
      toast.show(
        v.serving
          ? "Node enabled — resuming app traffic"
          : "Node disabled — app traffic draining to peers (roster updates within a minute)",
        "ok",
      );
    },
    onError: onErr,
  });
  const manualJoin = useMutation({
    mutationFn: () => api.post("/api/cluster/join", {
      join_token: joinToken, peer_url: peerUrl, node_name: nodeName,
      control_plane_url: cpUrl.trim() || null,
    }),
    onSuccess: () => { setJoining(true); toast.show("Joined — restarting onto the cluster keys…", "ok"); },
    onError: onErr,
  });
  const mint = useMutation({
    mutationFn: () => api.post<{ join_token: string }>("/api/cluster/join-token"),
    onSuccess: (d) => setMintedToken(d.join_token),
    onError: onErr,
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

  const overview = account?.overview ?? {};
  const rosterIds = new Set((status?.roster ?? []).map(n => n.node_id));
  const invitableNodes = (overview.nodes ?? []).filter(n => !rosterIds.has(n.node_id));

  function submitLink(e: FormEvent) {
    e.preventDefault();
    if (!peerUrl.trim()) { toast.show("Set this node's LAN address", "fail"); return; }
    link.mutate();
  }

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
                  <button className="btn primary" onClick={() => joinCluster.mutate(cl.cluster_id)} disabled={joinCluster.isPending}>
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
            <button className="btn primary" onClick={() => createCluster.mutate()} disabled={createCluster.isPending}>
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
      <h1>Cluster</h1>
      <p className="lede">
        Active-active clustering: every node serves the same apps through the shared tunnel,
        with replicated databases and automatic failover.
      </p>

      {joining && (
        <div className="card"><span className="spinner" /> Restarting onto the cluster keys — hang tight…</div>
      )}

      {accountSection}

      {status?.active && (
        <div className="card">
          <div className="card-row">
            <div className="row">
              <span className="badge ok"><Network size={12} /> {status.name} · {status.cluster_id}</span>
              {status.license && (
                <span className="badge">{status.license.node_count}/{status.license.max_nodes} nodes · {status.license.plan}</span>
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
            const serving = n.serving !== false;
            const isSelf = n.node_id === status.node_id;
            return (
            <div key={n.node_id} className="row" style={{ justifyContent: "space-between", gap: "0.75rem", flexWrap: "wrap", padding: "0.4rem 0" }}>
              <div className="row" style={{ gap: "0.5rem", minWidth: 0 }}>
                <span className={`badge ${n.online ? "ok" : "fail"}`}>{n.online ? "online" : "offline"}</span>
                <strong>{n.name || n.node_id}</strong>
                {n.ordinal != null && <span className="dim">n{n.ordinal}</span>}
                {isSelf && <span className="chip active">this node</span>}
                {!serving && <span className="badge warn">disabled</span>}
              </div>
              <div className="row" style={{ gap: "0.75rem" }}>
                <span className="dim">{n.peer_url || "no peer URL"}</span>
                <button className="btn ghost"
                  title={serving
                    ? (isSelf
                        ? "Drain app traffic from this node. The control plane stays connected — re-enable from this admin on the LAN."
                        : "Drain app traffic from this node so the shared tunnel routes to healthy peers. Control plane stays connected.")
                    : "Resume app traffic on this node"}
                  onClick={() => setServing.mutate({ node_id: n.node_id, serving: !serving })}
                  disabled={setServing.isPending}>
                  {serving ? <><PowerOff size={14} /> Disable</> : <><Power size={14} /> Enable</>}
                </button>
                {!isSelf && (
                  <button className="btn ghost danger" title="Remove this node from the cluster (for dead/unreachable nodes — use Leave on the node itself when possible)"
                    onClick={() => evict.mutate(n.node_id)} disabled={evict.isPending}>
                    <UserMinus size={14} /> Evict
                  </button>
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
              <div><button className="btn primary" type="submit" disabled={manualJoin.isPending}>Join cluster</button></div>
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
    </>
  );
}
