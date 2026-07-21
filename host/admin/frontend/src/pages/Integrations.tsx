import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Github, Plus, ExternalLink, Cloud, CloudCog, Server, ChevronRight } from "lucide-react";
import { api } from "../lib/api";
import { Modal } from "../components/Modal";
import PageHelp from "../components/PageHelp";
import { useToast } from "../lib/toast";
import type { CloudflareAccount, IntegrationItem, OAuthSettings, SetTokenResponse } from "../lib/types";

/**
 * Integrations — every connection to an external system (GitHub orgs,
 * Cloudflare, AWS, GCP), as a card list. Each card links to /integrations/:id
 * for details and actions. New connections go through the Add wizard: pick a
 * provider, connect via OAuth (or paste a token / keys), save.
 */

export function providerLogo(provider: string) {
  if (provider === "github") return <Github aria-hidden />;
  if (provider === "aws") return <Server aria-hidden />;
  if (provider === "gcp") return <CloudCog aria-hidden />;
  return <Cloud aria-hidden />;
}

export function statusDot(status: string) {
  const ok = ["connected", "active", "ok"].includes(status);
  const fail = ["failed", "error", "invalid"].includes(status);
  return <span className={`status-dot ${ok ? "ok" : fail ? "fail" : ""}`} title={status} />;
}

export function Integrations() {
  const [addOpen, setAddOpen] = useState(false);

  const { data: integrations } = useQuery<IntegrationItem[]>({
    queryKey: ["integrations"],
    queryFn: () => api.get<IntegrationItem[]>("/api/integrations"),
  });

  return (
    <>
      <div className="row">
        <h1 style={{ margin: 0 }}>Integrations</h1>
        <PageHelp title="About integrations">
          <p>
            Integrations are this host's connections to external systems. GitHub supplies
            source: connecting an account or organization lets you add its repositories as
            projects, with push webhooks triggering deploys. Cloudflare provides the tunnel
            and DNS that route domains here. AWS and Google Cloud act as deploy targets, so
            individual services can run on S3, App Runner, or EC2, and GCS, Cloud Run, or
            GCE instead of this box.
          </p>
          <p>
            Credentials — GitHub tokens, the Cloudflare API token, AWS keys, GCP
            service-account JSON — are verified against the provider when you save them and
            stored encrypted at rest. GitHub can also connect via OAuth instead of a token.
          </p>
          <p>
            Each card opens a detail page with the connection's status and actions. When
            this host is linked to a Homebox account, integrations sync to your other nodes
            through the encrypted account vault, with secrets re-encrypted under each
            cluster's own key.
          </p>
        </PageHelp>
        <div className="spacer" />
        <button className="btn primary" onClick={() => setAddOpen(true)}>
          <Plus size={14} /> Add
        </button>
      </div>

      {!integrations ? (
        <span className="spinner" />
      ) : integrations.length === 0 ? (
        <div className="empty-state" style={{ marginTop: "1rem" }}>
          <h3>Nothing connected yet</h3>
          <p>Add a GitHub organization to start deploying its repositories.</p>
          <button className="btn primary" onClick={() => setAddOpen(true)}><Plus size={14} /> Add integration</button>
        </div>
      ) : (
        <div className="provider-list">
          {integrations.map(i => <ProviderCard key={i.id} i={i} />)}
        </div>
      )}

      <AddIntegrationModal
        open={addOpen}
        onClose={() => setAddOpen(false)}
        hasCloudflare={(integrations ?? []).some(i => i.provider === "cloudflare")}
      />
    </>
  );
}

function ProviderCard({ i }: { i: IntegrationItem }) {
  const title = i.provider === "github"
    ? (i.account_login ?? "GitHub")
    : (i.name || (i.provider === "aws" ? "AWS" : i.provider === "gcp" ? "Google Cloud" : "Cloudflare"));
  const sub = i.provider === "github"
    ? `GitHub · ${i.source === "oauth" ? "OAuth" : "token"} · ${i.project_count} project${i.project_count === 1 ? "" : "s"}`
    : i.provider === "aws"
    ? `AWS · account ${i.account_login ?? "?"}`
    : i.provider === "gcp"
    ? `Google Cloud · ${i.account_login ?? "project"}`
    : `Cloudflare · ${i.account_id ? i.account_id.slice(0, 8) + "…" : "account"}`;

  return (
    <Link className="provider-card" to={`/integrations/${i.id}`}>
      <span className="provider-logo">{providerLogo(i.provider)}</span>
      <span style={{ minWidth: 0 }}>
        <div className="provider-title">{title}</div>
        <div className="provider-sub">{sub}</div>
      </span>
      <span className="spacer" />
      {statusDot(i.status)}
      <ChevronRight size={18} className="chev" aria-hidden />
    </Link>
  );
}

// ─── Add wizard: pick provider → connect (OAuth first, token fallback) ────────

type AddStep = "pick" | "github" | "cloudflare" | "aws" | "gcp";

function AddIntegrationModal({ open, onClose, hasCloudflare }: {
  open: boolean; onClose: () => void; hasCloudflare: boolean;
}) {
  const [step, setStep] = useState<AddStep>("pick");

  function close() {
    setStep("pick");
    onClose();
  }

  const title = step === "pick" ? "Add integration"
    : step === "github" ? "Connect GitHub"
    : step === "aws" ? "Connect AWS"
    : step === "gcp" ? "Connect Google Cloud"
    : "Connect Cloudflare";

  return (
    <Modal open={open} onClose={close} title={title} footer={<>
      {step !== "pick" && (
        <button className="btn ghost" type="button" onClick={() => setStep("pick")}>← Back</button>
      )}
      <span className="spacer" />
      <button className="btn ghost" type="button" onClick={close}>Cancel</button>
      {step === "github" && (
        <button className="btn primary" type="submit" form="add-github-form">Save</button>
      )}
      {step === "cloudflare" && (
        <button className="btn primary" type="submit" form="add-cloudflare-form">Save</button>
      )}
      {step === "aws" && (
        <button className="btn primary" type="submit" form="add-aws-form">Save</button>
      )}
      {step === "gcp" && (
        <button className="btn primary" type="submit" form="add-gcp-form">Save</button>
      )}
    </>}>
      {step === "pick" && (
        <>
          <p className="dim" style={{ marginTop: 0 }}>Pick a provider to connect.</p>
          <div className="provider-grid">
            <button className="provider-tile" onClick={() => setStep("github")}>
              <span className="provider-logo"><Github aria-hidden /></span>
              GitHub
              <small>deploy an organization's repos</small>
            </button>
            <button className="provider-tile" onClick={() => setStep("cloudflare")} disabled={hasCloudflare}>
              <span className="provider-logo"><Cloud aria-hidden /></span>
              Cloudflare
              <small>{hasCloudflare ? "already connected" : "tunnel, DNS & domains"}</small>
            </button>
            <button className="provider-tile" onClick={() => setStep("aws")}>
              <span className="provider-logo"><Server aria-hidden /></span>
              AWS
              <small>Deploy services to S3, App Runner, EC2</small>
            </button>
            <button className="provider-tile" onClick={() => setStep("gcp")}>
              <span className="provider-logo"><CloudCog aria-hidden /></span>
              Google Cloud
              <small>Deploy services to GCS, Cloud Run, GCE</small>
            </button>
          </div>
        </>
      )}
      {step === "github" && <GithubConnect onDone={close} />}
      {step === "cloudflare" && <CloudflareConnect onDone={close} />}
      {step === "aws" && <AwsConnect onDone={close} />}
      {step === "gcp" && <GcpConnect onDone={close} />}
    </Modal>
  );
}

function GithubConnect({ onDone }: { onDone: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [login, setLogin] = useState("");
  const [pat, setPat] = useState("");

  const { data: oauth } = useQuery<OAuthSettings>({
    queryKey: ["oauth-settings"],
    queryFn: () => api.get<OAuthSettings>("/api/oauth/settings"),
  });

  const connect = useMutation({
    mutationFn: () => api.post(`/api/integrations/github/connect-pat`, { login, pat }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["integrations"] });
      qc.invalidateQueries({ queryKey: ["projects"] });
      toast.show("GitHub connected", "ok");
      onDone();
    },
    onError: (e) => toast.show(String(e), "fail"),
  });

  function submit(e: FormEvent) { e.preventDefault(); if (!connect.isPending) connect.mutate(); }

  const tokenUrl = "https://github.com/settings/tokens/new?scopes=repo,admin:org,admin:repo_hook,workflow&description=Homebox%20Admin";

  return (
    <>
      {oauth?.configured && (
        <>
          <button className="btn primary" type="button" style={{ width: "100%", justifyContent: "center" }}
            onClick={() => { window.location.href = "/api/oauth/github/start"; }}>
            <Github size={14} /> Connect with GitHub
          </button>
          <p className="dim" style={{ margin: "0.5rem 0 0", fontSize: "0.78rem" }}>
            Connects your GitHub account — your own repositories plus any
            organizations you grant on the consent screen.
          </p>
          <div className="login-divider"><span>or use an org token</span></div>
        </>
      )}
      <form id="add-github-form" onSubmit={submit}>
        <div className="field">
          <label className="lbl">Organization</label>
          <input value={login} onChange={e => setLogin(e.target.value)} placeholder="my-org" required />
          <span className="hint">The slug from github.com/<strong>my-org</strong>.</span>
        </div>
        <div className="field">
          <label className="lbl">Personal access token</label>
          <input type="password" value={pat} onChange={e => setPat(e.target.value)} placeholder="ghp_… or github_pat_…" required />
          <span className="hint">
            {connect.isPending
              ? <span className="row"><span className="spinner" /> Verifying with GitHub…</span>
              : <>Needs <code>repo</code>, <code>admin:org</code>, and <code>admin:repo_hook</code> scopes.{" "}
                <a href={tokenUrl} target="_blank" rel="noopener">Generate one <ExternalLink size={11} /></a></>}
          </span>
        </div>
      </form>
    </>
  );
}

function CloudflareConnect({ onDone }: { onDone: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [token, setToken] = useState("");
  const [accounts, setAccounts] = useState<CloudflareAccount[]>([]);
  const [accountId, setAccountId] = useState("");

  function finish(msg: string) {
    qc.invalidateQueries({ queryKey: ["integrations"] });
    qc.invalidateQueries({ queryKey: ["tunnel"] });
    toast.show(msg, "ok");
    onDone();
  }

  const submit = useMutation({
    mutationFn: (body: { token: string; account_id?: string }) =>
      api.post<SetTokenResponse>("/api/tunnel/token", body),
    onSuccess: (resp) => {
      if (resp.account_id) finish("Cloudflare connected");
      else { setAccounts(resp.accounts); toast.show("Pick which Cloudflare account to use", "ok"); }
    },
    onError: (e) => toast.show(String(e), "fail"),
  });

  function save(e: FormEvent) {
    e.preventDefault();
    if (submit.isPending) return;
    if (accounts.length > 0) submit.mutate({ token, account_id: accountId || undefined });
    else if (token.trim()) submit.mutate({ token: token.trim() });
  }

  const tokenUrl =
    "https://dash.cloudflare.com/profile/api-tokens?permissionGroupKeys=%5B%7B%22key%22%3A%22argo_tunnel%22%2C%22type%22%3A%22edit%22%7D%2C%7B%22key%22%3A%22account_settings%22%2C%22type%22%3A%22read%22%7D%2C%7B%22key%22%3A%22dns%22%2C%22type%22%3A%22edit%22%7D%2C%7B%22key%22%3A%22zone%22%2C%22type%22%3A%22edit%22%7D%5D&name=Homebox+Admin&accountId=*&zoneId=all";

  return (
    <form id="add-cloudflare-form" onSubmit={save}>
      {accounts.length > 0 ? (
        <div className="field">
          <label className="lbl">Account</label>
          <select value={accountId} onChange={e => setAccountId(e.target.value)} required>
            <option value="" disabled>Pick an account…</option>
            {accounts.map(a => <option key={a.id} value={a.id}>{a.name} ({a.id.slice(0, 8)}…)</option>)}
          </select>
          <span className="hint">Tunnels and DNS will be created under this account.</span>
        </div>
      ) : (
        <div className="field">
          <label className="lbl">Cloudflare API token</label>
          <input type="password" value={token} onChange={e => setToken(e.target.value)}
            placeholder="Paste your scoped token" disabled={submit.isPending} autoFocus />
          <span className="hint">
            {submit.isPending
              ? <span className="row"><span className="spinner" /> Verifying scopes with Cloudflare…</span>
              : <>Scopes: <code>Cloudflare Tunnel:Edit</code>, <code>DNS:Edit</code>, <code>Zone:Edit</code>, <code>Account Settings:Read</code> — Zone Resources: <strong>All zones</strong>.{" "}
                <a href={tokenUrl} target="_blank" rel="noopener">Generate one <ExternalLink size={11} /></a></>}
          </span>
        </div>
      )}
    </form>
  );
}

function AwsConnect({ onDone }: { onDone: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [keyId, setKeyId] = useState("");
  const [secret, setSecret] = useState("");
  const [region, setRegion] = useState("us-east-1");

  const connect = useMutation({
    mutationFn: () => api.post("/api/integrations/aws/connect", {
      access_key_id: keyId.trim(), secret_access_key: secret.trim(), region: region.trim() || "us-east-1",
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["integrations"] });
      toast.show("AWS connected", "ok");
      onDone();
    },
    onError: (e) => toast.show(String(e), "fail"),
  });

  function submit(e: FormEvent) { e.preventDefault(); if (!connect.isPending) connect.mutate(); }

  return (
    <form id="add-aws-form" onSubmit={submit}>
      <div className="field">
        <label className="lbl">Access key ID</label>
        <input value={keyId} onChange={e => setKeyId(e.target.value)} placeholder="AKIA…" required autoFocus />
      </div>
      <div className="field">
        <label className="lbl">Secret access key</label>
        <input type="password" value={secret} onChange={e => setSecret(e.target.value)}
          placeholder="Paste the secret key" required />
        <span className="hint">
          {connect.isPending
            ? <span className="row"><span className="spinner" /> Verifying with AWS…</span>
            : <>Keys for an IAM user with deploy permissions — verified via STS, then encrypted at rest.</>}
        </span>
      </div>
      <div className="field">
        <label className="lbl">Region</label>
        <input value={region} onChange={e => setRegion(e.target.value)} placeholder="us-east-1" />
        <span className="hint">Default region for resources Homebox creates.</span>
      </div>
    </form>
  );
}

function GcpConnect({ onDone }: { onDone: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [saJson, setSaJson] = useState("");
  const [region, setRegion] = useState("us-central1");

  const connect = useMutation({
    mutationFn: () => api.post("/api/integrations/gcp/connect", {
      service_account_json: saJson.trim(), region: region.trim() || "us-central1",
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["integrations"] });
      toast.show("Google Cloud connected", "ok");
      onDone();
    },
    onError: (e) => toast.show(String(e), "fail"),
  });

  function submit(e: FormEvent) { e.preventDefault(); if (!connect.isPending) connect.mutate(); }

  return (
    <form id="add-gcp-form" onSubmit={submit}>
      <div className="field">
        <label className="lbl">Service-account key (JSON)</label>
        <textarea value={saJson} onChange={e => setSaJson(e.target.value)} rows={7}
          placeholder='{"type": "service_account", "project_id": "…", …}' required autoFocus
          style={{ fontFamily: "monospace", fontSize: "0.78rem" }} />
        <span className="hint">
          {connect.isPending
            ? <span className="row"><span className="spinner" /> Verifying with Google Cloud…</span>
            : <>Create a service account with Editor role and paste its JSON key.</>}
        </span>
      </div>
      <div className="field">
        <label className="lbl">Region</label>
        <input value={region} onChange={e => setRegion(e.target.value)} placeholder="us-central1" />
        <span className="hint">Default region for resources Homebox creates.</span>
      </div>
    </form>
  );
}
