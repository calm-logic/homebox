import { FormEvent, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2, Github } from "lucide-react";
import { api } from "../lib/api";
import { Modal } from "../components/Modal";
import PageHelp from "../components/PageHelp";
import { useToast } from "../lib/toast";
import type { Identity } from "../lib/types";

export function Identities() {
  const qc = useQueryClient();
  const toast = useToast();
  const [addOpen, setAddOpen] = useState(false);
  const [email, setEmail] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<Identity | null>(null);

  const { data: identities } = useQuery<Identity[]>({
    queryKey: ["identities"],
    queryFn: () => api.get<Identity[]>("/api/identities"),
  });

  const add = useMutation({
    mutationFn: () => api.post("/api/identities", { email: email.trim() }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["identities"] });
      toast.show("Identity added", "ok");
      setEmail("");
      setAddOpen(false);
    },
    onError: (e) => toast.show(String(e), "fail"),
  });

  const toggle = useMutation({
    mutationFn: (i: Identity) => api.post(`/api/identities/${i.id}/enabled`, { enabled: !i.enabled }),
    onSuccess: (_d, i) => {
      qc.invalidateQueries({ queryKey: ["identities"] });
      toast.show(i.enabled ? "Identity disabled" : "Identity enabled", "ok");
    },
    onError: (e) => toast.show(String(e), "fail"),
  });

  const remove = useMutation({
    mutationFn: (i: Identity) => api.del(`/api/identities/${i.id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["identities"] });
      toast.show("Identity removed", "ok");
      setDeleteTarget(null);
    },
    onError: (e) => toast.show(String(e), "fail"),
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    if (email.trim()) add.mutate();
  }

  return (
    <>
      <div className="row">
        <h1 style={{ margin: 0 }}>Identities</h1>
        <PageHelp title="About identities">
          <p>
            Identities are the whitelist for signing in to this admin. An email listed and
            enabled here can log in with Google or GitHub, no password involved. Anyone
            else is denied, so the list is the entire access-control story.
          </p>
          <p>
            The email must match the verified email of the Google or GitHub account used at
            login. Disabling an identity blocks sign-in without deleting its login history;
            removing it deletes the row, and you can re-add it later.
          </p>
          <p>
            Identities don't only come from the Add button: linking this host to a Homebox
            account automatically adds (or re-enables) the account's verified email so you
            can't lock yourself out, and when linked, identities sync to your other nodes
            through the encrypted account vault.
          </p>
        </PageHelp>
        <div className="spacer" />
        <button className="btn primary" onClick={() => setAddOpen(true)}><Plus size={14} /> Add</button>
      </div>

      {identities && identities.length > 0 ? (
        <table className="data-table">
          <thead>
            <tr>
              <th>Email</th><th>Status</th><th>Last login</th><th>Logins</th><th className="right" />
            </tr>
          </thead>
          <tbody>
            {identities.map(i => (
              <tr key={i.id}>
                <td><strong>{i.email}</strong></td>
                <td>
                  {i.enabled
                    ? <span className="badge ok">Enabled</span>
                    : <span className="badge warn">Disabled</span>}
                </td>
                <td>
                  {i.last_login_at ? (
                    <>
                      <span>{new Date(i.last_login_at).toLocaleString()}</span>{" "}
                      {i.last_login_provider === "github"
                        ? <span className="badge plain"><Github size={11} /> GitHub</span>
                        : <span className="badge info plain">Google</span>}
                    </>
                  ) : <span className="dim">Never</span>}
                </td>
                <td>{i.login_count}</td>
                <td className="actions">
                  <button className="btn small" disabled={toggle.isPending} onClick={() => toggle.mutate(i)}>
                    {i.enabled ? "Disable" : "Enable"}
                  </button>{" "}
                  <button className="btn small danger" aria-label={`Remove ${i.email}`} title="Remove"
                    disabled={remove.isPending} onClick={() => setDeleteTarget(i)}>
                    <Trash2 size={12} />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : identities ? (
        <div className="empty-state">
          <h3>No identities yet</h3>
          <p>Add an email to let it sign in with Google or GitHub, no password required.</p>
          <button className="btn primary" onClick={() => setAddOpen(true)}><Plus size={14} /> Add</button>
        </div>
      ) : <span className="spinner" />}

      <Modal
        open={addOpen}
        onClose={() => { setAddOpen(false); setEmail(""); }}
        title="Add identity"
        footer={<>
          <span className="spacer" />
          <button className="btn ghost" type="button" onClick={() => { setAddOpen(false); setEmail(""); }}>Cancel</button>
          <button className="btn primary" type="submit" form="add-identity-form" disabled={add.isPending || !email.trim()}>
            {add.isPending ? <span className="spinner" /> : "Add"}
          </button>
        </>}
      >
        <form id="add-identity-form" onSubmit={submit}>
          <div className="field">
            <label className="lbl">Email</label>
            <input
              type="email"
              value={email}
              onChange={e => setEmail(e.target.value)}
              placeholder="name@example.com"
              autoFocus
              required
            />
            <span className="hint">The verified email of the Google or GitHub account that will sign in.</span>
          </div>
        </form>
      </Modal>

      <Modal
        open={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        title={`Remove ${deleteTarget?.email ?? ""}?`}
        footer={<>
          <span className="spacer" />
          <button className="btn ghost" type="button" onClick={() => setDeleteTarget(null)}>Cancel</button>
          <button className="btn danger" type="button" disabled={remove.isPending}
            onClick={() => deleteTarget && remove.mutate(deleteTarget)}>
            {remove.isPending ? <span className="spinner" /> : "Remove"}
          </button>
        </>}
      >
        <p style={{ margin: 0 }}>
          <strong>{deleteTarget?.email}</strong> can no longer sign in. You can re-add it later.
        </p>
      </Modal>
    </>
  );
}
