import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Lead, Property, SiteVisit, TenantInfo, UserSummary } from "../types";
import { DashboardView } from "../views/DashboardView";

describe("Frontend Views — Data Integrity and UI State Tests", () => {
  const mockTenant: TenantInfo = {
    id: "tenant-1",
    name: "HAVN Realty Bengaluru",
    slug: "havn-bengaluru",
    timezone: "Asia/Kolkata",
    currency_code: "INR",
  };

  const mockUser: UserSummary = {
    id: "user-1",
    email: "manager@havn.com",
    full_name: "Operations Manager",
    role: "MANAGER",
    tenant_id: "tenant-1",
  };

  const mockProperties: Property[] = [
    {
      id: "prop-1",
      title: "Sovereign 3BHK Whitefield",
      property_type: "APARTMENT",
      bhk: 3,
      price: 15000000,
      area_sqft: 1800,
      location_text: "Whitefield",
      status: "AVAILABLE",
      created_at: new Date().toISOString(),
    },
  ];

  const mockVisits: SiteVisit[] = [
    {
      id: "visit-1",
      lead_id: "lead-1",
      property_id: "prop-1",
      scheduled_start_at: new Date(Date.now() + 86400000 * 2).toISOString(),
      scheduled_end_at: new Date(Date.now() + 86400000 * 2 + 3600000).toISOString(),
      status: "SCHEDULED",
      created_at: new Date().toISOString(),
    },
  ];

  it("renders genuine intent scores without fabricated 85 fallback", () => {
    const leadsWithScores: Lead[] = [
      {
        id: "lead-1",
        full_name: "Hot Prospect One",
        phone: "+919876543210",
        stage: "QUALIFIED",
        intent_score: 92,
        lead_health: "HOT",
        preferred_location: "Whitefield",
        bhk: 3,
        created_at: new Date().toISOString(),
      },
      {
        id: "lead-2",
        full_name: "Unscored Hot Prospect",
        phone: "+919876543211",
        stage: "QUALIFIED",
        intent_score: undefined, // Notice: intent_score is undefined!
        lead_health: "HOT",
        preferred_location: "Indiranagar",
        bhk: 2,
        created_at: new Date().toISOString(),
      },
    ];

    render(
      <DashboardView
        leads={leadsWithScores}
        properties={mockProperties}
        visits={mockVisits}
        agency={mockTenant}
        user={mockUser}
        onNavigate={vi.fn()}
      />
    );

    // Lead 1: Genuine calculated score rendered
    expect(screen.getByText("Score: 92")).toBeDefined();

    // Lead 2: Honest "Score: N/A" rendered instead of previous fabricated "Score: 85" fallback!
    expect(screen.getByText("Score: N/A")).toBeDefined();
    expect(screen.queryByText("Score: 85")).toBeNull();
  });

  it("calculates real trend and counts based on actual API data", () => {
    const now = new Date();
    const fourDaysAgo = new Date(now.getTime() - 4 * 86400000);
    const tenDaysAgo = new Date(now.getTime() - 10 * 86400000);

    const leads: Lead[] = [
      {
        id: "lead-recent",
        full_name: "Recent Inbound",
        stage: "NEW",
        created_at: fourDaysAgo.toISOString(),
      },
      {
        id: "lead-older",
        full_name: "Older Inbound",
        stage: "QUALIFIED",
        created_at: tenDaysAgo.toISOString(),
      },
    ];

    render(
      <DashboardView
        leads={leads}
        properties={mockProperties}
        visits={mockVisits}
        agency={mockTenant}
        user={mockUser}
        onNavigate={vi.fn()}
      />
    );

    // KPI total leads = 2
    expect(screen.getByText("Total Leads")).toBeDefined();
    expect(screen.getByText("2")).toBeDefined();

    // Trend: Exactly 1 lead created in last 7 days ("1 new in last 7 days")
    expect(screen.getByText("1 new in last 7 days")).toBeDefined();
    // Verify static fabricated strings are gone
    expect(screen.queryByText("+14% this week")).toBeNull();
  });
});
