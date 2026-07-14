import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";

import { Layout } from "./components/Layout";
import { RequireAuth } from "./components/RequireAuth";
import { ToastProvider } from "./lib/toast";
import { ThemeProvider } from "./lib/theme";
import { api } from "./lib/api";
import { Login } from "./pages/Login";
import { Overview } from "./pages/Overview";
import { DomainsPage } from "./pages/DomainsPage";
import { System } from "./pages/System";
import { Projects } from "./pages/Projects";
import { ProjectDetail } from "./pages/ProjectDetail";
import { ServiceDetail } from "./pages/ServiceDetail";
import { Integrations } from "./pages/Integrations";
import { IntegrationDetail } from "./pages/IntegrationDetail";
import { Identities } from "./pages/Identities";
import { Onboarding } from "./pages/Onboarding";
import { OAuthCallback } from "./pages/OAuthCallback";
import type { OnboardingState } from "./lib/types";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 5_000 },
  },
});

/**
 * RequireOnboarded — when /api/onboarding/state.complete is false, every
 * authenticated route punts to the wizard. Wraps the normal Layout so the
 * /onboarding route itself stays accessible (it isn't wrapped by this).
 */
function RequireOnboarded({ children }: { children: React.ReactNode }) {
  const location = useLocation();
  const { data, isLoading } = useQuery<OnboardingState>({
    queryKey: ["onboarding"],
    queryFn: () => api.get<OnboardingState>("/api/onboarding/state"),
    staleTime: 10_000,
  });
  // Render nothing during the first probe to avoid a redirect flicker.
  if (isLoading) return null;
  // /system stays reachable pre-onboarding: a fresh node can join an existing
  // cluster instead of being onboarded from scratch (it inherits the cluster's
  // tunnel, domains and projects through the join sync).
  const exempt = location.pathname === "/onboarding" || location.pathname === "/system";
  if (data && !data.complete && !exempt) {
    return <Navigate to="/onboarding" replace />;
  }
  return <>{children}</>;
}

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <ToastProvider>
          <BrowserRouter>
          <Routes>
            <Route path="/login" element={<Login />} />
            <Route path="/oauth/callback" element={<OAuthCallback />} />
            <Route path="/onboarding" element={<RequireAuth><Onboarding /></RequireAuth>} />
            <Route element={<RequireAuth><RequireOnboarded><Layout /></RequireOnboarded></RequireAuth>}>
              <Route path="/" element={<Overview />} />
              <Route path="/domains" element={<DomainsPage />} />
              <Route path="/system" element={<System />} />
              <Route path="/integrations" element={<Integrations />} />
              <Route path="/integrations/:integrationId" element={<IntegrationDetail />} />
              <Route path="/projects" element={<Projects />} />
              <Route path="/projects/:projectId" element={<ProjectDetail />} />
              <Route path="/projects/:projectId/deployments/:deploymentId" element={<ProjectDetail />} />
              <Route path="/projects/:projectId/services/:serviceId" element={<ServiceDetail />} />
              <Route path="/projects/:projectId/:section" element={<ProjectDetail />} />
              <Route path="/identities" element={<Identities />} />
              {/* Legacy routes — pages were merged into the parents above. */}
              <Route path="/tunnel" element={<Navigate to="/domains" replace />} />
              <Route path="/cicd" element={<Navigate to="/projects" replace />} />
              <Route path="/github" element={<Navigate to="/integrations" replace />} />
              <Route path="/runner" element={<Navigate to="/projects" replace />} />
              <Route path="/cluster" element={<Navigate to="/system" replace />} />
            </Route>
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
          </BrowserRouter>
        </ToastProvider>
      </ThemeProvider>
    </QueryClientProvider>
  );
}
