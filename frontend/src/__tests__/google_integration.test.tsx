import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { GoogleIntegrationView } from "../views/GoogleIntegrationView";
import { api } from "../api";
import type { GoogleStatusResponse, InventorySyncResponse, UserSummary, TenantInfo } from "../types";

describe("Google Sheets Integration View — Comprehensive UI Tests", () => {
  const mockUser: UserSummary = {
    id: "user-mgr-1",
    email: "manager@havn.com",
    full_name: "Operations Lead",
    role: "MANAGER",
    tenant_id: "tenant-1",
    is_active: true,
  };

  const mockAgency: TenantInfo = {
    id: "tenant-1",
    name: "HAVN Realty",
    slug: "havn-blr",
    timezone: "Asia/Kolkata",
    currency_code: "INR",
  };

  const disconnectedStatus: GoogleStatusResponse = {
    is_connected: false,
    connected_at: null,
    connected_by: null,
    google_email: null,
    scopes: [],
    spreadsheet_id: null,
    properties_sheet_name: "Properties",
    sync_mode: "MERGE_VALIDATED",
    last_sync_at: null,
    last_sync_status: null,
    last_sync_error: null,
    last_sync_stats: {},
  };

  const connectedStatus: GoogleStatusResponse = {
    is_connected: true,
    connected_at: "2026-10-12T10:00:00Z",
    connected_by: "user-mgr-1",
    google_email: "inventory@havnrealty.com",
    scopes: ["https://www.googleapis.com/auth/spreadsheets"],
    spreadsheet_id: "1tbSRSAkqPqU6OKlpE_NDXspnh_QbMbeXxT7ubI8Xr-Y",
    properties_sheet_name: "Properties",
    sync_mode: "MERGE_VALIDATED",
    last_sync_at: "2026-10-12T11:30:00Z",
    last_sync_status: "SUCCESS",
    last_sync_error: null,
    last_sync_stats: {
      total_rows: 15,
      created: 10,
      updated: 4,
      staged: 1,
      skipped: 0,
      errors_count: 0,
    },
  };

  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders disconnected state with Disconnected badge and Connect Google Sheets button", async () => {
    vi.spyOn(api, "getGoogleStatus").mockResolvedValueOnce(disconnectedStatus);

    render(
      <GoogleIntegrationView
        currentUser={mockUser}
        agency={mockAgency}
      />
    );

    // Initial loading or resolution
    expect(await screen.findByText("Disconnected")).toBeDefined();
    expect(screen.getByText("No account connected")).toBeDefined();
    expect(screen.getByRole("button", { name: /Connect Google Sheets/i })).toBeDefined();
  });

  it("calls getGoogleAuthorizeUrl and initiates browser redirect on Connect click", async () => {
    vi.spyOn(api, "getGoogleStatus").mockResolvedValueOnce(disconnectedStatus);
    const authSpy = vi.spyOn(api, "getGoogleAuthorizeUrl").mockResolvedValueOnce({
      authorization_url: "https://accounts.google.com/o/oauth2/v2/auth?state=signed_test_state",
      state: "signed_test_state",
    });

    // Mock window.location
    const originalLocation = window.location;
    // @ts-ignore
    delete window.location;
    window.location = { ...originalLocation, href: "" } as any;

    render(
      <GoogleIntegrationView
        currentUser={mockUser}
        agency={mockAgency}
      />
    );

    const connectBtn = await screen.findByRole("button", { name: /Connect Google Sheets/i });
    fireEvent.click(connectBtn);

    await waitFor(() => {
      expect(authSpy).toHaveBeenCalledTimes(1);
      expect(window.location.href).toBe("https://accounts.google.com/o/oauth2/v2/auth?state=signed_test_state");
    });

    window.location = originalLocation;
  });

  it("renders connected state with Connected badge, authorized email, and last sync info", async () => {
    vi.spyOn(api, "getGoogleStatus").mockResolvedValueOnce(connectedStatus);

    render(
      <GoogleIntegrationView
        currentUser={mockUser}
        agency={mockAgency}
      />
    );

    expect(await screen.findByText("Connected")).toBeDefined();
    expect(screen.getByText("inventory@havnrealty.com")).toBeDefined();
    expect(screen.getByText("SUCCESS")).toBeDefined();
    expect(screen.getByDisplayValue("1tbSRSAkqPqU6OKlpE_NDXspnh_QbMbeXxT7ubI8Xr-Y")).toBeDefined();
  });

  it("handles and displays safe error message without exposing credentials on failure", async () => {
    vi.spyOn(api, "getGoogleStatus").mockRejectedValueOnce(
      new Error("Google OAuth configuration error: GOOGLE_CLIENT_ID missing")
    );

    render(
      <GoogleIntegrationView
        currentUser={mockUser}
        agency={mockAgency}
      />
    );

    const alert = await screen.findByRole("alert");
    expect(alert).toBeDefined();
    expect(alert.textContent).toContain("Google OAuth configuration error");
    // Ensure no secrets/tokens are present
    expect(alert.textContent).not.toContain("GOCSPX-");
    expect(alert.textContent).not.toContain("secret_key");
  });

  it("executes inventory sync on button click and renders summary metrics", async () => {
    vi.spyOn(api, "getGoogleStatus")
      .mockResolvedValueOnce(connectedStatus)
      .mockResolvedValueOnce({
        ...connectedStatus,
        last_sync_status: "SUCCESS",
        last_sync_at: new Date().toISOString(),
      });

    const mockSyncResult: InventorySyncResponse = {
      status: "SUCCESS",
      spreadsheet_id: "1tbSRSAkqPqU6OKlpE_NDXspnh_QbMbeXxT7ubI8Xr-Y",
      sheet_name: "Properties",
      total_rows: 20,
      created: 14,
      updated: 5,
      staged: 1,
      duplicate_suspects: 0,
      skipped: 0,
      error_count: 0,
      errors: [],
    };

    const syncSpy = vi.spyOn(api, "syncGoogleInventory").mockResolvedValueOnce(mockSyncResult);
    const onSyncedCallback = vi.fn();

    render(
      <GoogleIntegrationView
        currentUser={mockUser}
        agency={mockAgency}
        onInventorySynced={onSyncedCallback}
      />
    );

    expect(await screen.findByText("Connected")).toBeDefined();

    const syncButtons = screen.getAllByRole("button", { name: /Sync Inventory Now/i });
    fireEvent.click(syncButtons[0]);

    await waitFor(() => {
      expect(syncSpy).toHaveBeenCalledTimes(1);
      expect(onSyncedCallback).toHaveBeenCalledTimes(1);
    });

    // Check rendered stats
    expect(await screen.findByText("Latest Sync Execution Results")).toBeDefined();
    expect(screen.getByText("Sync completed (SUCCESS): 14 created, 5 updated, 1 staged.")).toBeDefined();
  });

  it("updates spreadsheet configuration when form is submitted", async () => {
    vi.spyOn(api, "getGoogleStatus").mockResolvedValueOnce(connectedStatus);
    const updateSpy = vi.spyOn(api, "updateGoogleConfig").mockResolvedValueOnce({
      ...connectedStatus,
      spreadsheet_id: "new-custom-sheet-id-456",
      properties_sheet_name: "Units",
    });

    render(
      <GoogleIntegrationView
        currentUser={mockUser}
        agency={mockAgency}
      />
    );

    expect(await screen.findByDisplayValue("1tbSRSAkqPqU6OKlpE_NDXspnh_QbMbeXxT7ubI8Xr-Y")).toBeDefined();

    const input = screen.getByPlaceholderText(/e\.g\. 1tbSRSAkqPqU6OKlpE/i);
    fireEvent.change(input, { target: { value: "new-custom-sheet-id-456" } });

    const saveBtn = screen.getByRole("button", { name: /Save Settings/i });
    fireEvent.click(saveBtn);

    await waitFor(() => {
      expect(updateSpy).toHaveBeenCalledWith({
        spreadsheet_id: "new-custom-sheet-id-456",
        properties_sheet_name: "Properties",
      });
      expect(screen.getByText("Google Sheets configuration updated successfully.")).toBeDefined();
    });
  });
});
