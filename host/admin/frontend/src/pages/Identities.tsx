import { FormEvent, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { UserPlus, Trash2, Github } from "lucide-react";
import { api } from "../lib/api";
import { Modal } from "../components/Modal";
import { useToast } from "../lib/toast";
import type { Identity } from "../lib/types";

export function Identities() {
  const qc = useQueryClient();
  const toast = useToast();
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
      <div className="row" style={{ marginTop: "0.5rem" }}>
        <h2 style={{ margin: 0 }}>Identities</h2>
      </div>
      <p className="dim" style={{ marginTop: "0.4rem", marginBottom: "1rem" }}>
        Whitelisted emails that can sign into Homebox passwordlessly via Google or GitHub.
        Anyone signing in with an email not listed here (or disabled) is denied.
      </p>

      <form className="row" onSubmit={submit} style={{ marginBottom: "1rem" }}>
        <input
          type="email"
          value={email}
          onChange={e => setEmail(e.target.value)}
          placeholder="name@example.com"
          style={{ flex: 1, minWidth: "220px" }}
          required
        />
        <button className="btn primary" type="submit" disabled={add.isPending}>
          {add.isPending ? <span className="spinner" /> : <><UserPlus size={14} /> Add identity</>}
        </button>
      </form>

      {identities && identities.length > 0 ? (
        <table className="data-table">
          <thead>
            <tr>
              <th>Email</th><th>Status</th><th>Last login</th><th>Logins</th><th className="right">Actions</th>
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
                  <button className="btn small danger" disabled={remove.isPending} onClick={() => setDeleteTarget(i)}>
                    <Trash2 size={12} /> Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : identities ? (
        <div className="empty-state">
          <h3>No identities yet</h3>
          <p>Add an email above to let it sign in with Google or GitHub — no password required.</p>
        </div>
      ) : <span className="spinner" />}

      <Modal
        open={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        title={`Remove ${deleteTarget?.email ?? ""}?`}
        footer={<>
          <span className="spacer" />
          <button className="btn" type="button" onClick={() => setDeleteTarget(null)}>Cancel</button>
          <button className="btn danger" type="button" disabled={remove.isPending}
            onClick={() => deleteTarget && remove.mutate(deleteTarget)}>
            {remove.isPending ? <span className="spinner" /> : "Remove"}
          </button>
        </>}
      >
        <p style={{ margin: 0 }}>
          <strong>{deleteTarget?.email}</strong> will no longer be able to sign in via OAuth.
          You can add it again later.
        </p>
      </Modal>
    </>
  );
}
