import React, { useState } from "react";
import { Lock, Mail, Building, AlertCircle } from "lucide-react";
import { api } from "../api";
import type { UserSummary } from "../types";

interface LoginModalProps {
  onSuccess: (user: UserSummary) => void;
}

export const LoginModal: React.FC<LoginModalProps> = ({ onSuccess }) => {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setLoading(true);

    try {
      const res = await api.login(email, password);
      onSuccess(res.user);
    } catch (err: any) {
      setError(err.message || "Failed to sign in. Please verify your credentials.");
    } finally {
      setLoading(false);
    }
  };

  const handleQuickFill = (demoEmail: string, demoPass: string) => {
    setEmail(demoEmail);
    setPassword(demoPass);
  };

  return (
    <div className="modal-backdrop">
      <div className="modal-content" style={{ maxWidth: "420px", padding: "36px" }}>
        <div style={{ textAlign: "center", marginBottom: "28px" }}>
          <div
            style={{
              width: "48px",
              height: "48px",
              borderRadius: "12px",
              background: "var(--accent-gradient)",
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              boxShadow: "var(--accent-glow)",
              marginBottom: "12px",
            }}
          >
            <Building size={24} color="#fff" />
          </div>
          <h2 style={{ fontSize: "1.5rem", fontWeight: 700 }}>RealEstate.AI CRM</h2>
          <p style={{ fontSize: "0.875rem", color: "var(--text-secondary)", marginTop: "4px" }}>
            Sign in to access your agency dashboard
          </p>
        </div>

        {error && (
          <div
            style={{
              padding: "10px 14px",
              backgroundColor: "rgba(239, 68, 68, 0.12)",
              border: "1px solid rgba(239, 68, 68, 0.3)",
              borderRadius: "var(--radius-md)",
              color: "#f87171",
              fontSize: "0.8125rem",
              display: "flex",
              alignItems: "center",
              gap: "8px",
              marginBottom: "18px",
            }}
          >
            <AlertCircle size={16} style={{ flexShrink: 0 }} />
            <span>{error}</span>
          </div>
        )}

        <form onSubmit={handleSubmit} style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
          <div>
            <label style={{ fontSize: "0.75rem", fontWeight: 600, color: "var(--text-secondary)", display: "block", marginBottom: "6px" }}>
              Work Email
            </label>
            <div style={{ position: "relative" }}>
              <Mail size={16} color="var(--text-muted)" style={{ position: "absolute", left: "12px", top: "50%", transform: "translateY(-50%)" }} />
              <input
                type="email"
                required
                className="form-control"
                placeholder="agent@agency.com"
                style={{ paddingLeft: "36px" }}
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </div>
          </div>

          <div>
            <label style={{ fontSize: "0.75rem", fontWeight: 600, color: "var(--text-secondary)", display: "block", marginBottom: "6px" }}>
              Password
            </label>
            <div style={{ position: "relative" }}>
              <Lock size={16} color="var(--text-muted)" style={{ position: "absolute", left: "12px", top: "50%", transform: "translateY(-50%)" }} />
              <input
                type="password"
                required
                className="form-control"
                placeholder="••••••••••••"
                style={{ paddingLeft: "36px" }}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>
          </div>

          <button
            type="submit"
            className="btn btn-primary"
            style={{ width: "100%", height: "42px", marginTop: "8px" }}
            disabled={loading}
          >
            {loading ? "Authenticating..." : "Sign In to CRM"}
          </button>
        </form>

        <div style={{ marginTop: "24px", paddingTop: "20px", borderTop: "1px solid var(--border-subtle)", textAlign: "center" }}>
          <p style={{ fontSize: "0.75rem", color: "var(--text-muted)", marginBottom: "10px" }}>
            Quick Demo Autofill:
          </p>
          <div style={{ display: "flex", gap: "8px", justifyContent: "center" }}>
            <button
              type="button"
              className="btn btn-secondary"
              style={{ fontSize: "0.75rem", padding: "4px 10px" }}
              onClick={() => handleQuickFill("manager@prestige.com", "PrestigeDev2026!")}
            >
              Manager Demo
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};
