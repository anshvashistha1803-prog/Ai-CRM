import React, { useState } from "react";
import { Plus, Shield, Check, X, Copy } from "lucide-react";
import { api } from "../api";
import type { UserSummary, TenantInfo } from "../types";

interface TeamViewProps {
  teamUsers: UserSummary[];
  agency: TenantInfo | null;
  onRefresh: () => void;
}

export const TeamView: React.FC<TeamViewProps> = ({ teamUsers, agency, onRefresh }) => {
  const [openInviteModal, setOpenInviteModal] = useState(false);
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteName, setInviteName] = useState("");
  const [inviteRole, setInviteRole] = useState("AGENT");
  const [loading, setLoading] = useState(false);
  const [generatedToken, setGeneratedToken] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleInviteSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);

    try {
      const res = await api.inviteUser(inviteEmail, inviteName, inviteRole);
      setGeneratedToken(res.invitation_token);
      onRefresh();
    } catch (err: any) {
      setError(err.message || "Failed to invite user.");
    } finally {
      setLoading(false);
    }
  };

  const copyToClipboard = () => {
    if (generatedToken) {
      navigator.clipboard.writeText(generatedToken);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Agency Team & Access Control</h1>
          <p className="page-subtitle">Manage broker agents, sales managers, and administrators for {agency?.name || "your agency"}</p>
        </div>
        <button className="btn btn-primary" onClick={() => { setOpenInviteModal(true); setGeneratedToken(null); }}>
          <Plus size={16} /> Invite Team Member
        </button>
      </div>

      {/* Team Users Table */}
      <div className="glass-card" style={{ overflow: "hidden" }}>
        <table className="data-table">
          <thead>
            <tr>
              <th>Member Name</th>
              <th>Email Address</th>
              <th>Access Role</th>
              <th>Status</th>
              <th>Member ID</th>
            </tr>
          </thead>
          <tbody>
            {teamUsers.length === 0 ? (
              <tr>
                <td colSpan={5} style={{ textAlign: "center", padding: "40px", color: "var(--text-muted)" }}>
                  No team members found.
                </td>
              </tr>
            ) : (
              teamUsers.map((user) => (
                <tr key={user.id}>
                  <td>
                    <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
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
                        }}
                      >
                        {user.full_name ? user.full_name[0].toUpperCase() : "U"}
                      </div>
                      <span style={{ fontWeight: 600 }}>{user.full_name}</span>
                    </div>
                  </td>
                  <td>
                    <span style={{ color: "var(--text-secondary)", fontSize: "0.8125rem" }}>{user.email}</span>
                  </td>
                  <td>
                    <span
                      className="badge"
                      style={{
                        backgroundColor:
                          user.role === "AGENCY_ADMIN"
                            ? "rgba(239, 68, 68, 0.15)"
                            : user.role === "MANAGER"
                            ? "rgba(245, 158, 11, 0.15)"
                            : "rgba(99, 102, 241, 0.15)",
                        color:
                          user.role === "AGENCY_ADMIN"
                            ? "#f87171"
                            : user.role === "MANAGER"
                            ? "#fbbf24"
                            : "#a5b4fc",
                      }}
                    >
                      <Shield size={12} /> {user.role}
                    </span>
                  </td>
                  <td>
                    <span className={`badge ${user.is_active ? "badge-success" : "badge-danger"}`}>
                      {user.is_active ? "Active" : "Inactive"}
                    </span>
                  </td>
                  <td>
                    <span style={{ fontFamily: "monospace", fontSize: "0.75rem", color: "var(--text-muted)" }}>
                      {user.id.slice(0, 8)}...
                    </span>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* Invite Member Modal */}
      {openInviteModal && (
        <div className="modal-backdrop">
          <div className="modal-content" style={{ maxWidth: "460px" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "20px" }}>
              <h3 style={{ fontSize: "1.25rem" }}>Invite Team Member</h3>
              <button onClick={() => setOpenInviteModal(false)} style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer" }}>
                <X size={18} />
              </button>
            </div>

            {error && (
              <div style={{ padding: "8px 12px", backgroundColor: "rgba(239, 68, 68, 0.15)", color: "#f87171", borderRadius: "6px", fontSize: "0.8125rem", marginBottom: "14px" }}>
                {error}
              </div>
            )}

            {generatedToken ? (
              <div>
                <div style={{ textAlign: "center", padding: "16px 0", color: "var(--color-success)" }}>
                  <Check size={40} style={{ margin: "0 auto 8px" }} />
                  <h4 style={{ fontSize: "1.1rem" }}>Invitation Token Generated!</h4>
                  <p style={{ fontSize: "0.8125rem", color: "var(--text-secondary)", marginTop: "4px" }}>
                    Share this secure JWT token with the team member to complete account setup:
                  </p>
                </div>

                <div
                  style={{
                    padding: "12px",
                    backgroundColor: "#07090e",
                    borderRadius: "var(--radius-md)",
                    border: "1px solid var(--border-subtle)",
                    wordBreak: "break-all",
                    fontFamily: "monospace",
                    fontSize: "0.75rem",
                    color: "#a5b4fc",
                    marginBottom: "16px",
                    maxHeight: "100px",
                    overflowY: "auto",
                  }}
                >
                  {generatedToken}
                </div>

                <button className="btn btn-secondary" style={{ width: "100%" }} onClick={copyToClipboard}>
                  <Copy size={14} /> {copied ? "Copied to clipboard!" : "Copy Invitation Token"}
                </button>
              </div>
            ) : (
              <form onSubmit={handleInviteSubmit} style={{ display: "flex", flexDirection: "column", gap: "14px" }}>
                <div>
                  <label style={{ fontSize: "0.75rem", color: "var(--text-secondary)", display: "block", marginBottom: "4px" }}>Full Name *</label>
                  <input required type="text" className="form-control" placeholder="Rohan Varma" value={inviteName} onChange={(e) => setInviteName(e.target.value)} />
                </div>

                <div>
                  <label style={{ fontSize: "0.75rem", color: "var(--text-secondary)", display: "block", marginBottom: "4px" }}>Work Email *</label>
                  <input required type="email" className="form-control" placeholder="rohan@agency.com" value={inviteEmail} onChange={(e) => setInviteEmail(e.target.value)} />
                </div>

                <div>
                  <label style={{ fontSize: "0.75rem", color: "var(--text-secondary)", display: "block", marginBottom: "4px" }}>Role Privilege *</label>
                  <select className="form-control" value={inviteRole} onChange={(e) => setInviteRole(e.target.value)}>
                    <option value="AGENT">Agent (Assigned leads only)</option>
                    <option value="MANAGER">Manager (Agency pipeline oversight)</option>
                    <option value="AGENCY_ADMIN">Agency Admin (Full access)</option>
                  </select>
                </div>

                <button type="submit" className="btn btn-primary" style={{ height: "42px", marginTop: "8px" }} disabled={loading}>
                  {loading ? "Generating token..." : "Send Invitation"}
                </button>
              </form>
            )}
          </div>
        </div>
      )}
    </div>
  );
};
