import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2, Cloud, Wrench, Copy, Star } from "lucide-react";
import { api } from "../lib/api";
import { Modal } from "../components/Modal";
import { useToast } from "../lib/toast";
import type { DomainItem, IntegrationItem, TunnelStatus } from "../lib/types";

/**
 * Domains — the page formerly known as Routes, reduced to what it's about:
 * the domains routed to this host. Cloudflare/tunnel plumbing lives on the
 * Cloudflare integration page; uptime lives on System; DNS drift is checked
 * hourly by the monitor and surfaces here only as a banner when broken.
 */

interface DnsStatus {
  checked_at: string | null;
  in_sync: boolean;
  issues: string[];
  repaired: string[];
}

export function DomainsPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const [addOpen, setAddOpen] = useState(false);

  const { data: domains } = useQuery<DomainItem[]>({
    queryKey: ["domains"],
    queryFn: () => api.get<DomainItem[]>("/api/domains"),
    refetchInterval: 15000, // pending-NS rows flip active in the background
  });
  const { data: tunnel } = useQuery<TunnelStatus>({
    queryKey: ["tunnel"],
    queryFn: () => api.get<TunnelStatus>("/api/tunnel"),
  });
  const { data: integrations } = useQuery<IntegrationItem[]>({
    queryKey: ["integrations"],
    queryFn: () => api.get<IntegrationItem[]>("/api/integrations"),
  });
  const { data: dns } = useQuery<DnsStatus>({
    queryKey: ["dns-status"],
    queryFn: () => api.get<DnsStatus>("/api/tunnel/dns-status"),
    refetchInterval: 60000,
  });

  const remove = useMutation({
    mutationFn: (id: number) => api.del(`/api/domains/${id}`),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["domains"] }); toast.show("Domain removed", "ok"); },
    onError: (e) => toast.show(String(e), "fail"),
  });
  const makePrimary = useMutation({
    mutationFn: (id: number) => api.patch(`/api/domains/${id}`, { primary: true }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["domains"] }); toast.show("Primary domain updated", "ok"); },
    onError: (e) => toast.show(String(e), "fail"),
  });
  const repair = useMutation({
    mutationFn: () => api.post("/api/tunnel/resync-dns"),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dns-status"] });
      toast.show("DNS repaired", "ok");
    },
    onError: (e) => toast.show(String(e), "fail"),
  });

  const cfIntegration = (integrations ?? []).find(i => i.provider === "cloudflare");
  const cfLink = cfIntegration ? `/integrations/${cfIntegration.id}` : "/integrations";
  const tokenSet = !!tunnel?.cloudflare.token_set;
  const tunnelConnected = tunnel?.mode === "remote" && !!tunnel?.tunnel_id;
  const cfReady = tokenSet && tunnelConnected;

  return (
    <>
      <div className="row">
        <h1 style={{ margin: 0 }}>Domains</h1>
        <div className="spacer" />
        <button className="btn primary" onClick={() => setAddOpen(true)}><Plus size={14} /> Add</button>
      </div>
      <p className="lede" style={{ marginTop: "0.5rem" }}>
        Domains routed to this host through your Cloudflare Tunnel.
      </p>

      {tunnel && !tokenSet && (
        <div className="card" style={{ marginBottom: "1rem" }}>
          <div className="row">
            <span className="badge warn"><Cloud size={12} /> Cloudflare not connected</span>
            <span className="dim">Connect it under <Link to="/integrations">Integrations</Link> to add and route domains.</span>
          </div>
        </div>
      )}
      {tunnel && tokenSet && !tunnelConnected && (
        <div className="card" style={{ marginBottom: "1rem" }}>
          <div className="row">
            <span className="badge warn">No tunnel yet</span>
            <span className="dim">Create the tunnel on the <Link to={cfLink}>Cloudflare integration</Link> page.</span>
          </div>
        </div>
      )}
      {dns && !dns.in_sync && (
        <div className="card" style={{ marginBottom: "1rem" }}>
          <div className="card-row">
            <div className="grow">
              <div className="row">
                <span className="badge fail">DNS out of sync</span>
                <span className="dim">Some records point at the wrong tunnel — affected hosts return errors.</span>
              </div>
              <div className="dim" style={{ marginTop: "0.4rem" }}>
                {dns.issues.slice(0, 3).map(i => <div key={i}><code>{i}</code></div>)}
              </div>
            </div>
            <button className="btn primary" onClick={() => repair.mutate()} disabled={repair.isPending}>
              {repair.isPending ? <span className="spinner" /> : <Wrench size={14} />} Repair
            </button>
          </div>
        </div>
      )}

      {domains && domains.length > 0 ? (
        <table className="data-table">
          <thead>
            <tr><th>Domain</th><th>Status</th><th>Primary</th><th className="right" /></tr>
          </thead>
          <tbody>
            {domains.map(d => (
              <DomainRow key={d.id} d={d}
                onMakePrimary={() => makePrimary.mutate(d.id)}
                onRemove={() => { if (confirm(`Remove ${d.name}?`)) remove.mutate(d.id); }}
                makingPrimary={makePrimary.isPending}
                removing={remove.isPending} />
            ))}
          </tbody>
        </table>
      ) : domains ? (
        <div className="empty-state">
          <h3>No domains yet</h3>
          <p>Add a domain to start routing projects to this host.</p>
          <button className="btn primary" onClick={() => setAddOpen(true)}><Plus size={14} /> Add</button>
        </div>
      ) : <span className="spinner" />}

      <AddDomainModal open={addOpen} onClose={() => setAddOpen(false)} cfReady={cfReady} />
    </>
  );
}

function DomainRow({ d, onMakePrimary, onRemove, makingPrimary, removing }: {
  d: DomainItem; onMakePrimary: () => void; onRemove: () => void; makingPrimary: boolean; removing: boolean;
}) {
  const toast = useToast();
  const pending = d.zone_status === "pending";
  return (
    <>
      <tr>
        <td><strong>{d.name}</strong></td>
        <td>
          {pending
            ? <span className="badge warn">Pending nameservers</span>
            : d.cloudflare_routed
              ? <span className="badge ok">Routed</span>
              : <span className="badge muted plain">Manual DNS</span>}
        </td>
        <td>{d.is_primary && <span className="badge ok plain">Primary</span>}</td>
        <td className="actions">
          {!d.is_primary && (
            <>
              <button className="btn small ghost" aria-label={`Make ${d.name} primary`} title="Make primary"
                disabled={makingPrimary} onClick={onMakePrimary}>
                <Star size={12} />
              </button>{" "}
            </>
          )}
          <button className="btn small danger" aria-label={`Remove ${d.name}`} title="Remove"
            disabled={removing} onClick={onRemove}>
            <Trash2 size={12} />
          </button>
        </td>
      </tr>
      {pending && (d.name_servers?.length ?? 0) > 0 && (
        <tr>
          <td colSpan={4} style={{ background: "var(--tint)" }}>
            <div className="row" style={{ gap: "0.75rem", flexWrap: "wrap" }}>
              <span className="dim">Set these nameservers at your registrar — routing finishes automatically once they take effect:</span>
              {d.name_servers!.map(ns => (
                <button key={ns} className="btn small" title="Copy"
                  onClick={() => { navigator.clipboard.writeText(ns); toast.show(`Copied ${ns}`, "ok"); }}>
                  <Copy size={11} /> <code>{ns}</code>
                </button>
              ))}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

// ─── Unified add: type any domain; Cloudflare-managed by default ─────────────

function AddDomainModal({ open, onClose, cfReady }: { open: boolean; onClose: () => void; cfReady: boolean }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [name, setName] = useState("");
  const [primary, setPrimary] = useState(false);
  const [managed, setManaged] = useState(true);
  const [pendingNs, setPendingNs] = useState<string[] | null>(null);

  function reset() { setName(""); setPrimary(false); setManaged(true); setPendingNs(null); }
  function close() { reset(); onClose(); }

  const add = useMutation({
    mutationFn: async () => {
      if (managed && cfReady) {
        return api.post<{ pending: boolean; name_servers: string[] }>(
          "/api/domains/cloudflare", { name, primary });
      }
      await api.post("/api/domains", { name, primary });
      return { pending: false, name_servers: [] };
    },
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["domains"] });
      qc.invalidateQueries({ queryKey: ["tunnel"] });
      if (r.pending) {
        setPendingNs(r.name_servers ?? []);
        toast.show("Zone created — set the nameservers at your registrar", "ok");
      } else {
        toast.show("Domain added and routed", "ok");
        close();
      }
    },
    onError: (e) => toast.show(String(e), "fail"),
  });

  function submit(e: FormEvent) { e.preventDefault(); if (!add.isPending && name.trim()) add.mutate(); }

  return (
    <Modal
      open={open}
      onClose={close}
      title={pendingNs ? "Almost there" : "Add domain"}
      footer={pendingNs ? <>
        <span className="spacer" />
        <button className="btn primary" type="button" onClick={close}>Done</button>
      </> : <>
        <span className="spacer" />
        <button className="btn ghost" type="button" onClick={close}>Cancel</button>
        <button className="btn primary" type="submit" form="add-domain-form" disabled={add.isPending || !name.trim()}>
          {add.isPending ? <span className="spinner" /> : "Add"}
        </button>
      </>}
    >
      {pendingNs ? (
        <>
          <p style={{ marginTop: 0 }}>
            <strong>{name}</strong> was created in your Cloudflare account. Point it at these
            nameservers at your registrar:
          </p>
          <div className="card">
            {pendingNs.map(ns => <div key={ns}><code>{ns}</code></div>)}
          </div>
          <p className="dim">
            Nameserver changes take minutes to hours. Homebox checks every few minutes and
            finishes DNS + routing automatically — the domain shows <strong>Pending nameservers</strong> until then.
          </p>
        </>
      ) : (
        <form id="add-domain-form" onSubmit={submit}>
          <div className="field">
            <label className="lbl">Domain</label>
            <input value={name} onChange={e => setName(e.target.value)} placeholder="example.com" autoFocus required />
            <span className="hint">
              {managed && cfReady
                ? "Existing Cloudflare zones connect instantly; new domains are created in Cloudflare and you'll get nameservers to set."
                : "DNS is up to you — point the domain at your tunnel manually."}
            </span>
          </div>
          <label className="row" style={{ cursor: "pointer", gap: "0.5rem", marginBottom: "0.6rem" }}>
            <input type="checkbox" checked={primary} onChange={e => setPrimary(e.target.checked)} />
            Set as primary domain
          </label>
          {cfReady && (
            <label className="row" style={{ cursor: "pointer", gap: "0.5rem" }}>
              <input type="checkbox" checked={managed} onChange={e => setManaged(e.target.checked)} />
              Cloudflare-managed (recommended)
            </label>
          )}
        </form>
      )}
    </Modal>
  );
}
