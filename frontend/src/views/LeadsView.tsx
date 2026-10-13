import { useState, useEffect } from "react";
import {
  Search,
  Filter,
  Plus,
  Flame,
  Sparkles,
  ChevronRight,
  ShieldAlert,
  X,
} from "lucide-react";
import { api } from "../api";
import type { Lead, LeadActivity, LeadPropertyMatch, UserSummary } from "../types";

interface LeadsViewProps {
  leads: Lead[];
  teamUsers: UserSummary[];
  currentUser?: UserSummary | null;
  onRefresh: () => void;
  openCreateModal: boolean;
  onCloseCreateModal: () => void;
}

function getAllowedTransitions(currentStage: string, role?: string): string[] {
  const isManagerOrAdmin =
    role === "MANAGER" || role === "AGENCY_ADMIN" || role === "SUPER_ADMIN";
  switch (currentStage) {
    case "NEW":
      return isManagerOrAdmin
        ? ["QUALIFYING", "DISQUALIFIED", "DO_NOT_CONTACT"]
        : ["QUALIFYING", "DO_NOT_CONTACT"];
    case "QUALIFYING":
      return isManagerOrAdmin
        ? ["QUALIFIED", "DISQUALIFIED", "DO_NOT_CONTACT"]
        : ["QUALIFIED", "DO_NOT_CONTACT"];
    case "QUALIFIED":
      return isManagerOrAdmin
        ? ["SITE_VISIT_SCHEDULED", "DISQUALIFIED", "DO_NOT_CONTACT"]
        : ["SITE_VISIT_SCHEDULED", "DO_NOT_CONTACT"];
    case "SITE_VISIT_SCHEDULED":
      return isManagerOrAdmin
        ? ["NEGOTIATION", "QUALIFIED", "DISQUALIFIED", "DO_NOT_CONTACT"]
        : ["NEGOTIATION", "DO_NOT_CONTACT"];
    case "NEGOTIATION":
      return isManagerOrAdmin
        ? ["WON", "LOST", "QUALIFIED", "SITE_VISIT_SCHEDULED", "DO_NOT_CONTACT"]
        : ["WON", "LOST", "DO_NOT_CONTACT"];
    case "LOST":
      return isManagerOrAdmin ? ["QUALIFYING"] : [];
    case "STALLED":
      return isManagerOrAdmin
        ? ["QUALIFYING", "QUALIFIED", "DISQUALIFIED", "DO_NOT_CONTACT"]
        : ["DO_NOT_CONTACT"];
    default:
      return [];
  }
}

export const LeadsView: React.FC<LeadsViewProps> = ({
  leads,
  teamUsers,
  currentUser,
  onRefresh,
  openCreateModal,
  onCloseCreateModal,
}) => {
  const [selectedLead, setSelectedLead] = useState<Lead | null>(null);
  const [activeTab, setActiveTab] = useState<"details" | "matches" | "timeline">("details");
  const [stageFilter, setStageFilter] = useState<string>("ALL");
  const [search, setSearch] = useState<string>("");

  // Drawer states
  const [activities, setActivities] = useState<LeadActivity[]>([]);
  const [matches, setMatches] = useState<LeadPropertyMatch[]>([]);
  const [loadingMatches, setLoadingMatches] = useState(false);
  const [transitioning, setTransitioning] = useState(false);

  // New Lead form state
  const [newFullName, setNewFullName] = useState("");
  const [newPhone, setNewPhone] = useState("");
  const [newEmail, setNewEmail] = useState("");
  const [newBudgetMax, setNewBudgetMax] = useState("");
  const [newBhk, setNewBhk] = useState("3");
  const [newPropType, setNewPropType] = useState("APARTMENT");
  const [newLocation, setNewLocation] = useState("");
  const [createLoading, setCreateLoading] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  // Load activities and matches when a lead is selected
  useEffect(() => {
    if (selectedLead) {
      api.getLeadActivities(selectedLead.id).then(setActivities).catch(() => {});
      api.getMatches(selectedLead.id).then(setMatches).catch(() => {});
    }
  }, [selectedLead]);

  const handleComputeMatches = async () => {
    if (!selectedLead) return;
    setLoadingMatches(true);
    try {
      const res = await api.computeMatches(selectedLead.id);
      setMatches(res);
    } catch (err: any) {
      alert("Error computing matches: " + err.message);
    } finally {
      setLoadingMatches(false);
    }
  };

  const handleStageTransition = async (targetStage: string) => {
    if (!selectedLead) return;
    setTransitioning(true);
    try {
      const updated = await api.transitionLead(selectedLead.id, targetStage);
      setSelectedLead(updated);
      onRefresh();
    } catch (err: any) {
      alert("Transition failed: " + err.message);
    } finally {
      setTransitioning(false);
    }
  };

  const handleAssignAgent = async (agentId: string) => {
    if (!selectedLead) return;
    try {
      const updated = await api.assignLead(selectedLead.id, agentId || null);
      setSelectedLead(updated);
      onRefresh();
    } catch (err: any) {
      alert("Assignment failed: " + err.message);
    }
  };

  const handleCreateSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setCreateLoading(true);
    setCreateError(null);

    try {
      await api.createLead({
        full_name: newFullName,
        phone: newPhone,
        email: newEmail || undefined,
        budget_max: newBudgetMax ? parseInt(newBudgetMax, 10) : undefined,
        bhk: newBhk ? parseInt(newBhk, 10) : undefined,
        property_type: newPropType,
        preferred_location: newLocation || undefined,
      });
      onCloseCreateModal();
      onRefresh();
      setNewFullName("");
      setNewPhone("");
      setNewEmail("");
      setNewBudgetMax("");
    } catch (err: any) {
      setCreateError(err.message || "Failed to create lead.");
    } finally {
      setCreateLoading(false);
    }
  };

  const filteredLeads = leads.filter((l) => {
    if (stageFilter !== "ALL" && l.stage !== stageFilter) return false;
    if (search) {
      const q = search.toLowerCase();
      const nameMatch = l.full_name?.toLowerCase().includes(q);
      const locMatch = l.preferred_location?.toLowerCase().includes(q);
      const stageMatch = l.stage?.toLowerCase().includes(q);
      return nameMatch || locMatch || stageMatch;
    }
    return true;
  });

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Lead Management & Pipeline</h1>
          <p className="page-subtitle">Track prospects through qualification, automated scoring, and property matching</p>
        </div>
        <button className="btn btn-primary" onClick={() => onCloseCreateModal()}>
          <Plus size={16} /> Add Prospective Lead
        </button>
      </div>

      {/* Filter and Search Bar */}
      <div
        className="glass-card"
        style={{
          padding: "16px 20px",
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          gap: "16px",
          marginBottom: "20px",
        }}
      >
        <div style={{ display: "flex", gap: "8px", flexWrap: "wrap", alignItems: "center" }}>
          <Filter size={16} color="var(--text-muted)" style={{ marginRight: "4px" }} />
          {[
            { id: "ALL", label: "All" },
            { id: "NEW", label: "New" },
            { id: "QUALIFYING", label: "Qualifying" },
            { id: "QUALIFIED", label: "Qualified" },
            { id: "SITE_VISIT_SCHEDULED", label: "Visit Scheduled" },
            { id: "NEGOTIATION", label: "Negotiation" },
            { id: "WON", label: "Won" },
            { id: "STALLED", label: "Stalled" },
          ].map((st) => (
            <button
              key={st.id}
              onClick={() => setStageFilter(st.id)}
              className={stageFilter === st.id ? "btn btn-primary" : "btn btn-secondary"}
              style={{ fontSize: "0.75rem", padding: "5px 12px" }}
            >
              {st.label}
            </button>
          ))}
        </div>

        <div style={{ width: "260px", position: "relative" }}>
          <Search size={14} color="var(--text-muted)" style={{ position: "absolute", left: "10px", top: "50%", transform: "translateY(-50%)" }} />
          <input
            type="text"
            className="form-control"
            placeholder="Filter list..."
            style={{ paddingLeft: "32px", height: "34px", fontSize: "0.8125rem" }}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
      </div>

      {/* Leads Table */}
      <div className="glass-card" style={{ overflow: "hidden" }}>
        <table className="data-table">
          <thead>
            <tr>
              <th>Lead Name</th>
              <th>Intent & Health</th>
              <th>Stage</th>
              <th>Requirements</th>
              <th>Assigned Agent</th>
              <th>Last Active</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {filteredLeads.length === 0 ? (
              <tr>
                <td colSpan={7} style={{ textAlign: "center", padding: "40px", color: "var(--text-muted)" }}>
                  No leads found matching the filter criteria.
                </td>
              </tr>
            ) : (
              filteredLeads.map((lead) => {
                const assignedUser = teamUsers.find((u) => u.id === lead.assigned_agent_id);
                return (
                  <tr key={lead.id} onClick={() => setSelectedLead(lead)}>
                    <td>
                      <div style={{ fontWeight: 600 }}>{lead.full_name}</div>
                      <div style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>
                        {lead.source} • {lead.preferred_contact}
                      </div>
                    </td>
                    <td>
                      <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                        <span className={`badge badge-${lead.lead_health.toLowerCase()}`}>
                          {lead.lead_health === "HOT" && <Flame size={12} />}
                          {lead.lead_health}
                        </span>
                        {lead.intent_score !== undefined && (
                          <span style={{ fontSize: "0.75rem", fontWeight: 700 }}>
                            {lead.intent_score}
                          </span>
                        )}
                      </div>
                    </td>
                    <td>
                      <span
                        className="badge"
                        style={{
                          backgroundColor: "rgba(99, 102, 241, 0.12)",
                          color: "#a5b4fc",
                          border: "1px solid rgba(99, 102, 241, 0.25)",
                        }}
                      >
                        {lead.stage}
                      </span>
                    </td>
                    <td>
                      <div style={{ fontSize: "0.8125rem" }}>
                        {lead.bhk ? `${lead.bhk} BHK ` : ""}{lead.property_type || "Any Type"}
                      </div>
                      <div style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>
                        {lead.preferred_location || "Location open"}
                      </div>
                    </td>
                    <td>
                      <div style={{ fontSize: "0.8125rem", color: assignedUser ? "var(--text-primary)" : "var(--text-muted)" }}>
                        {assignedUser ? assignedUser.full_name : "Unassigned"}
                      </div>
                    </td>
                    <td>
                      <div style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>
                        {lead.last_activity_at ? new Date(lead.last_activity_at).toLocaleDateString() : "Just now"}
                      </div>
                    </td>
                    <td style={{ textAlign: "right" }}>
                      <ChevronRight size={16} color="var(--text-muted)" />
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      {/* Lead Detail Drawer */}
      {selectedLead && (
        <div className="modal-backdrop" onClick={() => setSelectedLead(null)}>
          <div
            className="modal-content"
            style={{
              maxWidth: "760px",
              maxHeight: "90vh",
              overflowY: "auto",
              padding: "32px",
            }}
            onClick={(e) => e.stopPropagation()}
          >
            {/* Header */}
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: "20px" }}>
              <div>
                <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
                  <h2 style={{ fontSize: "1.4rem" }}>{selectedLead.full_name}</h2>
                  <span className={`badge badge-${selectedLead.lead_health.toLowerCase()}`}>
                    {selectedLead.lead_health} ({selectedLead.intent_score !== undefined && selectedLead.intent_score !== null ? selectedLead.intent_score : "Unscored"})
                  </span>
                  {selectedLead.do_not_contact && (
                    <span className="badge badge-danger">
                      <ShieldAlert size={12} /> Do Not Contact
                    </span>
                  )}
                </div>
                <div style={{ fontSize: "0.8125rem", color: "var(--text-muted)", marginTop: "4px" }}>
                  Source: {selectedLead.source} • Registered: {new Date(selectedLead.created_at).toLocaleDateString()}
                </div>
              </div>
              <button
                onClick={() => setSelectedLead(null)}
                style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer" }}
              >
                <X size={20} />
              </button>
            </div>

            {/* Quick Action Bar (Stage & Agent) */}
            <div
              style={{
                display: "flex",
                gap: "16px",
                padding: "16px",
                backgroundColor: "rgba(255, 255, 255, 0.03)",
                borderRadius: "var(--radius-md)",
                border: "1px solid var(--border-subtle)",
                marginBottom: "24px",
                alignItems: "center",
              }}
            >
              <div style={{ flex: 1 }}>
                <label style={{ fontSize: "0.75rem", color: "var(--text-muted)", display: "block", marginBottom: "4px" }}>
                  Stage State Machine
                </label>
                {(() => {
                  const allowedNext = getAllowedTransitions(selectedLead.stage, currentUser?.role);
                  const isTerminal = allowedNext.length === 0;
                  return (
                    <select
                      className="form-control"
                      style={{ height: "36px", fontSize: "0.8125rem" }}
                      value={selectedLead.stage}
                      disabled={transitioning || isTerminal}
                      onChange={(e) => handleStageTransition(e.target.value)}
                    >
                      <option value={selectedLead.stage}>
                        {selectedLead.stage} {isTerminal ? "(Terminal)" : "(Current)"}
                      </option>
                      {allowedNext.map((st) => (
                        <option key={st} value={st}>
                          → {st}
                        </option>
                      ))}
                    </select>
                  );
                })()}
              </div>

              <div style={{ flex: 1 }}>
                <label style={{ fontSize: "0.75rem", color: "var(--text-muted)", display: "block", marginBottom: "4px" }}>
                  Assigned Agent
                </label>
                <select
                  className="form-control"
                  style={{ height: "36px", fontSize: "0.8125rem" }}
                  value={selectedLead.assigned_agent_id || ""}
                  onChange={(e) => handleAssignAgent(e.target.value)}
                >
                  <option value="">Unassigned</option>
                  {teamUsers.map((u) => (
                    <option key={u.id} value={u.id}>
                      {u.full_name} ({u.role})
                    </option>
                  ))}
                </select>
              </div>
            </div>

            {/* Tabs Header */}
            <div style={{ display: "flex", gap: "8px", borderBottom: "1px solid var(--border-subtle)", marginBottom: "20px" }}>
              {[
                { id: "details", label: "Requirements & AI Extraction" },
                { id: "matches", label: `Property Matches (${matches.length})` },
                { id: "timeline", label: `Timeline (${activities.length})` },
              ].map((t) => (
                <button
                  key={t.id}
                  onClick={() => setActiveTab(t.id as any)}
                  style={{
                    padding: "8px 16px",
                    background: "none",
                    border: "none",
                    borderBottom: activeTab === t.id ? "2px solid var(--accent-primary)" : "2px solid transparent",
                    color: activeTab === t.id ? "#ffffff" : "var(--text-secondary)",
                    fontWeight: activeTab === t.id ? 600 : 500,
                    fontSize: "0.875rem",
                    cursor: "pointer",
                  }}
                >
                  {t.label}
                </button>
              ))}
            </div>

            {/* Tab: Details */}
            {activeTab === "details" && (
              <div>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "16px", marginBottom: "20px" }}>
                  <div style={{ padding: "14px", backgroundColor: "rgba(255, 255, 255, 0.02)", borderRadius: "var(--radius-md)" }}>
                    <div style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>Budget Range</div>
                    <div style={{ fontSize: "1rem", fontWeight: 600, marginTop: "2px" }}>
                      {selectedLead.budget_max ? `Up to ₹${(selectedLead.budget_max / 10000000).toFixed(2)} Cr` : "Not specified"}
                    </div>
                  </div>

                  <div style={{ padding: "14px", backgroundColor: "rgba(255, 255, 255, 0.02)", borderRadius: "var(--radius-md)" }}>
                    <div style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>Property Configuration</div>
                    <div style={{ fontSize: "1rem", fontWeight: 600, marginTop: "2px" }}>
                      {selectedLead.bhk ? `${selectedLead.bhk} BHK ` : ""}{selectedLead.property_type || "Any Type"}
                    </div>
                  </div>

                  <div style={{ padding: "14px", backgroundColor: "rgba(255, 255, 255, 0.02)", borderRadius: "var(--radius-md)" }}>
                    <div style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>Preferred Location</div>
                    <div style={{ fontSize: "1rem", fontWeight: 600, marginTop: "2px" }}>
                      {selectedLead.preferred_location || "Open location"}
                    </div>
                  </div>

                  <div style={{ padding: "14px", backgroundColor: "rgba(255, 255, 255, 0.02)", borderRadius: "var(--radius-md)" }}>
                    <div style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>Move-In Timeline</div>
                    <div style={{ fontSize: "1rem", fontWeight: 600, marginTop: "2px" }}>
                      {selectedLead.move_in_timeline || "Flexible"}
                    </div>
                  </div>
                </div>

                {selectedLead.raw_message_text && (
                  <div style={{ marginBottom: "20px" }}>
                    <h4 style={{ fontSize: "0.875rem", color: "var(--text-secondary)", marginBottom: "8px" }}>
                      Conversation Text Transcript
                    </h4>
                    <div
                      style={{
                        padding: "12px 16px",
                        backgroundColor: "#07090e",
                        borderRadius: "var(--radius-md)",
                        fontSize: "0.8125rem",
                        fontFamily: "monospace",
                        color: "#94a3b8",
                        whiteSpace: "pre-wrap",
                      }}
                    >
                      {selectedLead.raw_message_text}
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* Tab: Matches */}
            {activeTab === "matches" && (
              <div>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "16px" }}>
                  <p style={{ fontSize: "0.8125rem", color: "var(--text-secondary)" }}>
                    Inventory matches ranked by deterministic compatibility score
                  </p>
                  <button className="btn btn-primary" onClick={handleComputeMatches} disabled={loadingMatches} style={{ fontSize: "0.75rem" }}>
                    <Sparkles size={14} /> {loadingMatches ? "Calculating..." : "Recompute Matches"}
                  </button>
                </div>

                {matches.length === 0 ? (
                  <div style={{ textAlign: "center", padding: "40px", color: "var(--text-muted)" }}>
                    No matched properties found yet. Click "Recompute Matches" to evaluate available agency inventory.
                  </div>
                ) : (
                  <div style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
                    {matches.map((match) => (
                      <div
                        key={match.id}
                        style={{
                          padding: "16px",
                          borderRadius: "var(--radius-md)",
                          backgroundColor: "rgba(255, 255, 255, 0.03)",
                          border: "1px solid var(--border-subtle)",
                          display: "flex",
                          justifyContent: "space-between",
                          alignItems: "center",
                        }}
                      >
                        <div>
                          <div style={{ fontWeight: 600, fontSize: "0.95rem" }}>
                            {match.property ? match.property.title : `Property ID: ${match.property_id.slice(0, 8)}`}
                          </div>
                          <div style={{ fontSize: "0.8125rem", color: "var(--text-muted)", marginTop: "2px" }}>
                            Status: <span style={{ color: "var(--color-success)" }}>{match.status}</span>
                          </div>
                        </div>
                        <div style={{ textAlign: "right" }}>
                          <span
                            className="badge"
                            style={{
                              backgroundColor: "rgba(16, 185, 129, 0.15)",
                              color: "#34d399",
                              fontSize: "0.875rem",
                              fontWeight: 700,
                            }}
                          >
                            {match.match_score}% Match
                          </span>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {/* Tab: Timeline */}
            {activeTab === "timeline" && (
              <div style={{ display: "flex", flexDirection: "column", gap: "14px" }}>
                {activities.length === 0 ? (
                  <div style={{ textAlign: "center", padding: "30px", color: "var(--text-muted)" }}>
                    No activities recorded yet.
                  </div>
                ) : (
                  activities.map((act) => (
                    <div
                      key={act.id}
                      style={{
                        padding: "12px 14px",
                        borderRadius: "var(--radius-md)",
                        backgroundColor: "rgba(255, 255, 255, 0.02)",
                        borderLeft: "3px solid var(--accent-primary)",
                      }}
                    >
                      <div style={{ display: "flex", justifyContent: "space-between", fontSize: "0.8125rem" }}>
                        <span style={{ fontWeight: 600 }}>{act.activity_type}</span>
                        <span style={{ color: "var(--text-muted)", fontSize: "0.75rem" }}>
                          {new Date(act.created_at).toLocaleString()}
                        </span>
                      </div>
                      <div style={{ fontSize: "0.8125rem", color: "var(--text-secondary)", marginTop: "4px" }}>
                        {act.description}
                      </div>
                    </div>
                  ))
                )}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Create Lead Modal */}
      {openCreateModal && (
        <div className="modal-backdrop">
          <div className="modal-content" style={{ maxWidth: "480px" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "20px" }}>
              <h3 style={{ fontSize: "1.25rem" }}>Add Prospective Lead</h3>
              <button onClick={onCloseCreateModal} style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer" }}>
                <X size={18} />
              </button>
            </div>

            {createError && (
              <div style={{ padding: "8px 12px", backgroundColor: "rgba(239, 68, 68, 0.15)", color: "#f87171", borderRadius: "6px", fontSize: "0.8125rem", marginBottom: "14px" }}>
                {createError}
              </div>
            )}

            <form onSubmit={handleCreateSubmit} style={{ display: "flex", flexDirection: "column", gap: "14px" }}>
              <div>
                <label style={{ fontSize: "0.75rem", color: "var(--text-secondary)", display: "block", marginBottom: "4px" }}>Full Name *</label>
                <input required type="text" className="form-control" value={newFullName} onChange={(e) => setNewFullName(e.target.value)} />
              </div>

              <div>
                <label style={{ fontSize: "0.75rem", color: "var(--text-secondary)", display: "block", marginBottom: "4px" }}>Phone Number (E.164) *</label>
                <input required type="tel" className="form-control" placeholder="+919876543210" value={newPhone} onChange={(e) => setNewPhone(e.target.value)} />
              </div>

              <div>
                <label style={{ fontSize: "0.75rem", color: "var(--text-secondary)", display: "block", marginBottom: "4px" }}>Email Address</label>
                <input type="email" className="form-control" placeholder="client@example.com" value={newEmail} onChange={(e) => setNewEmail(e.target.value)} />
              </div>

              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "12px" }}>
                <div>
                  <label style={{ fontSize: "0.75rem", color: "var(--text-secondary)", display: "block", marginBottom: "4px" }}>BHK</label>
                  <select className="form-control" value={newBhk} onChange={(e) => setNewBhk(e.target.value)}>
                    <option value="1">1 BHK</option>
                    <option value="2">2 BHK</option>
                    <option value="3">3 BHK</option>
                    <option value="4">4 BHK</option>
                  </select>
                </div>
                <div>
                  <label style={{ fontSize: "0.75rem", color: "var(--text-secondary)", display: "block", marginBottom: "4px" }}>Property Type</label>
                  <select className="form-control" value={newPropType} onChange={(e) => setNewPropType(e.target.value)}>
                    <option value="APARTMENT">Apartment</option>
                    <option value="VILLA">Villa</option>
                    <option value="PLOT">Plot</option>
                    <option value="COMMERCIAL">Commercial</option>
                  </select>
                </div>
              </div>

              <div>
                <label style={{ fontSize: "0.75rem", color: "var(--text-secondary)", display: "block", marginBottom: "4px" }}>Budget Max (INR)</label>
                <input type="number" className="form-control" placeholder="15000000" value={newBudgetMax} onChange={(e) => setNewBudgetMax(e.target.value)} />
              </div>

              <div>
                <label style={{ fontSize: "0.75rem", color: "var(--text-secondary)", display: "block", marginBottom: "4px" }}>Preferred Location</label>
                <input type="text" className="form-control" placeholder="e.g. Whitefield, Indiranagar" value={newLocation} onChange={(e) => setNewLocation(e.target.value)} />
              </div>

              <button type="submit" className="btn btn-primary" style={{ height: "40px", marginTop: "8px" }} disabled={createLoading}>
                {createLoading ? "Creating..." : "Save Prospective Lead"}
              </button>
            </form>
          </div>
        </div>
      )}
    </div>
  );
};
