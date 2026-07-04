// Shared types between API responses and UI.

export interface Me {
  username: string;
}

export interface MetricsSummary {
  integration_count: number;
  project_count: number;
  managed_count: number;
  domain_count: number;
  runner: {
    installed: boolean;
    container_count: number;
  };
}

export interface DomainItem {
  id: number;
  name: string;
  mode: "wildcard" | "dedicated";
  is_primary: boolean;
  cloudflare_routed: boolean;
  zone_status: "active" | "pending";
  name_servers: string[] | null;
}

export type DeploymentStatus =
  | "pending_checks" | "pending_promotion" | "pending_e2e"
  | "queued" | "cloning" | "dissecting" | "building" | "starting"
  | "running" | "failed" | "stopped" | "superseded" | "blocked";

export interface DeploymentInfo {
  id: number;
  status: DeploymentStatus;
  commit_sha: string | null;
  error: string | null;
  trigger: "manual" | "webhook";
  updated_at: string | null;
}

export interface DeploymentItem extends DeploymentInfo {
  created_at: string | null;
}

export interface DeploymentDetailData extends DeploymentItem {
  log_tail: string | null;
  stack_name: string;
  environment: { id: number; name: string };
}

// ── Integrations / Projects / Services / Environments ──────────────────────────

export interface IntegrationItem {
  id: number;
  provider: string;             // github | gitlab | cloudflare
  account_login: string | null;
  account_id: string | null;
  name: string | null;
  status: string;
  source: string;               // oauth | pat | token
  project_count: number;
  created_at: string | null;
  last_verified_at: string | null;
}

export interface IntegrationRef {
  id: number;
  provider: string;
  account_login: string | null;
}

export interface ServiceInstanceInfo {
  service_name: string;
  url: string | null;
  status: string;
  container_name: string | null;
}

export interface EnvironmentInfo {
  id: number;
  name: string;
  kind: "production" | "dev" | "preview";
  branch: string | null;
  slug_suffix: string;
  is_default: boolean;
  domain_id: number | null;
  promotion_gate: boolean;
  e2e_workflow: string | null;
  promote_from_env_id: number | null;
  deployment: DeploymentInfo | null;
  instances: ServiceInstanceInfo[];
}

export interface DetectedStackEntry {
  name: string;
  kind: string;
  public: boolean;
  label: string;
}

export interface ProjectItem {
  id: number;
  repo_full_name: string;
  name: string;
  default_branch: string;
  managed: boolean;
  auto_deploy: boolean;
  require_checks: boolean;
  domain_id: number | null;
  domain: string | null;
  domain_mode: "wildcard" | "dedicated" | null;
  description: string | null;
  dissected_at: string | null;
  detected_stack: { services?: DetectedStackEntry[] };
  integration: IntegrationRef | null;
  environments: EnvironmentInfo[];
}

export interface EnvVarItem {
  id: number;
  key: string;
  value: string;
  source: "auto" | "user";
  is_secret: boolean;
  environment_id: number | null;
}

export interface ServiceItem {
  id: number;
  name: string;
  kind: string;
  source_type: string;
  source_ref: string | null;
  is_public: boolean;
  subdomain_label: string;
  internal_port: number | null;
  depends_on: string[];
  env_template: Record<string, string>;
  env_vars: EnvVarItem[];
}

export interface ProjectDetailData extends ProjectItem {
  services: ServiceItem[];
}

export interface ProjectWorkflowRun {
  id: number;
  run_id: number;
  name: string;
  status: string;
  conclusion: string | null;
  head_branch: string;
  html_url: string;
  created_at: string | null;
}

export interface MetricPoint {
  ts: string;
  cpu_pct: number;
  mem_used: number;
  mem_limit: number;
  net_rx_bps: number;
  net_tx_bps: number;
}

export interface MetricsResponse {
  window: string;
  points: MetricPoint[];
}

export interface RunnerSummary {
  containers: RunnerContainer[];
  org_runners: Record<string, GitHubRunner[]>;
}

export interface RunnerContainer {
  name: string;
  org: string;
  state: string;
  running: boolean;
  image: string;
  started_at: string | null;
}

export interface GitHubRunner {
  id: number;
  name: string;
  status: string;
  os: string;
  labels: { name: string }[];
}

export interface WorkflowRun {
  id: number;
  repository_full_name: string;
  name: string;
  status: string;
  conclusion: string | null;
  head_branch: string;
  html_url: string;
  created_at: string;
}

export interface DnsRecordHealth {
  hostname: string;
  domain: string;
  zone: string | null;
  expected: string;
  actual: string | null;
  proxied: boolean | null;
  status: "ok" | "stale" | "missing" | "no_zone" | "error";
  error?: string;
}

export interface DnsReport {
  checked: boolean;
  in_sync: boolean;
  tunnel_target: string | null;
  records: DnsRecordHealth[];
  error?: string;
}

export interface DnsResyncResult {
  ok: boolean;
  updated: string[];
  skipped: { hostname: string; reason: string }[];
  errors: { hostname: string; error: string }[];
  tunnel_target: string | null;
}

export interface TunnelStatus {
  exists: boolean;
  running: boolean;
  state: string;
  mode: "none" | "remote";
  tunnel_id: string | null;
  tunnel_name: string | null;
  cloudflare: {
    token_set: boolean;
    account_id: string | null;
    account_name: string | null;
  };
  domains: DomainItem[];
}

export type UptimeStatus = "up" | "degraded" | "down" | "unknown";

export interface UptimePoint {
  ts: string;
  status: UptimeStatus;
  latency_ms: number | null;
}

export interface UptimeComponent {
  component: string; // admin_url | tunnel | cloudflared | traefik | docker_proxy | dns
  uptime_pct: number | null;
  current: UptimeStatus;
  detail: string | null;
  latency_ms: number | null;
  last_checked: string | null;
  sample_count: number;
  timeline: UptimePoint[];
}

export interface UptimeReport {
  window: string;
  since: string;
  components: UptimeComponent[];
}

export interface CloudflareAccount {
  id: string;
  name: string;
}

export interface CloudflareZone {
  id: string;
  name: string;
  status: string;
  account_id: string | null;
}

export interface SetTokenResponse {
  ok: boolean;
  accounts: CloudflareAccount[];
  account_id: string | null;
  account_name: string | null;
}

export interface LoginProviders {
  github: boolean;
  google: boolean;
}

export interface OAuthSettings {
  configured: boolean;
  client_id: string | null;
  proxy_url: string;
  providers: LoginProviders;
}

export interface Identity {
  id: number;
  email: string;
  enabled: boolean;
  last_login_at: string | null;
  last_login_provider: "github" | "google" | null;
  login_count: number;
  created_at: string | null;
}

export interface OnboardingState {
  complete: boolean;
  steps: {
    cloudflare_token: { done: boolean; account_name: string | null };
    tunnel: { done: boolean; tunnel_name: string | null };
    admin_domain: { done: boolean; hostname: string | null };
  };
}
