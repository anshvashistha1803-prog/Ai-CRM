import { useState } from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { LeadsView } from "../views/LeadsView";
import { maskPhone } from "../utils/phone";
import { api } from "../api";
import type { Lead, UserSummary } from "../types";

describe("LeadsView & Lead Management Workflow Regression Tests", () => {
  const mockUser: UserSummary = {
    id: "user-1",
    email: "manager@havnrealty.com",
    full_name: "Operations Lead",
    role: "MANAGER",
    tenant_id: "tenant-1",
    is_active: true,
  };

  const mockLeads: Lead[] = [
    {
      id: "lead-1",
      tenant_id: "tenant-1",
      full_name: "Aarav Patel",
      phone: "+919876543210",
      phone_masked: "+91••••10",
      email: "aarav@example.com",
      preferred_contact: "WHATSAPP",
      source: "WEBSITE",
      stage: "QUALIFIED",
      lead_health: "WARM",
      intent_score: 50,
      automation_status: "COMPLETED",
      do_not_contact: false,
      bhk: 3,
      property_type: "APARTMENT",
      preferred_location: "Whitefield",
      budget_max: 15000000,
      currency_code: "INR",
      created_at: "2026-10-11T11:10:50Z",
      updated_at: "2026-10-11T11:10:50Z",
    },
    {
      id: "lead-2",
      tenant_id: "tenant-1",
      full_name: "Aarav Patel Legacy",
      phone: undefined,
      phone_masked: undefined,
      email: undefined,
      preferred_contact: "PHONE",
      source: "MANUAL",
      stage: "QUALIFIED",
      lead_health: "UNKNOWN",
      intent_score: undefined,
      automation_status: "PENDING",
      do_not_contact: false,
      bhk: 2,
      property_type: "VILLA",
      preferred_location: "Indiranagar",
      budget_max: 25000000,
      currency_code: "INR",
      created_at: "2026-10-11T11:09:43Z",
      updated_at: "2026-10-11T11:09:43Z",
    },
    {
      id: "lead-3",
      tenant_id: "tenant-1",
      full_name: "Priya Sharma",
      phone: "+919123456789",
      preferred_contact: "PHONE",
      source: "PORTAL",
      stage: "NEW",
      lead_health: "HOT",
      intent_score: 85,
      automation_status: "IN_PROGRESS",
      do_not_contact: false,
      bhk: 4,
      property_type: "PENTHOUSE",
      preferred_location: "Koramangala",
      budget_max: 45000000,
      currency_code: "INR",
      created_at: "2026-10-12T09:00:00Z",
      updated_at: "2026-10-12T09:00:00Z",
    },
  ];

  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  // Test Harness for interactive state tests
  const InteractiveLeadsHarness = ({ initialLeads = mockLeads }: { initialLeads?: Lead[] }) => {
    const [openModal, setOpenModal] = useState(false);
    const [currentLeads, setCurrentLeads] = useState<Lead[]>(initialLeads);

    return (
      <LeadsView
        leads={currentLeads}
        teamUsers={[mockUser]}
        currentUser={mockUser}
        onRefresh={() => setCurrentLeads(currentLeads)}
        openCreateModal={openModal}
        onOpenCreateModal={() => setOpenModal(true)}
        onCloseCreateModal={() => setOpenModal(false)}
      />
    );
  };

  describe("maskPhone utility unit tests", () => {
    it("returns 'No phone' for undefined, null, or empty string", () => {
      expect(maskPhone(undefined)).toBe("No phone");
      expect(maskPhone(null)).toBe("No phone");
      expect(maskPhone("")).toBe("No phone");
      expect(maskPhone("   ")).toBe("No phone");
    });

    it("returns pre-masked phone if provided", () => {
      expect(maskPhone("+919876543210", "+91****10")).toBe("+91****10");
    });

    it("masks middle digits of E.164 phone numbers preserving prefix and last 2 digits", () => {
      const masked = maskPhone("+919876543210");
      expect(masked).toBe("+91••••10");
      expect(masked).not.toContain("98765432");
    });

    it("masks short numbers gracefully without crashing", () => {
      expect(maskPhone("123")).toBe("••••");
      expect(maskPhone("12345")).toBe("12••••45");
    });
  });

  describe("Lead Creation Modal Workflow", () => {
    it("clicking 'Add Prospective Lead' opens the create modal", () => {
      const onOpenMock = vi.fn();
      render(
        <LeadsView
          leads={mockLeads}
          teamUsers={[mockUser]}
          currentUser={mockUser}
          onRefresh={vi.fn()}
          openCreateModal={false}
          onOpenCreateModal={onOpenMock}
          onCloseCreateModal={vi.fn()}
        />
      );

      const addBtn = screen.getByRole("button", { name: /Add Prospective Lead/i });
      fireEvent.click(addBtn);

      expect(onOpenMock).toHaveBeenCalledTimes(1);
    });

    it("renders modal when openCreateModal is true and closes when close button is clicked", () => {
      render(<InteractiveLeadsHarness />);

      // Initially modal is not visible
      expect(screen.queryByTestId("create-lead-modal")).toBeNull();

      // Click Add Prospective Lead button
      const addBtn = screen.getByRole("button", { name: /Add Prospective Lead/i });
      fireEvent.click(addBtn);

      // Modal is now visible
      expect(screen.getByTestId("create-lead-modal")).toBeDefined();
      expect(screen.getByRole("heading", { name: "Add Prospective Lead" })).toBeDefined();

      // Click close button
      const closeBtn = screen.getByTestId("close-create-modal");
      fireEvent.click(closeBtn);

      // Modal is closed
      expect(screen.queryByTestId("create-lead-modal")).toBeNull();
    });

    it("validates required fields including phone before submitting", async () => {
      const createSpy = vi.spyOn(api, "createLead").mockResolvedValue({} as any);

      render(
        <LeadsView
          leads={mockLeads}
          teamUsers={[mockUser]}
          currentUser={mockUser}
          onRefresh={vi.fn()}
          openCreateModal={true}
          onOpenCreateModal={vi.fn()}
          onCloseCreateModal={vi.fn()}
        />
      );

      const phoneInput = screen.getByLabelText(/Phone Number \(E\.164\)/i);
      const nameInput = screen.getByLabelText(/Full Name/i);

      // Verify HTML5 required attributes
      expect(nameInput.hasAttribute("required")).toBe(true);
      expect(phoneInput.hasAttribute("required")).toBe(true);

      // Try submitting without phone
      fireEvent.change(nameInput, { target: { value: "Rohit Verma" } });
      fireEvent.change(phoneInput, { target: { value: "   " } });

      const submitBtn = screen.getByRole("button", { name: /Save Prospective Lead/i });
      fireEvent.click(submitBtn);

      // Validation error displayed
      await waitFor(() => {
        expect(screen.getByText(/Phone number is required/i)).toBeDefined();
      });

      // Backend API must NOT have been called
      expect(createSpy).not.toHaveBeenCalled();
    });

    it("submits the form and calls api.createLead with phone and entered values", async () => {
      const onRefreshMock = vi.fn();
      const onCloseMock = vi.fn();

      const createdLead: Lead = {
        id: "lead-new-1",
        tenant_id: "tenant-1",
        full_name: "Sneha Reddy",
        phone: "+919876500000",
        email: "sneha@example.com",
        preferred_contact: "WHATSAPP",
        source: "MANUAL",
        stage: "NEW",
        lead_health: "HOT",
        automation_status: "IN_PROGRESS",
        do_not_contact: false,
        budget_max: 20000000,
        bhk: 3,
        property_type: "APARTMENT",
        preferred_location: "Sarjapur Road",
        currency_code: "INR",
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      };

      const createSpy = vi.spyOn(api, "createLead").mockResolvedValue(createdLead);

      render(
        <LeadsView
          leads={mockLeads}
          teamUsers={[mockUser]}
          currentUser={mockUser}
          onRefresh={onRefreshMock}
          openCreateModal={true}
          onOpenCreateModal={vi.fn()}
          onCloseCreateModal={onCloseMock}
        />
      );

      fireEvent.change(screen.getByLabelText(/Full Name/i), { target: { value: "Sneha Reddy" } });
      fireEvent.change(screen.getByLabelText(/Phone Number \(E\.164\)/i), { target: { value: "+919876500000" } });
      fireEvent.change(screen.getByLabelText(/Email Address/i), { target: { value: "sneha@example.com" } });
      fireEvent.change(screen.getByLabelText(/Budget Max/i), { target: { value: "20000000" } });
      fireEvent.change(screen.getByLabelText(/Preferred Location/i), { target: { value: "Sarjapur Road" } });

      const submitBtn = screen.getByRole("button", { name: /Save Prospective Lead/i });
      fireEvent.click(submitBtn);

      await waitFor(() => {
        expect(createSpy).toHaveBeenCalledWith({
          full_name: "Sneha Reddy",
          phone: "+919876500000",
          email: "sneha@example.com",
          budget_max: 20000000,
          bhk: 3,
          property_type: "APARTMENT",
          preferred_location: "Sarjapur Road",
        });
        expect(onCloseMock).toHaveBeenCalled();
        expect(onRefreshMock).toHaveBeenCalled();
      });
    });
  });

  describe("Contact Information Rendering & Privacy", () => {
    it("displays masked phone in table row and 'No phone' for record without phone", () => {
      render(
        <LeadsView
          leads={mockLeads}
          teamUsers={[mockUser]}
          currentUser={mockUser}
          onRefresh={vi.fn()}
          openCreateModal={false}
          onOpenCreateModal={vi.fn()}
          onCloseCreateModal={vi.fn()}
        />
      );

      // Lead 1: masked phone displayed
      expect(screen.getByText(/\+91••••10/)).toBeDefined();
      // Ensure raw phone is not printed in table
      expect(screen.queryByText("+919876543210")).toBeNull();

      // Lead 2: legacy record without phone displays 'No phone'
      expect(screen.getByText(/No phone/)).toBeDefined();

      // Lead 3: masked phone generated dynamically
      expect(screen.getByText(/\+91••••89/)).toBeDefined();
      expect(screen.queryByText("+919123456789")).toBeNull();
    });

    it("displays contact info in drawer only when available without printing 'undefined' or 'null'", () => {
      render(
        <LeadsView
          leads={mockLeads}
          teamUsers={[mockUser]}
          currentUser={mockUser}
          onRefresh={vi.fn()}
          openCreateModal={false}
          onOpenCreateModal={vi.fn()}
          onCloseCreateModal={vi.fn()}
        />
      );

      // Click Lead 1 (has phone and email)
      const lead1Row = screen.getByText("Aarav Patel");
      fireEvent.click(lead1Row);

      // Drawer is open
      const phoneElem = screen.getByTestId("lead-drawer-phone");
      expect(phoneElem.textContent).toContain("+919876543210");
      const emailElem = screen.getByTestId("lead-drawer-email");
      expect(emailElem.textContent).toContain("aarav@example.com");

      // Verify no automated WhatsApp message link was injected
      const whatsappLinks = document.querySelectorAll('a[href*="wa.me"], a[href*="whatsapp.com"]');
      expect(whatsappLinks.length).toBe(0);

      // Close drawer
      fireEvent.click(screen.getByLabelText("Close lead drawer"));

      // Click Lead 2 (missing phone and email)
      const lead2Row = screen.getByText("Aarav Patel Legacy");
      fireEvent.click(lead2Row);

      // In drawer, phone and email testids should not exist
      expect(screen.queryByTestId("lead-drawer-phone")).toBeNull();
      expect(screen.queryByTestId("lead-drawer-email")).toBeNull();

      // Literal 'undefined' or 'null' must NOT be rendered
      expect(screen.queryByText(/undefined/i)).toBeNull();
      expect(screen.queryByText(/null/i)).toBeNull();
    });

    it("filters lead list by pipeline stage and search term", () => {
      render(
        <LeadsView
          leads={mockLeads}
          teamUsers={[mockUser]}
          currentUser={mockUser}
          onRefresh={vi.fn()}
          openCreateModal={false}
          onOpenCreateModal={vi.fn()}
          onCloseCreateModal={vi.fn()}
        />
      );

      // Initially all 3 leads are rendered
      expect(screen.getByText("Aarav Patel")).toBeDefined();
      expect(screen.getByText("Aarav Patel Legacy")).toBeDefined();
      expect(screen.getByText("Priya Sharma")).toBeDefined();

      // Filter by 'New' stage
      const newFilterBtn = screen.getByRole("button", { name: "New" });
      fireEvent.click(newFilterBtn);

      // Only Priya Sharma should be visible
      expect(screen.queryByText("Aarav Patel")).toBeNull();
      expect(screen.queryByText("Aarav Patel Legacy")).toBeNull();
      expect(screen.getByText("Priya Sharma")).toBeDefined();

      // Switch back to 'All'
      const allFilterBtn = screen.getByRole("button", { name: "All" });
      fireEvent.click(allFilterBtn);

      // Search by location 'Whitefield'
      const searchInput = screen.getByPlaceholderText(/Filter list\.\.\./i);
      fireEvent.change(searchInput, { target: { value: "Whitefield" } });

      // Only Aarav Patel is in Whitefield
      expect(screen.getByText("Aarav Patel")).toBeDefined();
      expect(screen.queryByText("Priya Sharma")).toBeNull();
    });
  });
});
