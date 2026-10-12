import React, { useState } from "react";
import { Plus, Clock, X } from "lucide-react";
import { api } from "../api";
import type { Lead, Property, SiteVisit, UserSummary } from "../types";

interface VisitsViewProps {
  visits: SiteVisit[];
  leads: Lead[];
  properties: Property[];
  teamUsers: UserSummary[];
  onRefresh: () => void;
}

export const VisitsView: React.FC<VisitsViewProps> = ({
  visits,
  leads,
  properties,
  teamUsers,
  onRefresh,
}) => {
  const [openBookModal, setOpenBookModal] = useState(false);
  const [rescheduleVisitId, setRescheduleVisitId] = useState<string | null>(null);
  const [cancelVisitId, setCancelVisitId] = useState<string | null>(null);

  // Form states for booking
  const [leadId, setLeadId] = useState(leads[0]?.id || "");
  const [propertyId, setPropertyId] = useState(properties[0]?.id || "");
  const [agentId, setAgentId] = useState("");
  const [startDate, setStartDate] = useState("");
  const [startTime, setStartTime] = useState("14:00");
  const [durationHours, setDurationHours] = useState("1");
  const [notes, setNotes] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  // Form state for reschedule
  const [newDate, setNewDate] = useState("");
  const [newTime, setNewTime] = useState("15:00");

  // Form state for cancellation
  const [cancelReason, setCancelReason] = useState("");

  const handleBookSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);

    try {
      const startIso = new Date(`${startDate}T${startTime}:00`).toISOString();
      const endTimestamp = new Date(`${startDate}T${startTime}:00`).getTime() + parseInt(durationHours, 10) * 3600000;
      const endIso = new Date(endTimestamp).toISOString();

      await api.scheduleVisit({
        lead_id: leadId,
        property_id: propertyId,
        assigned_agent_id: agentId || undefined,
        scheduled_start_at: startIso,
        scheduled_end_at: endIso,
        notes: notes || undefined,
      });

      setOpenBookModal(false);
      onRefresh();
    } catch (err: any) {
      setError(err.message || "Failed to schedule site visit.");
    } finally {
      setLoading(false);
    }
  };

  const handleRescheduleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!rescheduleVisitId) return;

    try {
      const startIso = new Date(`${newDate}T${newTime}:00`).toISOString();
      const endIso = new Date(new Date(`${newDate}T${newTime}:00`).getTime() + 3600000).toISOString();

      await api.rescheduleVisit(rescheduleVisitId, startIso, endIso);
      setRescheduleVisitId(null);
      onRefresh();
    } catch (err: any) {
      alert("Reschedule failed: " + err.message);
    }
  };

  const handleCancelSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!cancelVisitId) return;

    try {
      await api.cancelVisit(cancelVisitId, cancelReason || "Client requested cancellation");
      setCancelVisitId(null);
      setCancelReason("");
      onRefresh();
    } catch (err: any) {
      alert("Cancellation failed: " + err.message);
    }
  };

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Site Visits & Showings</h1>
          <p className="page-subtitle">Conflict-checked property visit bookings, client confirmations, and calendar management</p>
        </div>
        <button className="btn btn-primary" onClick={() => setOpenBookModal(true)}>
          <Plus size={16} /> Schedule Visit
        </button>
      </div>

      {/* Visits List */}
      <div className="glass-card" style={{ overflow: "hidden" }}>
        <table className="data-table">
          <thead>
            <tr>
              <th>Lead & Property</th>
              <th>Date & Time</th>
              <th>Assigned Agent</th>
              <th>Status</th>
              <th>Notes / Reason</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {visits.length === 0 ? (
              <tr>
                <td colSpan={6} style={{ textAlign: "center", padding: "40px", color: "var(--text-muted)" }}>
                  No scheduled site visits found. Click "Schedule Visit" to book a client showing.
                </td>
              </tr>
            ) : (
              visits.map((v) => {
                const lead = leads.find((l) => l.id === v.lead_id);
                const prop = properties.find((p) => p.id === v.property_id);
                const agent = teamUsers.find((u) => u.id === v.assigned_agent_id);
                const isActive = v.status === "SCHEDULED" || v.status === "CONFIRMED";

                return (
                  <tr key={v.id}>
                    <td>
                      <div style={{ fontWeight: 600 }}>{lead ? lead.full_name : "Lead"}</div>
                      <div style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>
                        {prop ? prop.title : "Property"}
                      </div>
                    </td>
                    <td>
                      <div style={{ display: "flex", alignItems: "center", gap: "6px", fontSize: "0.8125rem" }}>
                        <Clock size={14} color="var(--text-muted)" />
                        <span>{new Date(v.scheduled_start_at).toLocaleString()}</span>
                      </div>
                    </td>
                    <td>
                      <div style={{ fontSize: "0.8125rem" }}>
                        {agent ? agent.full_name : "Unassigned"}
                      </div>
                    </td>
                    <td>
                      <span
                        className="badge"
                        style={{
                          backgroundColor:
                            v.status === "SCHEDULED"
                              ? "rgba(99, 102, 241, 0.15)"
                              : v.status === "COMPLETED"
                              ? "rgba(16, 185, 129, 0.15)"
                              : "rgba(239, 68, 68, 0.15)",
                          color:
                            v.status === "SCHEDULED"
                              ? "#a5b4fc"
                              : v.status === "COMPLETED"
                              ? "#34d399"
                              : "#f87171",
                        }}
                      >
                        {v.status}
                      </span>
                    </td>
                    <td>
                      <div style={{ fontSize: "0.75rem", color: "var(--text-muted)", maxWidth: "200px" }}>
                        {v.cancellation_reason || v.notes || "—"}
                      </div>
                    </td>
                    <td>
                      {isActive && (
                        <div style={{ display: "flex", gap: "6px" }}>
                          <button
                            className="btn btn-secondary"
                            style={{ fontSize: "0.75rem", padding: "4px 8px" }}
                            onClick={() => setRescheduleVisitId(v.id)}
                          >
                            Reschedule
                          </button>
                          <button
                            className="btn btn-danger"
                            style={{ fontSize: "0.75rem", padding: "4px 8px" }}
                            onClick={() => setCancelVisitId(v.id)}
                          >
                            Cancel
                          </button>
                        </div>
                      )}
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      {/* Book Visit Modal */}
      {openBookModal && (
        <div className="modal-backdrop">
          <div className="modal-content" style={{ maxWidth: "500px" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "20px" }}>
              <h3 style={{ fontSize: "1.25rem" }}>Schedule Site Visit</h3>
              <button onClick={() => setOpenBookModal(false)} style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer" }}>
                <X size={18} />
              </button>
            </div>

            {error && (
              <div style={{ padding: "8px 12px", backgroundColor: "rgba(239, 68, 68, 0.15)", color: "#f87171", borderRadius: "6px", fontSize: "0.8125rem", marginBottom: "14px" }}>
                {error}
              </div>
            )}

            <form onSubmit={handleBookSubmit} style={{ display: "flex", flexDirection: "column", gap: "14px" }}>
              <div>
                <label style={{ fontSize: "0.75rem", color: "var(--text-secondary)", display: "block", marginBottom: "4px" }}>Prospective Lead *</label>
                <select className="form-control" value={leadId} onChange={(e) => setLeadId(e.target.value)} required>
                  {leads.map((l) => (
                    <option key={l.id} value={l.id}>
                      {l.full_name} ({l.stage})
                    </option>
                  ))}
                </select>
              </div>

              <div>
                <label style={{ fontSize: "0.75rem", color: "var(--text-secondary)", display: "block", marginBottom: "4px" }}>Property to Tour *</label>
                <select className="form-control" value={propertyId} onChange={(e) => setPropertyId(e.target.value)} required>
                  {properties.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.title} — ₹{(p.price / 10000000).toFixed(2)} Cr
                    </option>
                  ))}
                </select>
              </div>

              <div>
                <label style={{ fontSize: "0.75rem", color: "var(--text-secondary)", display: "block", marginBottom: "4px" }}>Tour Agent</label>
                <select className="form-control" value={agentId} onChange={(e) => setAgentId(e.target.value)}>
                  <option value="">Auto-assign (Current User / Lead Agent)</option>
                  {teamUsers.map((u) => (
                    <option key={u.id} value={u.id}>
                      {u.full_name} ({u.role})
                    </option>
                  ))}
                </select>
              </div>

              <div style={{ display: "grid", gridTemplateColumns: "1.2fr 1fr 1fr", gap: "12px" }}>
                <div>
                  <label style={{ fontSize: "0.75rem", color: "var(--text-secondary)", display: "block", marginBottom: "4px" }}>Visit Date *</label>
                  <input required type="date" className="form-control" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
                </div>
                <div>
                  <label style={{ fontSize: "0.75rem", color: "var(--text-secondary)", display: "block", marginBottom: "4px" }}>Time *</label>
                  <input required type="time" className="form-control" value={startTime} onChange={(e) => setStartTime(e.target.value)} />
                </div>
                <div>
                  <label style={{ fontSize: "0.75rem", color: "var(--text-secondary)", display: "block", marginBottom: "4px" }}>Duration</label>
                  <select className="form-control" value={durationHours} onChange={(e) => setDurationHours(e.target.value)}>
                    <option value="1">1 Hour</option>
                    <option value="2">2 Hours</option>
                    <option value="3">3 Hours</option>
                  </select>
                </div>
              </div>

              <div>
                <label style={{ fontSize: "0.75rem", color: "var(--text-secondary)", display: "block", marginBottom: "4px" }}>Notes</label>
                <input type="text" className="form-control" placeholder="Special access instructions, client gates" value={notes} onChange={(e) => setNotes(e.target.value)} />
              </div>

              <button type="submit" className="btn btn-primary" style={{ height: "42px", marginTop: "8px" }} disabled={loading}>
                {loading ? "Checking conflicts..." : "Confirm & Book Visit"}
              </button>
            </form>
          </div>
        </div>
      )}

      {/* Reschedule Modal */}
      {rescheduleVisitId && (
        <div className="modal-backdrop">
          <div className="modal-content" style={{ maxWidth: "420px" }}>
            <h3 style={{ fontSize: "1.2rem", marginBottom: "16px" }}>Reschedule Site Visit</h3>
            <form onSubmit={handleRescheduleSubmit} style={{ display: "flex", flexDirection: "column", gap: "14px" }}>
              <div>
                <label style={{ fontSize: "0.75rem", color: "var(--text-secondary)", display: "block", marginBottom: "4px" }}>New Date *</label>
                <input required type="date" className="form-control" value={newDate} onChange={(e) => setNewDate(e.target.value)} />
              </div>
              <div>
                <label style={{ fontSize: "0.75rem", color: "var(--text-secondary)", display: "block", marginBottom: "4px" }}>New Time *</label>
                <input required type="time" className="form-control" value={newTime} onChange={(e) => setNewTime(e.target.value)} />
              </div>
              <div style={{ display: "flex", gap: "10px", marginTop: "10px" }}>
                <button type="button" className="btn btn-secondary" onClick={() => setRescheduleVisitId(null)}>Cancel</button>
                <button type="submit" className="btn btn-primary" style={{ flex: 1 }}>Confirm Reschedule</button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Cancel Modal */}
      {cancelVisitId && (
        <div className="modal-backdrop">
          <div className="modal-content" style={{ maxWidth: "420px" }}>
            <h3 style={{ fontSize: "1.2rem", marginBottom: "16px" }}>Cancel Site Visit</h3>
            <form onSubmit={handleCancelSubmit} style={{ display: "flex", flexDirection: "column", gap: "14px" }}>
              <div>
                <label style={{ fontSize: "0.75rem", color: "var(--text-secondary)", display: "block", marginBottom: "4px" }}>Reason for Cancellation *</label>
                <textarea required rows={3} className="form-control" placeholder="e.g. Client had conflicting travel plans" value={cancelReason} onChange={(e) => setCancelReason(e.target.value)} />
              </div>
              <div style={{ display: "flex", gap: "10px", marginTop: "10px" }}>
                <button type="button" className="btn btn-secondary" onClick={() => setCancelVisitId(null)}>Keep Booking</button>
                <button type="submit" className="btn btn-danger" style={{ flex: 1 }}>Confirm Cancellation</button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
};
