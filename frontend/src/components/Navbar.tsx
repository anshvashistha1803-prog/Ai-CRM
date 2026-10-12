import React from "react";
import { Search, ShieldCheck } from "lucide-react";
import type { TenantInfo } from "../types";

interface NavbarProps {
  agency: TenantInfo | null;
  searchTerm: string;
  onSearchChange: (val: string) => void;
}

export const Navbar: React.FC<NavbarProps> = ({ agency, searchTerm, onSearchChange }) => {
  return (
    <header
      style={{
        height: "64px",
        backgroundColor: "rgba(14, 19, 31, 0.8)",
        backdropFilter: "blur(12px)",
        borderBottom: "1px solid var(--border-subtle)",
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        padding: "0 36px",
        position: "sticky",
        top: 0,
        zIndex: 50,
      }}
    >
      {/* Global Search Input */}
      <div style={{ position: "relative", width: "380px" }}>
        <Search
          size={16}
          color="var(--text-muted)"
          style={{ position: "absolute", left: "14px", top: "50%", transform: "translateY(-50%)" }}
        />
        <input
          type="text"
          placeholder="Search leads, phone numbers, or properties..."
          className="form-control"
          style={{ paddingLeft: "38px", height: "38px" }}
          value={searchTerm}
          onChange={(e) => onSearchChange(e.target.value)}
        />
      </div>

      {/* Agency Status and Indicators */}
      <div style={{ display: "flex", alignItems: "center", gap: "16px" }}>
        <div
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: "6px",
            padding: "4px 12px",
            borderRadius: "var(--radius-full)",
            backgroundColor: "rgba(16, 185, 129, 0.1)",
            border: "1px solid rgba(16, 185, 129, 0.25)",
            fontSize: "0.75rem",
            color: "var(--color-success)",
            fontWeight: 600,
          }}
        >
          <ShieldCheck size={14} />
          <span>Tenant Isolated: {agency?.slug || "Default"}</span>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: "6px", fontSize: "0.75rem", color: "var(--text-muted)" }}>
          <div
            style={{
              width: "8px",
              height: "8px",
              borderRadius: "50%",
              backgroundColor: "var(--color-success)",
              boxShadow: "0 0 8px var(--color-success)",
            }}
          />
          <span>API Connected</span>
        </div>
      </div>
    </header>
  );
};
