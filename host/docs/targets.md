# ☁️ Deployment Targets — per-service, per-environment multi-cloud

Status: **BUILT, P0–P6** (2026-07-15). Manual E2E per `targets-e2e-plan.md`
still pending real AWS/GCP sandbox accounts.

> 2026-07-18: the `homebox` target is now location-aware — with a linked
> account it can name any account cluster (`config.cluster_id`) or standalone
> node (`config.node_id`); absent means "this homebox". Each cluster deploys
> only its own subset and excludes foreign-cluster hostnames from its
> tunnel/DNS. See `linked-accounts.md`.

Every service of every environment picks where it runs: `homebox` (default,
docker compose on the cluster) or a cloud provider. DNS stays on Cloudflare,
so routing a service elsewhere = pointing its per-service hostname at the
cloud endpoint instead of the tunnel.

## Target matrix

| Service kind | homebox | cloudflare | aws | gcp |
|---|---|---|---|---|
| static | compose+nginx | Pages (`pages`) | S3 website (`s3`) | GCS website (`gcs`) |
| web / api | compose | Containers (`cf_containers`, wrangler-driven) | App Runner (`app_runner`) | Cloud Run (`cloud_run`) |
| database | compose (pgEdge) | — | EC2 VM (`ec2_db`) | GCE VM (`gce_db`) |
| cache / worker | compose | — | — | — |

Variant resolution: `targets/__init__.variant_for(target, kind)`;
`config.variant` overrides. New provider = module in `app/targets/` + a
branch in `get_provider` + a `_VARIANTS` entry.

## Data model & sync

- `service_targets` (migration 0004): `service_id`, `environment_id`
  (NULL = service-wide default, same convention as ServiceEnvVar),
  `target`, `integration_id`, and SPLIT column groups:
  `config`+`updated_at` (user intent, stamped by routes) vs
  `state`+`state_updated_at` (machine state, written only by the cloud
  coordinator). `cluster_sync` applies newer-wins PER GROUP, so a config
  edit on node A and a state write on node B never clobber each other.
- `state` layout: orchestrator keys at top level (`status`, `endpoint`,
  `error`, `dns`, `mesh`, `db`, `previous`) + provider resource ids nested
  under `state.resource_ids`. Providers always see a FLATTENED view
  (`deploy._provider_state`) — they persist and read their own keys flat.
- Retarget flow: `PUT /api/services/{id}/target` stores
  `state.previous = {target, state}` (never overwriting a pending teardown),
  stamps `updated_at`, queues redeploys. The previous target's resources are
  destroyed only AFTER the new target is live: by `_deploy_cloud_targets`
  when the new target is cloud, by `_teardown_retargeted` when the new
  target is homebox (local stack up = live).

## Deploy engine (deploy.py)

Order of operations inside `_do_deploy`:

1. `effective_targets` + `is_cloud_coordinator` (lowest fresh serving
   non-mirror roster ordinal; mirrors never; single-node always).
2. **DB-VM pre-step** (`_provision_db_vms`, coordinator only) — BEFORE
   `_assemble_stack`, so consumers' env URLs can point at the VM mesh IP.
3. `_assemble_stack`: cloud-targeted services are excluded from the compose
   (plan entry `cloud: True`) — EXCEPT `ec2_db`/`gce_db`, which stay local
   (plan `db_vm: True`): the VM is an ADDITIVE Spock replica, homebox nodes
   keep active-active copies. Consumer auto-env URLs are rewritten by
   `targetslib.rewrite_cross_target_env` (matrix below).
4. compose up (or `compose down` when everything is cloud-targeted).
5. `_deploy_cloud_targets` (coordinator only): per service — build artifacts
   (static extraction / image build / wrangler passthrough), provider
   `deploy()`, per-host CNAME upsert + auxiliary DNS records, state write,
   previous-target teardown.
6. `_teardown_retargeted` (coordinator only): consume `state.previous` for
   services now back on homebox.
7. `verify_instances(cloud_probe=is_coord)` + Spock wiring
   (`ensure_replication(extra_nodes=db_vm_extra_nodes(...), wire_extra=is_coord)`).

Failures in cloud sections are recorded on the row (`status=error`) and never
fail the local deploy; `targetslib.reconcile_targets` (cluster loop, ~5 min)
redeploys envs whose targets are error/stale — and also live targets with a
pending Cloud Run domain mapping (`resource_ids.domain_mapping` in
`pending_verification`/`pending_mapping`).

### Cross-target env rewrite matrix

| consumer → producer | rewrite |
|---|---|
| homebox → homebox | unchanged (compose DNS) |
| homebox → DB VM | producer `state.mesh.ip` (10.77.x.y) |
| serverless → homebox DB/cache | `127.0.0.1:<proxy port>` (see below) |
| serverless → DB VM | VM public endpoint + `sslmode=require` |

## DNS

- Cloud deploys upsert a per-host CNAME (specific host beats the domain
  wildcard at Cloudflare); retargeting away deletes it so the wildcard
  re-covers via the tunnel.
- `targetslib.cloud_routed_hostnames()` is THE exclusion registry: the
  hourly DNS drift report/repair must never repoint a cloud-routed hostname
  (dedicated tests pin this).
- Providers can emit `state.extra_dns_records` (`[{type, name, value}]`,
  CNAME or TXT, written unproxied): App Runner cert validation, Cloud Run
  site-verification TXT. Written even while there is no CNAME yet.

## Serverless → homebox DB (phase 4)

A Cloud Run/App Runner consumer reaching a homebox-hosted database:

1. `targetslib.serverless_db_plan` derives, per (project, env): proxy rules
   per consumer, env overrides, and tunnel TCP ingress rules — via the ONE
   shared derivation `db_tunnel_rule` (hostname
   `db-<project>-<svc>-<env>.<domain>`, service `tcp://<stack>-<svc>-1:<port>`;
   5432 database / 6379 cache; local ports 15432+).
2. Cloudflare side (coordinator, during cloud deploy): shared Access service
   token `homebox-db-access` (secret encrypted into the integration state),
   one Access app per DB hostname with a non-identity service-token policy,
   tunnel ingress re-push. `_push_ingress`/`_push_remote_ingress` re-derive
   rules from persisted state via `all_tunnel_tcp_rules` — hourly pushes
   keep the tcp rules.
3. Image wrapping (`targets/artifacts.wrap_with_access_proxy`): FROM the
   built image, ADD a version+sha256-pinned cloudflared, entrypoint spawns
   one `cloudflared access tcp` per rule then execs the original
   entrypoint/cmd. Token env (`TUNNEL_SERVICE_TOKEN_ID/SECRET`) is injected
   by the orchestrator.
   ⚠️ `artifacts.CLOUDFLARED_SHA256` ships as a fail-closed placeholder —
   pin the real checksum for `CLOUDFLARED_VERSION` before first real use.

## Database VMs (phase 5) — clustered installs only (v1)

`_provision_db_vms` (coordinator): allocates a mesh ordinal from the
reserved range ≥ 0xF000 (`allocate_mesh_ordinal`), mints a WireGuard
keypair (private key encrypted into `state.mesh` BEFORE provisioning, so
re-runs reuse the identity), builds `wg_peers` from the roster, derives DB
creds from the compose env + `derive_repl_password(cluster secret)`, and
calls the provider with the `ctx.config` contract documented in
`targets/db_vm_common.py`. EC2 requires `config.ami` (region-specific);
sizes default t3.small / e2-small.

The VM cloud-init mirrors `cluster_db._INIT_SCRIPTS` for a standalone
pgEdge container (spock/snowflake/lolor, `lolor.node` in postgresql.conf).
Mesh: nodes dial the VM (`meshlib.ensure_mesh` pulls
`targetslib.mesh_extra_peers`); the VM has no Endpoint lines (NAT'd nodes).
Replication: `cluster_db.ensure_replication(extra_nodes=…, wire_extra=coordinator)`
wires both directions — the VM side over `_psql_remote` (psql from the local
container to the VM's mesh IP; no SSH/agent on the VM).

Retarget back to homebox: `_teardown_retargeted` terminates the VM
(SG/firewall best-effort); local replicas keep all data (they were
active-active copies). Spock subscriptions pointing at the gone VM are
cleaned up by the reconcile loop's sub GC / manually if needed.

## Cloud Run custom domains (phase 6)

Site Verification (INET_DOMAIN, DNS_TXT — token emitted as an
`extra_dns_records` TXT the orchestrator writes) → v1 Knative domain
mapping (`certificateMode: AUTOMATIC`) → CNAME `ghs.googlehosted.com`,
**unproxied** (orange cloud breaks Google's cert issuance). While pending,
the instance URL falls back to run.app and reconcile retries. Requires
`siteverification.googleapis.com` enabled on the GCP project.

## Ops notes

- Admin image ships node/npx (Dockerfile, NodeSource 22) — required by the
  `cf_containers` target (`npx wrangler@4`); rebuild nodes to pick it up.
- Cloudflare token scopes: Pages: Edit, Workers Scripts: Edit, Access: Apps
  and Policies: Edit, Access: Service Tokens: Edit (see e2e plan).
- Onboarding's pre-filled "create token" link (Onboarding/Integrations/
  IntegrationDetail.tsx, byte-identical) now requests Pages: Edit + Workers
  Scripts: Edit as OPTIONAL groups alongside the tunnel/DNS/zone scopes.
  `_validate_and_store_token` probes them non-blocking (cf.list_pages_projects /
  cf.list_workers_scripts) and records `pages_ok` / `workers_ok`; the token-
  connect response + onboarding /state surface both so the UI can hint a re-scope
  without blocking. CAVEAT: Cloudflare Containers also push an OCI image to the
  account's managed Workers registry during `wrangler deploy`, which Workers
  Scripts: Edit may not fully cover — `workers_ok` means "can manage Worker
  scripts", not a guaranteed image push. (Access: Apps / Service Tokens are the
  AWS/GCP serverless→Homebox-DB path, deliberately NOT in the pre-fill link.)
- Deterministic names everywhere: `homebox-<project>-<env>-<service>`
  (idempotent create-or-update = coordinator-handover safe).
- Tests: `tests/test_targetslib`, `test_target_deploy_engine`,
  `test_target_orchestration` (orchestrator wiring), `test_serverless_db_path`,
  `test_db_vm_targets`, `test_static_bucket_targets`, per-provider suites.
  All run on sqlite + httpx.MockTransport — no cloud accounts.
