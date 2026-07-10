import { useRef, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LogOut, Moon, Sun, Globe, Boxes, Workflow, Users, Plug, Activity } from "lucide-react";

import { Logo } from "./Logo";
import { Modal } from "./Modal";
import { ColorPicker } from "./ColorPicker";
import { api } from "../lib/api";
import { useToast } from "../lib/toast";
import { useTheme } from "../lib/theme";
import { useAccent } from "../lib/accent";
import { useTabIndicator } from "../lib/useTabIndicator";
import type { Me } from "../lib/types";

export function Layout() {
  // Keep the auth probe for session-expiry handling, but don't display the username.
  const { data: me } = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.get<Me>("/api/auth/me") });

  const qc = useQueryClient();
  const nav = useNavigate();
  const location = useLocation();
  const toast = useToast();
  const theme = useTheme();
  const accent = useAccent();
  const [confirmLogout, setConfirmLogout] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const navRef = useRef<HTMLElement>(null);
  useTabIndicator(navRef, "a.active", [location.pathname]);

  const logout = useMutation({
    mutationFn: () => api.post("/api/auth/logout"),
    onSuccess: () => { setConfirmLogout(false); qc.clear(); nav("/login", { replace: true }); },
    onError: () => toast.show("Logout failed", "fail"),
  });

  return (
    <div className="shell">
      <header className="app-header">
        <NavLink to="/" className="brand" aria-label="Homebox">
          <Logo size={39} />
          <span>Homebox</span>
        </NavLink>
        <nav className="app-nav" ref={navRef}>
          <span className="tab-indicator" aria-hidden />
          <NavLink to="/domains"><Globe size={15} aria-hidden /> <span className="nav-label">Domains</span></NavLink>
          <NavLink to="/integrations"><Plug size={15} aria-hidden /> <span className="nav-label">Integrations</span></NavLink>
          <NavLink to="/projects"><Boxes size={15} aria-hidden /> <span className="nav-label">Projects</span></NavLink>
          <NavLink to="/identities"><Users size={15} aria-hidden /> <span className="nav-label">Identities</span></NavLink>
          <NavLink to="/system"><Activity size={15} aria-hidden /> <span className="nav-label">System</span></NavLink>
        </nav>
        <div className="app-user">
          <div className="accent-wrap">
            <button
              type="button"
              className="accent-swatch"
              onClick={() => setPickerOpen(o => !o)}
              aria-label="Change accent color"
              aria-expanded={pickerOpen}
              title="Accent color"
              style={{ background: "var(--accent)" }}
            />
            {pickerOpen && (
              <ColorPicker
                value={accent.accent}
                onChange={hex => { void accent.set(hex); }}
                onClose={() => setPickerOpen(false)}
              />
            )}
          </div>
          <button
            className="icon-btn"
            type="button"
            onClick={theme.toggle}
            aria-label={theme.theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
            title={theme.theme === "dark" ? "Light theme" : "Dark theme"}
          >
            {theme.theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
          </button>
          <button
            className="icon-btn"
            type="button"
            onClick={() => setConfirmLogout(true)}
            aria-label="Sign out"
            title="Sign out"
          >
            <LogOut size={16} />
          </button>
        </div>
      </header>
      <main className="app-main">
        <Outlet />
      </main>

      <Modal
        open={confirmLogout}
        onClose={() => { if (!logout.isPending) setConfirmLogout(false); }}
        title="Sign out?"
        footer={<>
          <span className="spacer" />
          <button className="btn ghost" type="button" onClick={() => setConfirmLogout(false)} disabled={logout.isPending}>
            Cancel
          </button>
          <button className="btn danger" type="button" onClick={() => logout.mutate()} disabled={logout.isPending}>
            {logout.isPending ? <span className="spinner" /> : <><LogOut size={14} /> Sign out</>}
          </button>
        </>}
      >
        <p style={{ margin: 0 }}>
          {me?.username ? <><strong>{me.username}</strong> will be signed out. </> : ""}Unsaved changes will be lost.
        </p>
      </Modal>
    </div>
  );
}
