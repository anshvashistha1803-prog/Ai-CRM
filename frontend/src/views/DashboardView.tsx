import React from "react";
import { Users, Flame, Calendar, Building, ArrowUpRight, Plus, Sparkles } from "lucide-react";
import type { Lead, Property, SiteVisit } from "../types";

interface DashboardViewProps {
  leads: Lead[];
  properties: Property[];
  visits: SiteVisit[];
  onNavigate: (view: string) => void;
  onOpenNewLead: () => void;
}

export const DashboardView: React.FC<DashboardViewProps> = ({
  leads,
  properties,
  visits,
  onNavigate,
  onOpenNewLead,
}) => {
  const hotLeads = leads.filter(
    (l) => l.lead_health === "HOT" || (l.intent_score !== undefined && l.intent_score !== null && l.intent_score >= 70)
  );
  const activeVisits = visits.filter((v) => v.status === "SCHEDULED" || v.status === "CONFIRMED");
  const availableProps = properties.filter((p) => p.status === "AVAILABLE");

  const stageCounts: Record<string, number> = {};
  leads.forEach((l) => {
    stageCounts[l.stage] = (stageCounts[l.stage] || 0) + 1;
  });

  const [mountTime] = React.useState(() => Date.now());
  const { newThisWeek, nextVisitText } = React.useMemo(() => {
    const sevenDaysAgo = mountTime - 7 * 24 * 60 * 60 * 1000;
    const countNew = leads.filter((l) => {
      const created = new Date(l.created_at).getTime();
      return !isNaN(created) && created >= sevenDaysAgo;
    }).length;

    const upcomingVisits = activeVisits
      .map((v) => new Date(v.scheduled_start_at))
      .filter((d) => !isNaN(d.getTime()) && d.getTime() >= mountTime)
      .sort((a, b) => a.getTime() - b.getTime());

    const nextText = upcomingVisits.length > 0
      ? `Next: ${upcomingVisits[0].toLocaleDateString(undefined, { month: "short", day: "numeric" })}`
      : (activeVisits.length > 0 ? "Dates pending" : "No visits scheduled");

    return { newThisWeek: countNew, nextVisitText: nextText };
  }, [leads, activeVisits, mountTime]);

  const cards = [
    {
      title: "Total Leads",
      value: leads.length,
      icon: Users,
      color: "var(--accent-primary)",
      trend: `${newThisWeek} new in last 7 days`,
    },
    {
      title: "Hot Prospects",
      value: hotLeads.length,
      icon: Flame,
      color: "#f59e0b",
      trend: leads.length > 0
        ? `${Math.round((hotLeads.length / leads.length) * 100)}% of pipeline`
        : "None identified",
    },
    {
      title: "Site Visits Scheduled",
      value: activeVisits.length,
      icon: Calendar,
      color: "var(--color-info)",
      trend: nextVisitText,
    },
    {
      title: "Available Properties",
      value: availableProps.length,
      icon: Building,
      color: "var(--color-success)",
      trend: `${availableProps.length} of ${properties.length} active`,
    },
  ];

  return (
    <div>
      {/* Header with Quick Actions */}
      <div className="page-header">
        <div>
          <h1 className="page-title">Executive CRM Overview</h1>
          <p className="page-subtitle">Real-time pipeline metrics, automated scoring, and scheduled site visits</p>
        </div>
        <div style={{ display: "flex", gap: "10px" }}>
          <button className="btn btn-secondary" onClick={() => onNavigate("proposals")}>
            <Sparkles size={16} color="var(--accent-primary)" />
            AI Review Queue
          </button>
          <button className="btn btn-primary" onClick={onOpenNewLead}>
            <Plus size={16} />
            New Prospective Lead
          </button>
        </div>
      </div>

      {/* KPI Stats Grid */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
          gap: "20px",
          marginBottom: "32px",
        }}
      >
        {cards.map((card, idx) => {
          const Icon = card.icon;
          return (
            <div key={idx} className="glass-card" style={{ padding: "20px" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
                <span style={{ fontSize: "0.8125rem", color: "var(--text-secondary)", fontWeight: 500 }}>
                  {card.title}
                </span>
                <div
                  style={{
                    width: "36px",
                    height: "36px",
                    borderRadius: "8px",
                    backgroundColor: "rgba(255, 255, 255, 0.05)",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                  }}
                >
                  <Icon size={18} color={card.color} />
                </div>
              </div>
              <div style={{ fontSize: "2rem", fontWeight: 700, margin: "10px 0 4px" }}>
                {card.value}
              </div>
              <div style={{ fontSize: "0.75rem", color: "var(--text-muted)", display: "flex", alignItems: "center", gap: "4px" }}>
                <span>{card.trend}</span>
              </div>
            </div>
          );
        })}
      </div>

      {/* Two Column Section: Pipeline Funnel + Urgent Leads */}
      <div style={{ display: "grid", gridTemplateColumns: "1.2fr 1fr", gap: "24px" }}>
        {/* Pipeline Stage Distribution */}
        <div className="glass-card" style={{ padding: "24px" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "20px" }}>
            <h3 style={{ fontSize: "1.1rem" }}>Pipeline Stages</h3>
            <button
              className="btn btn-secondary"
              style={{ fontSize: "0.75rem", padding: "4px 8px" }}
              onClick={() => onNavigate("leads")}
            >
              View Board <ArrowUpRight size={14} />
            </button>
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: "14px" }}>
            {[
              { stage: "NEW", label: "New Inbound Leads", color: "#6366f1" },
              { stage: "QUALIFYING", label: "AI Qualifying", color: "#8b5cf6" },
              { stage: "QUALIFIED", label: "Ready / Qualified", color: "#06b6d4" },
              { stage: "SITE_VISIT_SCHEDULED", label: "Site Visit Scheduled", color: "#3b82f6" },
              { stage: "NEGOTIATION", label: "In Negotiation", color: "#f59e0b" },
              { stage: "WON", label: "Closed / Won", color: "#10b981" },
            ].map((s) => {
              const count = stageCounts[s.stage] || 0;
              const pct = leads.length > 0 ? Math.round((count / leads.length) * 100) : 0;
              return (
                <div key={s.stage}>
                  <div style={{ display: "flex", justifyContent: "space-between", fontSize: "0.8125rem", marginBottom: "5px" }}>
                    <span style={{ color: "var(--text-secondary)" }}>{s.label}</span>
                    <span style={{ fontWeight: 600 }}>{count} ({pct}%)</span>
                  </div>
                  <div style={{ height: "6px", width: "100%", backgroundColor: "rgba(255, 255, 255, 0.06)", borderRadius: "4px", overflow: "hidden" }}>
                    <div
                      style={{
                        height: "100%",
                        width: `${pct}%`,
                        backgroundColor: s.color,
                        borderRadius: "4px",
                        transition: "width 0.4s ease",
                      }}
                    />
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        {/* Hot Leads / High Intent Table */}
        <div className="glass-card" style={{ padding: "24px" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "20px" }}>
            <h3 style={{ fontSize: "1.1rem" }}>High-Intent Prospects</h3>
            <span className="badge badge-hot">{hotLeads.length} Hot</span>
          </div>

          {hotLeads.length === 0 ? (
            <div style={{ padding: "32px", textAlign: "center", color: "var(--text-muted)", fontSize: "0.875rem" }}>
              No hot leads identified yet. Complete AI qualifications to generate intent scores.
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: "10px" }}>
              {hotLeads.slice(0, 5).map((lead) => (
                <div
                  key={lead.id}
                  style={{
                    padding: "12px 14px",
                    borderRadius: "var(--radius-md)",
                    backgroundColor: "rgba(255, 255, 255, 0.02)",
                    border: "1px solid var(--border-subtle)",
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                  }}
                >
                  <div>
                    <div style={{ fontWeight: 600, fontSize: "0.875rem" }}>{lead.full_name}</div>
                    <div style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>
                      {lead.preferred_location || "Location not set"} • {lead.bhk ? `${lead.bhk} BHK` : "Any"}
                    </div>
                  </div>
                  <div style={{ textAlign: "right" }}>
                    <span className="badge badge-hot">
                      {lead.intent_score !== undefined && lead.intent_score !== null
                        ? `Score: ${lead.intent_score}`
                        : "Score: N/A"}
                    </span>
                    <div style={{ fontSize: "0.7rem", color: "var(--text-muted)", marginTop: "2px" }}>
                      {lead.stage}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
};
