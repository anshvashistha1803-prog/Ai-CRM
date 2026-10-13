import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";

describe("Frontend API Client — Runtime Contract Verification", () => {
  const originalFetch = global.fetch;

  beforeEach(() => {
    localStorage.clear();
    api.setToken(null);
  });

  afterEach(() => {
    global.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it("constructs API URL using configured base without trailing slash", async () => {
    let capturedUrl = "";
    global.fetch = vi.fn().mockImplementation(async (url: string) => {
      capturedUrl = url;
      return new Response(JSON.stringify({ status: "healthy" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });

    await api.getCurrentUser();
    // Default base is /api/v1 (or VITE_API_BASE if specified)
    expect(capturedUrl).toMatch(/^(\/api\/v1|https?:\/\/.*\/api\/v1)?\/auth\/me$/);
  });

  it("transitionLead calls the exact backend route /leads/:id/stage with POST", async () => {
    let capturedUrl = "";
    let capturedMethod = "";
    let capturedBody = "";
    let capturedHeaders: Record<string, string> = {};

    api.setToken("test-jwt-token-12345");

    global.fetch = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      capturedUrl = url;
      capturedMethod = init?.method || "GET";
      capturedBody = (init?.body as string) || "";
      capturedHeaders = (init?.headers as Record<string, string>) || {};
      return new Response(
        JSON.stringify({
          id: "lead-1",
          full_name: "Test Lead",
          stage: "QUALIFIED",
        }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }
      );
    });

    const leadId = "11111111-2222-3333-4444-555555555555";
    const res = await api.transitionLead(leadId, "QUALIFIED", "Documented verification");

    expect(capturedUrl).toContain(`/leads/${leadId}/stage`);
    expect(capturedMethod).toBe("POST");
    expect(capturedHeaders["Authorization"]).toBe("Bearer test-jwt-token-12345");

    const parsedBody = JSON.parse(capturedBody);
    expect(parsedBody).toEqual({
      target_stage: "QUALIFIED",
      reason: "Documented verification",
    });
    expect(res.stage).toBe("QUALIFIED");
  });

  it("extracts and propagates backend error messages on API failure", async () => {
    global.fetch = vi.fn().mockImplementation(async () => {
      return new Response(
        JSON.stringify({
          error: "INVALID_TRANSITION",
          message: "Transition from QUALIFYING to WON is not permitted.",
        }),
        {
          status: 422,
          headers: { "Content-Type": "application/json" },
        }
      );
    });

    await expect(
      api.transitionLead("lead-1", "WON", "premature")
    ).rejects.toThrow("Transition from QUALIFYING to WON is not permitted.");
  });

  it("handles duplicate lead 409 Conflict with meaningful message", async () => {
    global.fetch = vi.fn().mockImplementation(async () => {
      return new Response(
        JSON.stringify({
          error: "DUPLICATE_LEAD",
          message: "Another lead already has this contact information.",
        }),
        {
          status: 409,
          headers: { "Content-Type": "application/json" },
        }
      );
    });

    await expect(
      api.createLead({ full_name: "Dup Lead", phone: "+919876543210" })
    ).rejects.toThrow("Another lead already has this contact information.");
  });

  it("calls /integrations/google/authorize and returns authorization_url", async () => {
    let capturedUrl = "";
    api.setToken("manager-token");

    global.fetch = vi.fn().mockImplementation(async (url: string) => {
      capturedUrl = url;
      return new Response(
        JSON.stringify({
          authorization_url: "https://accounts.google.com/o/oauth2/v2/auth?client_id=test",
          state: "test-state-token",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      );
    });

    const res = await api.getGoogleAuthorizeUrl();
    expect(capturedUrl).toContain("/integrations/google/authorize");
    expect(res.authorization_url).toContain("accounts.google.com");
    expect(res.state).toBe("test-state-token");
  });

  it("calls /integrations/google/status and parses response", async () => {
    let capturedUrl = "";
    api.setToken("manager-token");

    global.fetch = vi.fn().mockImplementation(async (url: string) => {
      capturedUrl = url;
      return new Response(
        JSON.stringify({
          is_connected: true,
          google_email: "realestate@havn.com",
          spreadsheet_id: "test-sheet-id",
          properties_sheet_name: "Properties",
          scopes: ["https://www.googleapis.com/auth/spreadsheets"],
          sync_mode: "MERGE_VALIDATED",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      );
    });

    const res = await api.getGoogleStatus();
    expect(capturedUrl).toContain("/integrations/google/status");
    expect(res.is_connected).toBe(true);
    expect(res.google_email).toBe("realestate@havn.com");
  });

  it("calls /integrations/google/sync/inventory with POST", async () => {
    let capturedUrl = "";
    let capturedMethod = "";
    api.setToken("manager-token");

    global.fetch = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      capturedUrl = url;
      capturedMethod = init?.method || "GET";
      return new Response(
        JSON.stringify({
          status: "SUCCESS",
          spreadsheet_id: "test-sheet-id",
          sheet_name: "Properties",
          total_rows: 5,
          created: 3,
          updated: 2,
          staged: 0,
          duplicate_suspects: 0,
          skipped: 0,
          error_count: 0,
          errors: [],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      );
    });

    const res = await api.syncGoogleInventory();
    expect(capturedUrl).toContain("/integrations/google/sync/inventory");
    expect(capturedMethod).toBe("POST");
    expect(res.status).toBe("SUCCESS");
    expect(res.created).toBe(3);
  });
});
