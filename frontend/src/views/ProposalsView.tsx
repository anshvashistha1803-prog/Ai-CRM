import React, { useState } from "react";
import { Sparkles, Check, X } from "lucide-react";
import { api } from "../api";
import type { Lead, LeadQualificationProposal } from "../types";

interface ProposalsViewProps {
  leads: Lead[];
  onRefresh: () => void;
}

export const ProposalsView: React.FC<ProposalsViewProps> = ({ leads, onRefresh }) => {
  const [selectedLeadId, setSelectedLeadId] = useState<string>(leads[0]?.id || "");
  const [proposals, setProposals] = useState<LeadQualificationProposal[]>([]);
  const [loading, setLoading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [reviewNotes, setReviewNotes] = useState("");
  const [activeReviewId, setActiveReviewId] = useState<string | null>(null);

  // Load proposals when lead is selected
  React.useEffect(() => {
    let ignore = false;
    if (selectedLeadId) {
      const fetchProposals = async () => {
        setLoading(true);
        try {
          const data = await api.getProposals(selectedLeadId);
          if (!ignore) setProposals(data);
        } catch {
          if (!ignore) setProposals([]);
        } finally {
          if (!ignore) setLoading(false);
        }
      };
      void fetchProposals();
    }
    return () => {
      ignore = true;
    };
  }, [selectedLeadId]);

  const handleGenerate = async () => {
    if (!selectedLeadId) return;
    setGenerating(true);
    try {
      await api.generateProposal(selectedLeadId);
      const updated = await api.getProposals(selectedLeadId);
      setProposals(updated);
      onRefresh();
    } catch (err: any) {
      alert("Error generating proposal: " + err.message);
    } finally {
      setGenerating(false);
    }
  };

  const handleReview = async (proposalId: string, action: "ACCEPT" | "REJECT") => {
    try {
      await api.reviewProposal(selectedLeadId, proposalId, action, reviewNotes || undefined);
      const updated = await api.getProposals(selectedLeadId);
      setProposals(updated);
      setActiveReviewId(null);
      setReviewNotes("");
      onRefresh();
    } catch (err: any) {
      alert("Review failed: " + err.message);
    }
  };

  const currentLead = leads.find((l) => l.id === selectedLeadId);

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">AI Qualification Review Hub</h1>
          <p className="page-subtitle">Inspect AI extracted parameters, verify confidence scores, and approve lead criteria mutations</p>
        </div>
        <button className="btn btn-primary" onClick={handleGenerate} disabled={generating || !selectedLeadId}>
          <Sparkles size={16} /> {generating ? "Extracting..." : "Generate AI Proposal"}
        </button>
      </div>

      {/* Lead Selector Pill Bar */}
      <div className="glass-card" style={{ padding: "16px 20px", marginBottom: "24px" }}>
        <label style={{ fontSize: "0.75rem", color: "var(--text-muted)", display: "block", marginBottom: "8px" }}>
          Select Prospective Lead:
        </label>
        <div style={{ display: "flex", gap: "8px", overflowX: "auto", paddingBottom: "4px" }}>
          {leads.map((l) => (
            <button
              key={l.id}
              onClick={() => setSelectedLeadId(l.id)}
              className={selectedLeadId === l.id ? "btn btn-primary" : "btn btn-secondary"}
              style={{ fontSize: "0.8125rem", whiteSpace: "nowrap" }}
            >
              {l.full_name} ({l.stage})
            </button>
          ))}
        </div>
      </div>

      {/* Main Proposals Content */}
      {loading ? (
        <div style={{ textAlign: "center", padding: "60px", color: "var(--text-muted)" }}>
          Loading AI proposals...
        </div>
      ) : proposals.length === 0 ? (
        <div className="glass-card" style={{ padding: "60px", textAlign: "center" }}>
          <Sparkles size={36} color="var(--accent-primary)" style={{ margin: "0 auto 16px" }} />
          <h3 style={{ fontSize: "1.2rem", marginBottom: "8px" }}>No Proposals for {currentLead?.full_name || "this Lead"}</h3>
          <p style={{ color: "var(--text-muted)", fontSize: "0.875rem", maxWidth: "420px", margin: "0 auto 20px" }}>
            Extract qualification criteria directly from the lead's conversation text and messages using our LLM adapter.
          </p>
          <button className="btn btn-primary" onClick={handleGenerate} disabled={generating}>
            Extract Now
          </button>
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: "20px" }}>
          {proposals.map((prop) => {
            const isPending = prop.status === "PENDING_REVIEW";
            return (
              <div
                key={prop.id}
                className="glass-card"
                style={{
                  padding: "24px",
                  borderLeft: isPending ? "4px solid var(--accent-primary)" : "1px solid var(--border-subtle)",
                }}
              >
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: "16px" }}>
                  <div>
                    <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
                      <span style={{ fontWeight: 600, fontSize: "1.05rem" }}>
                        Proposal by {prop.proposed_by} (Model: {prop.llm_model || "Mock"})
                      </span>
                      <span
                        className="badge"
                        style={{
                          backgroundColor: isPending ? "rgba(99, 102, 241, 0.15)" : "rgba(16, 185, 129, 0.15)",
                          color: isPending ? "#a5b4fc" : "#34d399",
                        }}
                      >
                        {prop.status}
                      </span>
                    </div>
                    <div style={{ fontSize: "0.75rem", color: "var(--text-muted)", marginTop: "4px" }}>
                      Created at {new Date(prop.created_at).toLocaleString()}
                    </div>
                  </div>

                  {isPending && (
                    <div style={{ display: "flex", gap: "10px" }}>
                      <button
                        className="btn btn-danger"
                        style={{ fontSize: "0.8125rem", padding: "6px 14px" }}
                        onClick={() => setActiveReviewId(activeReviewId === prop.id ? null : prop.id)}
                      >
                        <X size={14} /> Reject
                      </button>
                      <button
                        className="btn btn-success"
                        style={{ fontSize: "0.8125rem", padding: "6px 14px" }}
                        onClick={() => handleReview(prop.id, "ACCEPT")}
                      >
                        <Check size={14} /> Approve & Apply
                      </button>
                    </div>
                  )}
                </div>

                {/* Proposed Values Grid */}
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
                    gap: "14px",
                    padding: "16px",
                    backgroundColor: "rgba(0, 0, 0, 0.2)",
                    borderRadius: "var(--radius-md)",
                    marginBottom: "16px",
                  }}
                >
                  <div>
                    <div style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>Budget Max</div>
                    <div style={{ fontWeight: 600, fontSize: "0.95rem" }}>
                      {prop.budget_max ? `₹${(prop.budget_max / 10000000).toFixed(2)} Cr` : "N/A"}
                    </div>
                  </div>

                  <div>
                    <div style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>BHK & Type</div>
                    <div style={{ fontWeight: 600, fontSize: "0.95rem" }}>
                      {prop.bhk ? `${prop.bhk} BHK ` : ""}{prop.property_type || "N/A"}
                    </div>
                  </div>

                  <div>
                    <div style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>Preferred Location</div>
                    <div style={{ fontWeight: 600, fontSize: "0.95rem" }}>
                      {prop.preferred_location || "N/A"}
                    </div>
                  </div>

                  <div>
                    <div style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>Timeline / Purpose</div>
                    <div style={{ fontWeight: 600, fontSize: "0.95rem" }}>
                      {prop.move_in_timeline || "N/A"} • {prop.purpose || "BUY"}
                    </div>
                  </div>
                </div>

                {/* Confidence Scores */}
                {prop.confidence && Object.keys(prop.confidence).length > 0 && (
                  <div style={{ display: "flex", gap: "16px", flexWrap: "wrap", alignItems: "center" }}>
                    <span style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>Confidence:</span>
                    {Object.entries(prop.confidence).map(([k, v]) => (
                      <span key={k} style={{ fontSize: "0.75rem", color: "var(--text-secondary)" }}>
                        {k}: <strong style={{ color: "#34d399" }}>{Math.round(Number(v) * 100)}%</strong>
                      </span>
                    ))}
                  </div>
                )}

                {/* Rejection Review Notes Drawer */}
                {activeReviewId === prop.id && (
                  <div style={{ marginTop: "16px", paddingTop: "16px", borderTop: "1px solid var(--border-subtle)" }}>
                    <label style={{ fontSize: "0.75rem", color: "var(--text-muted)", display: "block", marginBottom: "6px" }}>
                      Rejection Reason / Review Notes:
                    </label>
                    <textarea
                      className="form-control"
                      rows={2}
                      placeholder="e.g. Client clarified on phone they only want villas, not apartments."
                      value={reviewNotes}
                      onChange={(e) => setReviewNotes(e.target.value)}
                      style={{ marginBottom: "10px" }}
                    />
                    <button
                      className="btn btn-danger"
                      style={{ fontSize: "0.8125rem" }}
                      onClick={() => handleReview(prop.id, "REJECT")}
                    >
                      Confirm Rejection
                    </button>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
};
