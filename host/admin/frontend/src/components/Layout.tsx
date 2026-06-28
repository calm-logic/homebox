import { useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LogOut, Moon, Sun, Waypoints, Boxes, Workflow, Users, Plug } from "lucide-react";

import { Logo } from "./Logo";
import { Modal } from "./Modal";
import { ColorPicker } from "./ColorPicker";
import { api } from "../lib/api";
import { useToast } from "../lib/toast";
import { useTheme } from "../lib/theme";
import { useAccent } from "../lib/accent";
import type { Me } from "../lib/types";

export function Layout() {
  // Keep the auth probe for session-expiry handling, but don't display the username.
  const { data: me } = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.get<Me>("/api/auth/me") });

  const qc = useQueryClient();
  const nav = useNavigate();
  const toast = useToast();
  const theme = useTheme();
  const accent = useAccent();
  const [confirmLogout, setConfirmLogout] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);

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
        <nav className="app-nav">
          <NavLink to="/tunnel"><Waypoints size={15} aria-hidden /> <span className="nav-label">Routes</span></NavLink>
          <NavLink to="/integrations"><Plug size={15} aria-hidden /> <span className="nav-label">Integrations</span></NavLink>
          <NavLink to="/projects"><Boxes size={15} aria-hidden /> <span className="nav-label">Projects</span></NavLink>
          <NavLink to="/cicd"><Workflow size={15} aria-hidden /> <span className="nav-label">CI/CD</span></NavLink>
          <NavLink to="/identities"><Users size={15} aria-hidden /> <span className="nav-label">Identities</span></NavLink>
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
        title="Sign out of Homebox?"
        footer={<>
          <span className="spacer" />
          <button className="btn" type="button" onClick={() => setConfirmLogout(false)} disabled={logout.isPending}>
            Stay signed in
          </button>
          <button className="btn danger" type="button" onClick={() => logout.mutate()} disabled={logout.isPending}>
            {logout.isPending ? <span className="spinner" /> : <><LogOut size={14} /> Sign out</>}
          </button>
        </>}
      >
        <p style={{ margin: 0 }}>
          You'll be returned to the login screen{me?.username ? <>, <strong>{me.username}</strong></> : ""}.
          Any unsaved form data on this page will be lost.
        </p>
      </Modal>
    </div>
  );
}
