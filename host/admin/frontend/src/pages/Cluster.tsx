import { FormEvent, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, Network, Plus, RefreshCw, Ticket, Unplug } from "lucide-react";
import { api } from "../lib/api";
import { Modal } from "../components/Modal";
import { useToast } from "../lib/toast";

type ClusterNode = {
  node_id: string;
  name: string;
  peer_url: string;
  version: string;
  online: boolean;
  last_seen: number | null;
};

type ClusterStatus = {
  active: boolean;
  node_id: string;
  cluster_id?: string;
  name?: string;
  node_name?: string;
  peer_url?: string;
  control_plane_url?: string;
  roster?: ClusterNode[];
  license?: { valid: boolean; plan: string; max_nodes: number; node_count: number };
  initial_sync_done?: boolean;
  last_heartbeat?: string | null;
  last_sync_at?: string | null;
};

export function Cluster() {
  const qc = useQueryClient();
  const toast = useToast();
  const [mode, setMode] = useState<"create" | "join">("create");
  const [name, setName] = useState("home");
  const [accountToken, setAccountToken] = useState("");
  const [joinToken, setJoinToken] = useState("");
  const [peerUrl, setPeerUrl] = useState("");
  const [nodeName, setNodeName] = useState("");
  const [cpUrl, setCpUrl] = useState("");
  const [confirmLeave, setConfirmLeave] = useState(false);
  const [mintedToken, setMintedToken] = useState<string | null>(null);
  const [joining, setJoining] = useState(false);

  const { data: status } = useQuery<ClusterStatus>({
    queryKey: ["cluster"],
    queryFn: () => api.get<ClusterStatus>("/api/cluster"),
    // While a join restart is in flight, poll until the node comes back.
    refetchInterval: joining ? 3000 : 30000,
    retry: true,
  });

  const create = useMutation({
    mutationFn: () =>
      api.post("/api/cluster/create", {
        name, account_token: accountToken, peer_url: peerUrl,
        node_name: nodeName, control_plane_url: cpUrl.trim() || null,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["cluster"] });
      toast.show("Cluster created — this node is the seed", "ok");
    },
    onError: (e) => toast.show(String(e), "fail"),
  });

  const join = useMutation({
    mutationFn: () =>
      api.post("/api/cluster/join", {
        join_token: joinToken, peer_url: peerUrl,
        node_name: nodeName, control_plane_url: cpUrl.trim() || null,
      }),
    onSuccess: () => {
      setJoining(true);
      toast.show("Joined! Restarting to adopt cluster keys — this page will catch up shortly.", "ok");
    },
    onError: (e) => toast.show(String(e), "fail"),
  });

  const mint = useMutation({
    mutationFn: () => api.post<{ join_token: string }>("/api/cluster/join-token"),
    onSuccess: (d) => setMintedToken(d.join_token),
    onError: (e) => toast.show(String(e), "fail"),
  });

  const sync = useMutation({
    mutationFn: () => api.post("/api/cluster/sync"),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["cluster"] });
      toast.show("Sync triggered", "ok");
    },
    onError: (e) => toast.show(String(e), "fail"),
  });

  const leave = useMutation({
    mutationFn: () => api.post("/api/cluster/leave"),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["cluster"] });
      setConfirmLeave(false);
      toast.show("Left the cluster", "ok");
    },
    onError: (e) => toast.show(String(e), "fail"),
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    if (!peerUrl.trim()) {
      toast.show("Set this node's LAN address (peers reach it there)", "fail");
      return;
    }
    if (mode === "create") create.mutate();
    else join.mutate();
  }

  if (status?.active) {
    const roster = status.roster ?? [];
    return (
      <>
        <h1>Cluster</h1>
        <p className="lede">
          Active-active cluster <strong>{status.name}</strong> — every node serves the same
          apps; traffic arrives via the shared Cloudflare tunnel on whichever node is closest.
        </p>

        <div className="card">
          <div className="card-row">
            <div className="row">
              <span className="badge ok"><Network size={12} /> {status.cluster_id}</span>
              {status.license && (
                <span className="badge">{status.license.node_count}/{status.license.max_nodes} nodes · {status.license.plan}</span>
              )}
              {!status.initial_sync_done && <span className="badge warn">initial sync pending…</span>}
            </div>
            <div className="row" style={{ gap: "0.5rem" }}>
              <button className="ghost" onClick={() => sync.mutate()} disabled={sync.isPending}>
                <RefreshCw size={14} /> Sync now
              </button>
              <button className="ghost" onClick={() => mint.mutate()} disabled={mint.isPending}>
                <Ticket size={14} /> Mint join token
              </button>
              <button className="ghost danger" onClick={() => setConfirmLeave(true)}>
                <Unplug size={14} /> Leave
              </button>
            </div>
          </div>

          {mintedToken && (
            <div className="card-row" style={{ marginTop: "0.75rem" }}>
              <code style={{ wordBreak: "break-all", userSelect: "all" }}>{mintedToken}</code>
              <button
                className="ghost"
                onClick={() => { navigator.clipboard.writeText(mintedToken); toast.show("Copied", "ok"); }}
              >
                <Copy size={14} /> Copy
              </button>
            </div>
          )}
          {mintedToken && (
            <div className="dim" style={{ marginTop: "0.4rem" }}>
              Paste this on the new node's Cluster page (single use, expires in 48h).
            </div>
          )}
        </div>

        <div className="card">
          <h3>Nodes</h3>
          {roster.map((n) => (
            <div key={n.node_id} className="row" style={{ justifyContent: "space-between", gap: "0.75rem", flexWrap: "wrap", padding: "0.4rem 0" }}>
              <div className="row" style={{ gap: "0.5rem", minWidth: 0 }}>
                <span className={`badge ${n.online ? "ok" : "fail"}`}>{n.online ? "online" : "offline"}</span>
                <strong>{n.name || n.node_id}</strong>
                {n.node_id === status.node_id && <span className="chip active">this node</span>}
              </div>
              <div className="row" style={{ gap: "0.75rem" }}>
                <span className="dim">{n.peer_url || "no peer URL"}</span>
                <span className="dim">{n.version}</span>
              </div>
            </div>
          ))}
          <div className="dim" style={{ marginTop: "0.6rem" }}>
            Last heartbeat {status.last_heartbeat ?? "never"} · last config sync {status.last_sync_at ?? "never"}
          </div>
        </div>

        <Modal
          open={confirmLeave}
          title="Leave cluster?"
          onClose={() => setConfirmLeave(false)}
          footer={
            <>
              <button className="ghost" onClick={() => setConfirmLeave(false)}>Cancel</button>
              <button className="danger" onClick={() => leave.mutate()} disabled={leave.isPending}>Leave cluster</button>
            </>
          }
        >
          Running app stacks stay up and keys are not rotated — this only removes the node
          from the roster and stops config/deploy sync.
        </Modal>
      </>
    );
  }

  return (
    <>
      <h1>Cluster</h1>
      <p className="lede">
        Run this homebox as part of an active-active cluster: every node deploys the same
        apps, shares the Cloudflare tunnel, and replicates app databases. Requires a
        homebox.sh account.
      </p>

      {joining && (
        <div className="card">
          <span className="spinner" /> Restarting onto the cluster keys — hang tight…
        </div>
      )}

      <div className="card">
        <div className="mode-chips" style={{ marginBottom: "0.9rem" }}>
          <span className={`chip ${mode === "create" ? "active" : ""}`} onClick={() => setMode("create")}>
            <Plus size={12} /> Create a cluster
          </span>
          <span className={`chip ${mode === "join" ? "active" : ""}`} onClick={() => setMode("join")}>
            <Network size={12} /> Join a cluster
          </span>
        </div>

        <form onSubmit={submit} className="stack" style={{ display: "grid", gap: "0.7rem", maxWidth: 560 }}>
          {mode === "create" ? (
            <>
              <label>Cluster name
                <input value={name} onChange={(e) => setName(e.target.value)} placeholder="home" />
              </label>
              <label>homebox.sh account token
                <input value={accountToken} onChange={(e) => setAccountToken(e.target.value)}
                       placeholder="any token in dev mode" />
              </label>
            </>
          ) : (
            <label>Join token (minted on an existing node)
              <input value={joinToken} onChange={(e) => setJoinToken(e.target.value)}
                     placeholder="hbj.hbc_…" />
            </label>
          )}
          <label>This node's LAN address (peers connect here, port 80)
            <input value={peerUrl} onChange={(e) => setPeerUrl(e.target.value)}
                   placeholder="http://192.168.1.10" />
          </label>
          <label>Node name (optional)
            <input value={nodeName} onChange={(e) => setNodeName(e.target.value)} placeholder="basement-mini" />
          </label>
          <label>Control plane URL (leave empty for cluster.homebox.sh)
            <input value={cpUrl} onChange={(e) => setCpUrl(e.target.value)}
                   placeholder={status?.control_plane_url || "https://cluster.homebox.sh"} />
          </label>
          <div>
            <button type="submit" disabled={create.isPending || join.isPending}>
              {mode === "create" ? "Create cluster" : "Join cluster"}
            </button>
          </div>
        </form>
      </div>
    </>
  );
}
