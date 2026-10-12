import React from "react";
import {
  LayoutDashboard,
  Users,
  Sparkles,
  Building2,
  CalendarCheck,
  UserCheck,
  LogOut,
  Building,
} from "lucide-react";
import type { UserSummary, TenantInfo } from "../types";

interface SidebarProps {
  currentView: string;
  onSelectView: (view: string) => void;
  currentUser: UserSummary | null;
  agency: TenantInfo | null;
  onLogout: () => void;
}

export const Sidebar: React.FC<SidebarProps> = ({
  currentView,
  onSelectView,
  currentUser,
  agency,
  onLogout,
}) => {
  const navItems = [
    { id: "dashboard", label: "Dashboard", icon: LayoutDashboard },
    { id: "leads", label: "Leads & Pipeline", icon: Users },
    { id: "proposals", label: "AI Proposals", icon: Sparkles },
    { id: "properties", label: "Properties", icon: Building2 },
    { id: "visits", label: "Site Visits", icon: CalendarCheck },
    { id: "team", label: "Agency Team", icon: UserCheck },
  ];

  return (
    <aside
      style={{
        width: "260px",
        height: "100vh",
        backgroundColor: "var(--bg-surface)",
        borderRight: "1px solid var(--border-subtle)",
        display: "flex",
        flexDirection: "column",
        padding: "24px 16px",
        boxSizing: "border-box",
      }}
    >
      {/* Brand Header */}
      <div style={{ display: "flex", alignItems: "center", gap: "12px", padding: "0 8px 24px" }}>
        <div
          style={{
            width: "36px",
            height: "36px",
            borderRadius: "10px",
            background: "var(--accent-gradient)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            boxShadow: "var(--accent-glow)",
          }}
        >
          <Building size={20} color="#fff" />
        </div>
        <div>
          <div style={{ fontWeight: 700, fontSize: "1.1rem", letterSpacing: "-0.02em" }}>
            RealEstate<span style={{ color: "var(--accent-primary)" }}>.AI</span>
          </div>
          <div style={{ fontSize: "0.75rem", color: "var(--text-muted)", marginTop: "-2px" }}>
            {agency ? agency.name : "CRM Enterprise"}
          </div>
        </div>
      </div>

      {/* Navigation Links */}
      <nav style={{ flex: 1, display: "flex", flexDirection: "column", gap: "4px" }}>
        {navItems.map((item) => {
          const Icon = item.icon;
          const isActive = currentView === item.id;
          return (
            <button
              key={item.id}
              onClick={() => onSelectView(item.id)}
              style={{
                display: "flex",
                alignItems: "center",
                gap: "12px",
                width: "100%",
                padding: "10px 14px",
                borderRadius: "var(--radius-md)",
                border: "none",
                background: isActive ? "rgba(99, 102, 241, 0.15)" : "transparent",
                color: isActive ? "#ffffff" : "var(--text-secondary)",
                fontWeight: isActive ? 600 : 500,
                fontSize: "0.875rem",
                cursor: "pointer",
                transition: "all 0.15s ease",
                textAlign: "left",
                borderLeft: isActive ? "3px solid var(--accent-primary)" : "3px solid transparent",
              }}
            >
              <Icon size={18} color={isActive ? "var(--accent-primary)" : "var(--text-muted)"} />
              {item.label}
            </button>
          );
        })}
      </nav>

      {/* User Session Footer */}
      {currentUser && (
        <div
          style={{
            padding: "14px",
            backgroundColor: "rgba(255, 255, 255, 0.03)",
            borderRadius: "var(--radius-md)",
            border: "1px solid var(--border-subtle)",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: "10px", overflow: "hidden" }}>
            <div
              style={{
                width: "32px",
                height: "32px",
                borderRadius: "50%",
                background: "#334155",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontWeight: 600,
                fontSize: "0.8rem",
                color: "#e2e8f0",
                flexShrink: 0,
              }}
            >
              {currentUser.full_name ? currentUser.full_name[0].toUpperCase() : "U"}
            </div>
            <div style={{ overflow: "hidden" }}>
              <div
                style={{
                  fontSize: "0.8125rem",
                  fontWeight: 600,
                  whiteSpace: "nowrap",
                  textOverflow: "ellipsis",
                  overflow: "hidden",
                }}
              >
                {currentUser.full_name}
              </div>
              <div style={{ fontSize: "0.7rem", color: "var(--text-muted)" }}>
                {currentUser.role}
              </div>
            </div>
          </div>
          <button
            onClick={onLogout}
            title="Log Out"
            style={{
              background: "none",
              border: "none",
              color: "var(--text-muted)",
              cursor: "pointer",
              padding: "4px",
              display: "flex",
            }}
          >
            <LogOut size={16} />
          </button>
        </div>
      )}
    </aside>
  );
};
