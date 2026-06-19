import { useQuery } from "@tanstack/react-query";
import { Navigate, useLocation } from "react-router-dom";
import { api, ApiError } from "../lib/api";
import type { Me } from "../lib/types";

export function RequireAuth({ children }: { children: React.ReactNode }) {
  const loc = useLocation();
  const q = useQuery<Me>({
    queryKey: ["me"],
    queryFn: () => api.get<Me>("/api/auth/me"),
    retry: (count, err) => count < 1 && !(err instanceof ApiError && err.status === 401),
  });

  if (q.isLoading) {
    return <div style={{ display: "grid", placeItems: "center", height: "100vh" }}><span className="spinner" /></div>;
  }
  if (q.isError) {
    const status = q.error instanceof ApiError ? q.error.status : 500;
    if (status === 401) {
      return <Navigate to={`/login?next=${encodeURIComponent(loc.pathname + loc.search)}`} replace />;
    }
    return <div style={{ padding: "2rem", color: "var(--danger)" }}>Auth check failed: {String(q.error)}</div>;
  }
  return <>{children}</>;
}
