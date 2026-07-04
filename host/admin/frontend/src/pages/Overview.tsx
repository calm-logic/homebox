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
    { text: "Add a domain", done: data.domain_count > 0, href: "/domains" },
    { text: "Connect the Cloudflare tunnel", done: data.domain_count > 0, href: "/integrations" },
    { text: "Connect a GitHub organization", done: data.integration_count > 0, href: "/integrations" },
    { text: "Adopt a repository", done: data.managed_count > 0, href: "/projects" },
  ];

  return (
    <>
      <h1>Overview</h1>
      <p className="lede">Domains, GitHub, and deploys — managed from one place.</p>

      <div className="metric-grid">
        <Metric label="Domains" value={data.domain_count} link="/domains" />
        <Metric label="Integrations" value={data.integration_count} link="/integrations" />
        <Metric label="Projects" value={`${data.managed_count}/${data.project_count}`} link="/projects" />
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
