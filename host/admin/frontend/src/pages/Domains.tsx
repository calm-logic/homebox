import { FormEvent, useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2, Cloud } from "lucide-react";
import { api } from "../lib/api";
import { Modal } from "../components/Modal";
import { useToast } from "../lib/toast";
import type { CloudflareZone, DomainItem, TunnelStatus } from "../lib/types";

export function Domains() {
  const qc = useQueryClient();
  const toast = useToast();
  const [openManual, setOpenManual] = useState(false);
  const [openCf, setOpenCf] = useState(false);

  const { data: domains } = useQuery<DomainItem[]>({
    queryKey: ["domains"],
    queryFn: () => api.get<DomainItem[]>("/api/domains"),
  });
  const { data: tunnel } = useQuery<TunnelStatus>({
    queryKey: ["tunnel"],
    queryFn: () => api.get<TunnelStatus>("/api/tunnel"),
  });

  const remove = useMutation({
    mutationFn: (id: number) => api.del(`/api/domains/${id}`),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["domains"] }); toast.show("Domain removed", "ok"); },
    onError: (e) => toast.show(String(e), "fail"),
  });

  const cfReady = !!tunnel?.cloudflare.token_set && !!tunnel?.tunnel_id && tunnel.mode === "remote";

  return (
    <>
      <div className="row" style={{ marginTop: "2rem" }}>
        <div className="spacer" />
        {cfReady && (
          <button className="btn primary" onClick={() => setOpenCf(true)}>
            <Cloud size={14} /> Connect from Cloudflare
          </button>
        )}
        <button className="btn" onClick={() => setOpenManual(true)}><Plus size={14} /> Add manually</button>
      </div>
      <p className="dim" style={{ marginTop: "0.4rem", marginBottom: "1rem" }}>A <strong>wildcard</strong> domain hosts many projects (each at its own subdomain). A <strong>dedicated</strong> domain is for a single project.</p>

      {!cfReady && (
        <div className="card" style={{ marginBottom: "1rem", borderColor: "var(--border-strong)" }}>
          <div className="row">
            <span className="badge warn">Cloudflare not connected</span>
            <span className="dim">
              Connect a Cloudflare token and tunnel above to add domains automatically (DNS records + ingress in one click).
            </span>
          </div>
        </div>
      )}

      {domains && domains.length > 0 ? (
        <table className="data-table">
          <thead>
            <tr><th>Domain</th><th>Mode</th><th>Primary</th><th>Routed</th><th className="right">Actions</th></tr>
          </thead>
          <tbody>
            {domains.map(d => (
              <tr key={d.id}>
                <td><strong>{d.name}</strong></td>
                <td>{d.mode === "wildcard" ? <span className="badge info plain">Wildcard</span> : <span className="badge plain">Dedicated</span>}</td>
                <td>{d.is_primary && <span className="badge ok">Primary</span>}</td>
                <td>{d.cloudflare_routed ? <span className="badge ok plain">CF</span> : <span className="dim">manual</span>}</td>
                <td className="actions">
                  <button className="btn small danger"
                    disabled={remove.isPending}
                    onClick={() => { if (confirm(`Remove ${d.name}?`)) remove.mutate(d.id); }}>
                    <Trash2 size={12} /> Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : domains ? (
        <div className="empty-state">
          <h3>No domains yet</h3>
          <p>Add your first Cloudflare-managed domain to start routing projects to this host.</p>
          {cfReady ? (
            <button className="btn primary" onClick={() => setOpenCf(true)}><Cloud size={14} /> Connect from Cloudflare</button>
          ) : (
            <button className="btn primary" onClick={() => setOpenManual(true)}>Add a domain</button>
          )}
        </div>
      ) : <span className="spinner" />}

      <AddDomainModal open={openManual} onClose={() => setOpenManual(false)} />
      <ConnectCloudflareDomainModal open={openCf} onClose={() => setOpenCf(false)} />
    </>
  );
}

// ─── Manual add (no Cloudflare API call — DNS must already exist) ─────────────

function AddDomainModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [name, setName] = useState("");
  const [mode, setMode] = useState<"wildcard" | "dedicated">("wildcard");
  const [primary, setPrimary] = useState(false);

  const add = useMutation({
    mutationFn: () => api.post("/api/domains", { name, mode, primary }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["domains"] });
      toast.show("Domain added — click Apply ingress on the Routes page to route it.", "ok");
      setName(""); setPrimary(false); setMode("wildcard");
      onClose();
    },
    onError: (e) => toast.show(String(e), "fail"),
  });

  function submit(e: FormEvent) { e.preventDefault(); add.mutate(); }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Add a domain manually"
      footer={<>
        <span className="spacer" />
        <button className="btn" type="button" onClick={onClose}>Cancel</button>
        <button className="btn primary" type="submit" form="add-domain-form" disabled={add.isPending}>
          {add.isPending ? <span className="spinner" /> : "Add domain"}
        </button>
      </>}
    >
      <form id="add-domain-form" onSubmit={submit}>
        <div className="field">
          <label className="lbl">Domain name</label>
          <input value={name} onChange={e => setName(e.target.value)} placeholder="example.com" required />
          <span className="hint">You'll need to configure DNS yourself. For automatic DNS, use <strong>Connect from Cloudflare</strong>.</span>
        </div>
        <div className="field">
          <label className="lbl">Mode</label>
          <div className="mode-chips">
            <span className={`chip ${mode === "wildcard" ? "active" : ""}`} onClick={() => setMode("wildcard")}>Wildcard (root + subdomains)</span>
            <span className={`chip ${mode === "dedicated" ? "active" : ""}`} onClick={() => setMode("dedicated")}>Dedicated (one project)</span>
          </div>
        </div>
        <div className="field">
          <label style={{ display: "flex", gap: "0.5rem", alignItems: "center", textTransform: "none", letterSpacing: 0, color: "var(--text)" }}>
            <input type="checkbox" checked={primary} onChange={e => setPrimary(e.target.checked)} style={{ width: "auto" }} />
            Make this the primary domain
          </label>
        </div>
      </form>
    </Modal>
  );
}

// ─── Cloudflare zone picker — DNS + ingress in one step ───────────────────────

function ConnectCloudflareDomainModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [zoneId, setZoneId] = useState("");
  const [mode, setMode] = useState<"wildcard" | "dedicated">("wildcard");
  const [primary, setPrimary] = useState(false);

  const { data: zones, isFetching, error } = useQuery<CloudflareZone[]>({
    queryKey: ["cf-zones"],
    queryFn: () => api.get<CloudflareZone[]>("/api/tunnel/zones"),
    enabled: open,
    staleTime: 60_000,
  });

  // Reset zone selection when the modal opens.
  useEffect(() => { if (open) setZoneId(""); }, [open]);

  const connect = useMutation({
    mutationFn: () => api.post("/api/domains/connect-cloudflare", {
      zone_id: zoneId, mode, primary,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["domains"] });
      qc.invalidateQueries({ queryKey: ["tunnel"] });
      toast.show("Domain connected — DNS and ingress updated", "ok");
      setZoneId(""); setPrimary(false); setMode("wildcard");
      onClose();
    },
    onError: (e) => toast.show(String(e), "fail"),
  });

  function submit(e: FormEvent) { e.preventDefault(); connect.mutate(); }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Connect a domain from Cloudflare"
      footer={<>
        <span className="spacer" />
        <button className="btn" type="button" onClick={onClose}>Cancel</button>
        <button className="btn primary" type="submit" form="cf-connect-domain-form" disabled={connect.isPending || !zoneId}>
          {connect.isPending ? <span className="spinner" /> : "Connect domain"}
        </button>
      </>}
    >
      <form id="cf-connect-domain-form" onSubmit={submit}>
        <div className="field">
          <label className="lbl">Zone</label>
          {isFetching && !zones ? <span className="spinner" /> : null}
          {error && <div className="badge fail">Failed to load zones: {String(error)}</div>}
          {zones && zones.length === 0 && <div className="dim">No zones in your Cloudflare account yet — add one to Cloudflare first.</div>}
          {zones && zones.length > 0 && (
            <select value={zoneId} onChange={e => setZoneId(e.target.value)} required>
              <option value="" disabled>Pick a domain…</option>
              {zones.map(z => (
                <option key={z.id} value={z.id}>
                  {z.name}{z.status !== "active" ? ` (${z.status})` : ""}
                </option>
              ))}
            </select>
          )}
          <span className="hint">DNS records (apex + wildcard) will be created automatically pointing at your tunnel.</span>
        </div>
        <div className="field">
          <label className="lbl">Mode</label>
          <div className="mode-chips">
            <span className={`chip ${mode === "wildcard" ? "active" : ""}`} onClick={() => setMode("wildcard")}>Wildcard (root + subdomains)</span>
            <span className={`chip ${mode === "dedicated" ? "active" : ""}`} onClick={() => setMode("dedicated")}>Dedicated (one project)</span>
          </div>
        </div>
        <div className="field">
          <label style={{ display: "flex", gap: "0.5rem", alignItems: "center", textTransform: "none", letterSpacing: 0, color: "var(--text)" }}>
            <input type="checkbox" checked={primary} onChange={e => setPrimary(e.target.checked)} style={{ width: "auto" }} />
            Make this the primary domain
          </label>
        </div>
      </form>
    </Modal>
  );
}
