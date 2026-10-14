export type UserRole = "SUPER_ADMIN" | "AGENCY_ADMIN" | "MANAGER" | "AGENT";

export interface UserSummary {
  id: string;
  email: string;
  full_name: string;
  role: UserRole;
  is_active: boolean;
  tenant_id: string;
}

export interface TenantInfo {
  id: string;
  name: string;
  slug: string;
  timezone: string;
  currency_code: string;
  whatsapp_phone_number_id?: string;
  email_from?: string;
}

export type LeadStage =
  | "NEW"
  | "QUALIFYING"
  | "QUALIFIED"
  | "SITE_VISIT_SCHEDULED"
  | "NEGOTIATION"
  | "WON"
  | "LOST"
  | "STALLED"
  | "DISQUALIFIED"
  | "DO_NOT_CONTACT";

export type LeadHealth = "HOT" | "WARM" | "COLD" | "UNKNOWN";

export interface Lead {
  id: string;
  tenant_id: string;
  external_id?: string;
  source: string;
  full_name: string;
  phone?: string;
  phone_masked?: string;
  email?: string;
  email_masked?: string;
  preferred_contact: string;
  budget_min?: number;
  budget_max?: number;
  currency_code: string;
  property_type?: string;
  bhk?: number;
  preferred_location?: string;
  purpose?: string;
  move_in_timeline?: string;
  stage: LeadStage;
  lead_health: LeadHealth;
  intent_score?: number;
  automation_status: string;
  assigned_agent_id?: string;
  do_not_contact: boolean;
  notes?: string;
  raw_message_text?: string;
  last_activity_at?: string;
  score_breakdown?: any;
  created_at: string;
  updated_at: string;
}

export interface LeadActivity {
  id: string;
  lead_id: string;
  actor_id?: string;
  actor_type: string;
  activity_type: string;
  description: string;
  metadata?: any;
  created_at: string;
}

export interface Property {
  id: string;
  tenant_id: string;
  title: string;
  property_type: string;
  bhk?: number;
  price: number;
  currency_code: string;
  location_text: string;
  area_sqft?: number;
  description?: string;
  amenities: string[];
  status: "AVAILABLE" | "RESERVED" | "SOLD" | "OFF_MARKET";
  photos: string[];
  created_at: string;
  updated_at: string;
}

export interface LeadPropertyMatch {
  id: string;
  tenant_id: string;
  lead_id: string;
  property_id: string;
  match_score: number;
  match_reasons: any;
  status: "SUGGESTED" | "SHORTLISTED" | "REJECTED" | "VISITED";
  created_at: string;
  property?: Property;
}

export interface LeadQualificationProposal {
  id: string;
  tenant_id: string;
  lead_id: string;
  proposed_by: string;
  status: "PENDING_REVIEW" | "ACCEPTED" | "REJECTED" | "SUPERSEDED";
  budget_min?: number;
  budget_max?: number;
  property_type?: string;
  bhk?: number;
  preferred_location?: string;
  purpose?: string;
  move_in_timeline?: string;
  confidence: Record<string, number>;
  llm_model?: string;
  reviewed_by?: string;
  reviewed_at?: string;
  review_notes?: string;
  created_at: string;
}

export interface SiteVisit {
  id: string;
  tenant_id: string;
  lead_id: string;
  property_id: string;
  assigned_agent_id?: string;
  scheduled_start_at: string;
  scheduled_end_at: string;
  status: "SCHEDULED" | "CONFIRMED" | "COMPLETED" | "CANCELLED" | "RESCHEDULED" | "NO_SHOW";
  cancellation_reason?: string;
  rescheduled_from_id?: string;
  notes?: string;
  client_confirmed: boolean;
  broker_confirmed: boolean;
  created_at: string;
}

export interface GoogleAuthorizeResponse {
  authorization_url: string;
  state: string;
}

export interface GoogleStatusResponse {
  is_connected: boolean;
  connected_at?: string | null;
  connected_by?: string | null;
  google_email?: string | null;
  scopes: string[];
  spreadsheet_id?: string | null;
  properties_sheet_name: string;
  incoming_leads_sheet_name?: string;
  qualified_leads_sheet_name?: string;
  column_mappings?: Record<string, any>;
  sync_mode?: string;
  last_sync_at?: string | null;
  last_sync_status?: string | null;
  last_sync_error?: string | null;
  last_sync_stats?: Record<string, any>;
}

export interface GoogleConfigUpdateRequest {
  spreadsheet_id?: string | null;
  properties_sheet_name?: string | null;
  incoming_leads_sheet_name?: string | null;
  qualified_leads_sheet_name?: string | null;
  column_mappings?: Record<string, any> | null;
  sync_mode?: string | null;
}

export interface InventorySyncResponse {
  status: string;
  spreadsheet_id: string;
  sheet_name: string;
  total_rows: number;
  created: number;
  updated: number;
  staged: number;
  duplicate_suspects: number;
  skipped: number;
  error_count: number;
  errors: Array<{
    row?: number;
    external_id?: string;
    error?: string;
    reason?: string;
    field?: string;
    [key: string]: any;
  }>;
}
