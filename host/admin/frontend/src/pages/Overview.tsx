import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";
import { api } from "../lib/api";
import type { MetricsSummary } from "../lib/types";

export function Overview() {
  const { data } = useQuery<MetricsSummary>({
    queryKey: ["summary"],
    queryFn: () => api.get<MetricsSummary>("/api/summary"),
  });

  if (!data) {
    return <div><span className="spinner" /></div>;
  }

  const steps: { text: string; done: boolean; href: string }[] = [
    { text: "Add a domain — pick a wildcard root or a dedicated project domain.", done: data.domain_count > 0, href: "/tunnel" },
    { text: "Verify the Cloudflare tunnel is connected.", done: data.domain_count > 0, href: "/tunnel" },
    { text: "Connect a GitHub organization.", done: data.org_count > 0, href: "/projects" },
    { text: "Register a self-hosted runner so deploys land on this host.", done: data.runner.container_count > 0, href: "/cicd" },
    { text: "Bind a repository to a project slug and deploy.", done: data.repo_count > 0, href: "/projects" },
  ];

  return (
    <>
      <h1>Overview</h1>
      <p className="lede">Your self-hosted application platform. Configure domains, connect GitHub, and deploy projects from one place.</p>

      <div className="metric-grid">
        <Metric label="Routes" value={data.domain_count} link="/tunnel" />
        <Metric label="Organizations" value={data.org_count} link="/projects" />
        <Metric label="Projects" value={data.repo_count} link="/projects" />
        <Metric
          label="Runner"
          value={data.runner.container_count > 0 ? <span className="badge ok">{data.runner.container_count} active</span> : <span className="badge warn">Not set up</span>}
          link="/cicd"
        />
      </div>

      <h2>Get started</h2>
      <ol className="stepper">
        {steps.map((s, i) => (
          <li key={i} className={s.done ? "done" : (steps.findIndex(x => !x.done) === i ? "active" : "")}>
            <Link to={s.href} className="step-link" aria-label={s.text}>
              <span className="step-text">{s.text}</span>
              <ChevronRight size={18} className="step-chevron" aria-hidden="true" />
            </Link>
          </li>
        ))}
      </ol>
    </>
  );
}

function Metric({ label, value, link }: { label: string; value: React.ReactNode; link: string }) {
  return (
    <Link to={link} className="metric">
      <span className="label">{label}</span>
      <span className="value">{value}</span>
    </Link>
  );
}
