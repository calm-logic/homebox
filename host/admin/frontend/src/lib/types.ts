// Shared types between API responses and UI.

export interface Me {
  username: string;
}

export interface MetricsSummary {
  org_count: number;
  repo_count: number;
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
  project_slug: string | null;
  is_primary: boolean;
  cloudflare_routed: boolean;
}

export interface OrgItem {
  id: number;
  login: string;
  created_at: string;
  source: "pat" | "oauth";
}

export type DeploymentStatus =
  | "queued" | "cloning" | "building" | "starting" | "running" | "failed" | "stopped";

export interface DeploymentInfo {
  status: DeploymentStatus;
  url: string | null;
  commit_sha: string | null;
  error: string | null;
  trigger: "manual" | "webhook";
  updated_at: string | null;
}

export interface RepoItem {
  id: number;
  full_name: string;
  default_branch: string;
  project_slug: string | null;
  managed: boolean;
  deployment: DeploymentInfo | null;
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
