import type {
  Lead,
  LeadActivity,
  LeadPropertyMatch,
  LeadQualificationProposal,
  Property,
  SiteVisit,
  TenantInfo,
  UserSummary,
} from "./types";

const API_BASE = "http://localhost:8000/api/v1";

class ApiClient {
  private token: string | null = null;

  constructor() {
    this.token = localStorage.getItem("crm_token");
  }

  setToken(token: string | null) {
    this.token = token;
    if (token) {
      localStorage.setItem("crm_token", token);
    } else {
      localStorage.removeItem("crm_token");
    }
  }

  getToken(): string | null {
    return this.token;
  }

  private async request<T>(endpoint: string, options: RequestInit = {}): Promise<T> {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      ...(options.headers as Record<string, string>),
    };

    if (this.token) {
      headers["Authorization"] = `Bearer ${this.token}`;
    }

    const response = await fetch(`${API_BASE}${endpoint}`, {
      ...options,
      headers,
    });

    if (!response.ok) {
      let errorMsg = `HTTP Error ${response.status}`;
      try {
        const errorData = await response.json();
        errorMsg = errorData.message || errorData.detail || errorMsg;
      } catch {
        // fallback
      }
      throw new Error(errorMsg);
    }

    if (response.status === 204) {
      return {} as T;
    }

    return response.json();
  }

  // ── Auth ───────────────────────────────────────────────────────────────────
  async login(email: string, password: string): Promise<{ token: { access_token: string }; user: UserSummary }> {
    const res = await this.request<{ token: { access_token: string }; user: UserSummary }>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    });
    this.setToken(res.token.access_token);
    return res;
  }

  logout() {
    this.setToken(null);
  }

  async getCurrentUser(): Promise<UserSummary> {
    return this.request<UserSummary>("/auth/me");
  }

  // ── Tenant ─────────────────────────────────────────────────────────────────
  async getAgency(): Promise<TenantInfo> {
    return this.request<TenantInfo>("/tenants/me");
  }

  // ── Leads ──────────────────────────────────────────────────────────────────
  async getLeads(query = ""): Promise<{ data: Lead[]; total: number; page: number; limit: number }> {
    return this.request<{ data: Lead[]; total: number; page: number; limit: number }>(`/leads${query}`);
  }

  async getLead(id: string): Promise<Lead> {
    return this.request<Lead>(`/leads/${id}`);
  }

  async createLead(payload: any): Promise<Lead> {
    return this.request<Lead>("/leads", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  async updateLead(id: string, payload: any): Promise<Lead> {
    return this.request<Lead>(`/leads/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
  }

  async assignLead(leadId: string, agentId: string | null): Promise<Lead> {
    return this.request<Lead>(`/leads/${leadId}/assign`, {
      method: "POST",
      body: JSON.stringify({ assigned_agent_id: agentId }),
    });
  }

  async transitionLead(leadId: string, targetStage: string, reason?: string): Promise<Lead> {
    return this.request<Lead>(`/leads/${leadId}/transition`, {
      method: "POST",
      body: JSON.stringify({ target_stage: targetStage, reason }),
    });
  }

  async getLeadActivities(leadId: string): Promise<LeadActivity[]> {
    return this.request<LeadActivity[]>(`/leads/${leadId}/activities`);
  }

  // ── Properties ─────────────────────────────────────────────────────────────
  async getProperties(query = ""): Promise<{ data: Property[]; total: number }> {
    return this.request<{ data: Property[]; total: number }>(`/properties${query}`);
  }

  async createProperty(payload: any): Promise<Property> {
    return this.request<Property>("/properties", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  // ── Matching ───────────────────────────────────────────────────────────────
  async getMatches(leadId: string): Promise<LeadPropertyMatch[]> {
    return this.request<LeadPropertyMatch[]>(`/leads/${leadId}/matches`);
  }

  async computeMatches(leadId: string, minScore = 30.0): Promise<LeadPropertyMatch[]> {
    return this.request<LeadPropertyMatch[]>(`/leads/${leadId}/matches/compute?min_score=${minScore}`, {
      method: "POST",
    });
  }

  async updateMatchStatus(leadId: string, matchId: string, status: string): Promise<LeadPropertyMatch> {
    return this.request<LeadPropertyMatch>(`/leads/${leadId}/matches/${matchId}`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    });
  }

  // ── Qualification Proposals ────────────────────────────────────────────────
  async getProposals(leadId: string): Promise<LeadQualificationProposal[]> {
    return this.request<LeadQualificationProposal[]>(`/leads/${leadId}/proposals`);
  }

  async generateProposal(leadId: string, messages?: string[]): Promise<LeadQualificationProposal> {
    return this.request<LeadQualificationProposal>(`/leads/${leadId}/proposals/generate`, {
      method: "POST",
      body: JSON.stringify({ messages }),
    });
  }

  async reviewProposal(leadId: string, proposalId: string, action: "ACCEPT" | "REJECT", review_notes?: string): Promise<LeadQualificationProposal> {
    return this.request<LeadQualificationProposal>(`/leads/${leadId}/proposals/${proposalId}/review`, {
      method: "POST",
      body: JSON.stringify({ action, review_notes }),
    });
  }

  // ── Site Visits ────────────────────────────────────────────────────────────
  async getVisits(query = ""): Promise<SiteVisit[]> {
    return this.request<SiteVisit[]>(`/visits${query}`);
  }

  async scheduleVisit(payload: any): Promise<SiteVisit> {
    return this.request<SiteVisit>("/visits", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  async rescheduleVisit(visitId: string, newStart: string, newEnd: string, notes?: string): Promise<SiteVisit> {
    return this.request<SiteVisit>(`/visits/${visitId}/reschedule`, {
      method: "POST",
      body: JSON.stringify({ new_start_at: newStart, new_end_at: newEnd, notes }),
    });
  }

  async cancelVisit(visitId: string, cancellation_reason: string): Promise<SiteVisit> {
    return this.request<SiteVisit>(`/visits/${visitId}/cancel`, {
      method: "POST",
      body: JSON.stringify({ cancellation_reason }),
    });
  }

  // ── Team Users ─────────────────────────────────────────────────────────────
  async getTeamUsers(): Promise<UserSummary[]> {
    return this.request<UserSummary[]>("/users");
  }

  async inviteUser(email: string, fullName: string, role: string): Promise<{ invitation_token: string }> {
    return this.request<{ invitation_token: string }>("/users/invite", {
      method: "POST",
      body: JSON.stringify({ email, full_name: fullName, role }),
    });
  }
}

export const api = new ApiClient();
