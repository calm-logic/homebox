import { FormEvent, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2, Cloud, Wrench, Copy, Star, MoreVertical } from "lucide-react";
import { api } from "../lib/api";
import { Modal } from "../components/Modal";
import PageHelp from "../components/PageHelp";
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
        <PageHelp title="About domains">
          <p>
            Domains here are routed to this host through your Cloudflare Tunnel. The one
            marked <strong>Primary</strong> is where project environments get their default
            subdomains.
          </p>
          <p>
            Adding a Cloudflare-managed domain creates (or connects) the zone in your
            Cloudflare account and points the apex and a <code>*.domain</code> wildcard at
            the tunnel, so any subdomain reaches this host without per-app DNS records.
            Brand-new domains sit at <strong>Pending nameservers</strong> until the
            nameservers you set at your registrar take effect; Homebox checks in the
            background and finishes routing automatically. You can also add a domain and
            manage DNS yourself.
          </p>
          <p>
            A monitor re-checks DNS hourly. If records drift — for example a record ends up
            pointing at a different tunnel — a banner appears here with a one-click Repair
            that rewrites the records to this host's tunnel.
          </p>
          <p>
            When this host is linked to a Homebox account, domains sync to your other nodes
            through the encrypted account vault.
          </p>
        </PageHelp>
        <div className="spacer" />
        <button className="btn primary" onClick={() => setAddOpen(true)}><Plus size={14} /> Add</button>
      </div>

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

/** Kebab dropdown for a domain row. Portaled to document.body (fixed,
 *  measured placement, flips upward near the viewport bottom) so the table's
 *  overflow never clips it — same pattern as Topology's DropMenu. */
function DomainRowMenu({ d, onMakePrimary, onRemove, makingPrimary, removing }: {
  d: DomainItem; onMakePrimary: () => void; onRemove: () => void; makingPrimary: boolean; removing: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    if (!open) { setPos(null); return; }
    const place = () => {
      const anchor = wrapRef.current?.getBoundingClientRect();
      const menu = menuRef.current;
      if (!anchor || !menu) return;
      const gap = 4;
      const mh = menu.offsetHeight;
      const mw = menu.offsetWidth;
      const fitsBelow = anchor.bottom + gap + mh <= window.innerHeight - gap;
      const top = fitsBelow ? anchor.bottom + gap : Math.max(gap, anchor.top - gap - mh);
      const left = Math.max(gap, Math.min(anchor.right - mw, window.innerWidth - mw - gap));
      setPos({ top, left });
    };
    place();
    window.addEventListener("scroll", place, true);
    window.addEventListener("resize", place);
    return () => {
      window.removeEventListener("scroll", place, true);
      window.removeEventListener("resize", place);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      const t = e.target as Node;
      if (!wrapRef.current?.contains(t) && !menuRef.current?.contains(t)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

  return (
    <div className="menu-wrap" ref={wrapRef} onClick={e => e.stopPropagation()}>
      <button className="icon-btn" type="button" aria-label={`Actions for ${d.name}`}
        aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen(o => !o)}>
        <MoreVertical size={15} />
      </button>
      {open && createPortal(
        <div className="menu menu-portal" role="menu" ref={menuRef}
          style={pos ? { top: pos.top, left: pos.left } : { top: -9999, left: -9999, visibility: "hidden" }}
          onClick={e => e.stopPropagation()}>
          {!d.is_primary && (
            <button className="menu-item" role="menuitem" disabled={makingPrimary}
              onClick={() => { setOpen(false); onMakePrimary(); }}>
              <Star size={13} /> Set as primary
            </button>
          )}
          <button className="menu-item danger" role="menuitem" disabled={removing}
            onClick={() => { setOpen(false); onRemove(); }}>
            <Trash2 size={13} /> Remove
          </button>
        </div>,
        document.body,
      )}
    </div>
  );
}

function DomainRow({ d, onMakePrimary, onRemove, makingPrimary, removing }: {
  d: DomainItem; onMakePrimary: () => void; onRemove: () => void; makingPrimary: boolean; removing: boolean;
}) {
  const toast = useToast();
  const nav = useNavigate();
  const pending = d.zone_status === "pending";
  return (
    <>
      <tr className="clickable" onClick={() => nav(`/domains/${d.id}`)}>
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
          <DomainRowMenu d={d} onMakePrimary={onMakePrimary} onRemove={onRemove}
            makingPrimary={makingPrimary} removing={removing} />
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
