import React, { useState, useEffect, useCallback } from "react";
import {
  FileSpreadsheet,
  CheckCircle2,
  AlertCircle,
  RefreshCw,
  ExternalLink,
  ShieldCheck,
  Save,
  Unlink,
  Layers,
  AlertTriangle,
} from "lucide-react";
import { api } from "../api";
import type {
  GoogleStatusResponse,
  InventorySyncResponse,
  UserSummary,
  TenantInfo,
} from "../types";

interface GoogleIntegrationViewProps {
  currentUser: UserSummary | null;
  agency: TenantInfo | null;
  onInventorySynced?: () => void;
}

export const GoogleIntegrationView: React.FC<GoogleIntegrationViewProps> = ({
  currentUser,
  agency,
  onInventorySynced,
}) => {
  const [status, setStatus] = useState<GoogleStatusResponse | null>(null);
  const [loadingStatus, setLoadingStatus] = useState(true);
  const [isAuthorizing, setIsAuthorizing] = useState(false);
  const [isSyncing, setIsSyncing] = useState(false);
  const [isSavingConfig, setIsSavingConfig] = useState(false);
  const [isDisconnecting, setIsDisconnecting] = useState(false);

  // Form state
  const [spreadsheetId, setSpreadsheetId] = useState("");
  const [propertiesSheetName, setPropertiesSheetName] = useState("Properties");

  // Notifications
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const [syncResult, setSyncResult] = useState<InventorySyncResponse | null>(null);

  const sanitizeError = (err: any): string => {
    const raw = typeof err === "string" ? err : err?.message || "An unexpected error occurred.";
    // Ensure no sensitive token or key patterns are ever rendered
    return raw
      .replace(/ey[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}/g, "[REDACTED_JWT]")
      .replace(/GOCSPX-[A-Za-z0-9_-]+/g, "[REDACTED_SECRET]")
      .replace(/AIza[0-9A-Za-z-_]{35}/g, "[REDACTED_API_KEY]");
  };

  const fetchStatus = useCallback(async () => {
    setLoadingStatus(true);
    setErrorMessage(null);
    try {
      const data = await api.getGoogleStatus();
      setStatus(data);
      if (data.spreadsheet_id) {
        setSpreadsheetId(data.spreadsheet_id);
      }
      if (data.properties_sheet_name) {
        setPropertiesSheetName(data.properties_sheet_name);
      }
    } catch (err: any) {
      setErrorMessage(sanitizeError(err));
    } finally {
      setLoadingStatus(false);
    }
  }, []);

  useEffect(() => {
    let isMounted = true;
    api
      .getGoogleStatus()
      .then((data) => {
        if (!isMounted) return;
        setStatus(data);
        if (data.spreadsheet_id) setSpreadsheetId(data.spreadsheet_id);
        if (data.properties_sheet_name) setPropertiesSheetName(data.properties_sheet_name);
      })
      .catch((err: any) => {
        if (!isMounted) return;
        setErrorMessage(sanitizeError(err));
      })
      .finally(() => {
        if (!isMounted) return;
        setLoadingStatus(false);
      });
    return () => {
      isMounted = false;
    };
  }, []);

  const handleConnect = async () => {
    setIsAuthorizing(true);
    setErrorMessage(null);
    setSuccessMessage(null);
    try {
      const res = await api.getGoogleAuthorizeUrl();
      if (res && res.authorization_url) {
        // Redirect browser to Google OAuth 2.0 authorization URL
        window.location.href = res.authorization_url;
      } else {
        setErrorMessage("Backend did not provide a valid Google authorization URL.");
      }
    } catch (err: any) {
      setErrorMessage(sanitizeError(err));
      setIsAuthorizing(false);
    }
  };

  const handleSaveConfig = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsSavingConfig(true);
    setErrorMessage(null);
    setSuccessMessage(null);
    try {
      const updated = await api.updateGoogleConfig({
        spreadsheet_id: spreadsheetId.trim() || null,
        properties_sheet_name: propertiesSheetName.trim() || "Properties",
      });
      setStatus(updated);
      setSuccessMessage("Google Sheets configuration updated successfully.");
    } catch (err: any) {
      setErrorMessage(sanitizeError(err));
    } finally {
      setIsSavingConfig(false);
    }
  };

  const handleSyncInventory = async () => {
    setIsSyncing(true);
    setErrorMessage(null);
    setSuccessMessage(null);
    setSyncResult(null);
    try {
      const result = await api.syncGoogleInventory();
      setSyncResult(result);
      setSuccessMessage(
        `Sync completed (${result.status}): ${result.created} created, ${result.updated} updated, ${result.staged} staged.`
      );
      // Re-fetch status to update last_sync timestamp
      await fetchStatus();
      if (onInventorySynced) {
        onInventorySynced();
      }
    } catch (err: any) {
      setErrorMessage(sanitizeError(err));
    } finally {
      setIsSyncing(false);
    }
  };

  const handleDisconnect = async () => {
    if (!window.confirm("Are you sure you want to disconnect Google Sheets? Tokens will be revoked.")) {
      return;
    }
    setIsDisconnecting(true);
    setErrorMessage(null);
    setSuccessMessage(null);
    try {
      await api.disconnectGoogle();
      setSuccessMessage("Google integration disconnected successfully.");
      await fetchStatus();
    } catch (err: any) {
      setErrorMessage(sanitizeError(err));
    } finally {
      setIsDisconnecting(false);
    }
  };

  const isConnected = !!status?.is_connected;
  const canManage = currentUser?.role === "SUPER_ADMIN" || currentUser?.role === "AGENCY_ADMIN" || currentUser?.role === "MANAGER";

  return (
    <div>
      {/* Header */}
      <div className="page-header">
        <div>
          <h1 className="page-title">Google Sheets Integration</h1>
          <p className="page-subtitle">
            Synchronize property inventory and listings securely from Google Sheets into {agency?.name || "HAVN Realty"}
          </p>
        </div>
        <div style={{ display: "flex", gap: "10px" }}>
          <button
            className="btn btn-secondary"
            onClick={fetchStatus}
            disabled={loadingStatus}
            title="Refresh status"
          >
            <RefreshCw size={15} className={loadingStatus ? "spin-icon" : ""} />
            Refresh Status
          </button>
          {isConnected && (
            <button
              className="btn btn-primary"
              onClick={handleSyncInventory}
              disabled={isSyncing || !canManage}
              id="sync-inventory-btn"
            >
              <RefreshCw size={15} className={isSyncing ? "spin-icon" : ""} />
              {isSyncing ? "Syncing Inventory..." : "Sync Inventory Now"}
            </button>
          )}
        </div>
      </div>

      {/* Notifications */}
      {errorMessage && (
        <div
          role="alert"
          style={{
            padding: "12px 16px",
            backgroundColor: "rgba(239, 68, 68, 0.12)",
            border: "1px solid rgba(239, 68, 68, 0.3)",
            borderRadius: "var(--radius-md)",
            color: "#fca5a5",
            fontSize: "0.875rem",
            marginBottom: "20px",
            display: "flex",
            alignItems: "center",
            gap: "10px",
          }}
        >
          <AlertCircle size={18} color="#ef4444" style={{ flexShrink: 0 }} />
          <div style={{ flex: 1 }}>{errorMessage}</div>
          <button
            onClick={() => setErrorMessage(null)}
            style={{ background: "none", border: "none", color: "#fca5a5", cursor: "pointer" }}
          >
            &times;
          </button>
        </div>
      )}

      {successMessage && (
        <div
          role="status"
          style={{
            padding: "12px 16px",
            backgroundColor: "rgba(16, 185, 129, 0.12)",
            border: "1px solid rgba(16, 185, 129, 0.3)",
            borderRadius: "var(--radius-md)",
            color: "#6ee7b7",
            fontSize: "0.875rem",
            marginBottom: "20px",
            display: "flex",
            alignItems: "center",
            gap: "10px",
          }}
        >
          <CheckCircle2 size={18} color="#10b981" style={{ flexShrink: 0 }} />
          <div style={{ flex: 1 }}>{successMessage}</div>
          <button
            onClick={() => setSuccessMessage(null)}
            style={{ background: "none", border: "none", color: "#6ee7b7", cursor: "pointer" }}
          >
            &times;
          </button>
        </div>
      )}

      {/* Main Grid */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "24px", marginBottom: "24px" }}>
        {/* Card 1: Connection Status */}
        <div className="glass-card" style={{ padding: "24px" }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "16px" }}>
            <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
              <div
                style={{
                  width: "38px",
                  height: "38px",
                  borderRadius: "10px",
                  background: isConnected ? "rgba(16, 185, 129, 0.15)" : "rgba(148, 163, 184, 0.1)",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                }}
              >
                <FileSpreadsheet size={20} color={isConnected ? "#10b981" : "#94a3b8"} />
              </div>
              <div>
                <h3 style={{ fontSize: "1.05rem", fontWeight: 600 }}>Connection Status</h3>
                <p style={{ fontSize: "0.8rem", color: "var(--text-muted)" }}>Google OAuth 2.0 Integration</p>
              </div>
            </div>
            <div>
              {loadingStatus ? (
                <span className="badge badge-cold">Checking...</span>
              ) : isConnected ? (
                <span className="badge badge-success">
                  <CheckCircle2 size={12} /> Connected
                </span>
              ) : (
                <span className="badge badge-cold">
                  <AlertCircle size={12} /> Disconnected
                </span>
              )}
            </div>
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: "12px", marginBottom: "20px" }}>
            <div style={{ display: "flex", justifyContent: "space-between", fontSize: "0.875rem", padding: "8px 0", borderBottom: "1px solid var(--border-subtle)" }}>
              <span style={{ color: "var(--text-secondary)" }}>Authorized Account</span>
              <span style={{ fontWeight: 500, color: isConnected ? "var(--text-primary)" : "var(--text-muted)" }}>
                {status?.google_email || "No account connected"}
              </span>
            </div>
            <div style={{ display: "flex", justifyContent: "space-between", fontSize: "0.875rem", padding: "8px 0", borderBottom: "1px solid var(--border-subtle)" }}>
              <span style={{ color: "var(--text-secondary)" }}>Connected Since</span>
              <span style={{ fontWeight: 500, color: "var(--text-muted)" }}>
                {status?.connected_at ? new Date(status.connected_at).toLocaleString() : "—"}
              </span>
            </div>
            <div style={{ display: "flex", justifyContent: "space-between", fontSize: "0.875rem", padding: "8px 0", borderBottom: "1px solid var(--border-subtle)" }}>
              <span style={{ color: "var(--text-secondary)" }}>Granted Scopes</span>
              <span style={{ fontWeight: 500, color: "var(--text-muted)", fontSize: "0.8rem" }}>
                {status?.scopes && status.scopes.length > 0
                  ? status.scopes.map((s) => s.split("/").pop()).join(", ")
                  : "spreadsheets (read/write)"}
              </span>
            </div>
          </div>

          {/* Action Button */}
          {!isConnected ? (
            <div>
              <button
                className="btn btn-primary"
                onClick={handleConnect}
                disabled={isAuthorizing || !canManage}
                style={{ width: "100%", padding: "12px" }}
                id="connect-google-btn"
              >
                {isAuthorizing ? (
                  <>
                    <RefreshCw size={16} className="spin-icon" />
                    Redirecting to Google...
                  </>
                ) : (
                  <>
                    <ExternalLink size={16} />
                    Connect Google Sheets
                  </>
                )}
              </button>
              <p style={{ fontSize: "0.75rem", color: "var(--text-muted)", marginTop: "10px", textAlign: "center" }}>
                Redirects to Google OAuth 2.0 to grant access to property inventory sheets.
              </p>
            </div>
          ) : (
            <div style={{ display: "flex", gap: "10px" }}>
              <button
                className="btn btn-secondary"
                onClick={handleConnect}
                disabled={isAuthorizing || !canManage}
                style={{ flex: 1 }}
              >
                <RefreshCw size={15} />
                Reconnect Account
              </button>
              {currentUser?.role === "AGENCY_ADMIN" || currentUser?.role === "SUPER_ADMIN" ? (
                <button
                  className="btn btn-danger"
                  onClick={handleDisconnect}
                  disabled={isDisconnecting}
                  style={{ display: "flex", alignItems: "center", gap: "6px" }}
                  id="disconnect-google-btn"
                >
                  <Unlink size={15} />
                  Disconnect
                </button>
              ) : null}
            </div>
          )}
        </div>

        {/* Card 2: Spreadsheet Configuration */}
        <div className="glass-card" style={{ padding: "24px" }}>
          <div style={{ display: "flex", alignItems: "center", gap: "10px", marginBottom: "16px" }}>
            <div
              style={{
                width: "38px",
                height: "38px",
                borderRadius: "10px",
                background: "rgba(99, 102, 241, 0.15)",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
              }}
            >
              <Layers size={20} color="var(--accent-primary)" />
            </div>
            <div>
              <h3 style={{ fontSize: "1.05rem", fontWeight: 600 }}>Spreadsheet Settings</h3>
              <p style={{ fontSize: "0.8rem", color: "var(--text-muted)" }}>Target sheet & worksheet name</p>
            </div>
          </div>

          <form onSubmit={handleSaveConfig} style={{ display: "flex", flexDirection: "column", gap: "14px" }}>
            <div>
              <label style={{ display: "block", fontSize: "0.8125rem", color: "var(--text-secondary)", marginBottom: "6px", fontWeight: 500 }}>
                Google Spreadsheet ID
              </label>
              <input
                type="text"
                className="form-control"
                placeholder="e.g. 1tbSRSAkqPqU6OKlpE_NDXspnh_QbMbeXxT7ubI8Xr-Y"
                value={spreadsheetId}
                onChange={(e) => setSpreadsheetId(e.target.value)}
                disabled={!canManage}
                id="spreadsheet-id-input"
              />
              <p style={{ fontSize: "0.72rem", color: "var(--text-muted)", marginTop: "4px" }}>
                Found in the sheet URL: docs.google.com/spreadsheets/d/<strong>[SPREADSHEET_ID]</strong>/edit
              </p>
            </div>

            <div>
              <label style={{ display: "block", fontSize: "0.8125rem", color: "var(--text-secondary)", marginBottom: "6px", fontWeight: 500 }}>
                Worksheet Tab Name
              </label>
              <input
                type="text"
                className="form-control"
                placeholder="Properties"
                value={propertiesSheetName}
                onChange={(e) => setPropertiesSheetName(e.target.value)}
                disabled={!canManage}
                id="sheet-name-input"
              />
            </div>

            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", paddingTop: "8px" }}>
              <div style={{ fontSize: "0.8125rem", color: "var(--text-muted)" }}>
                Mode: <span className="badge badge-warm">{status?.sync_mode || "MERGE_VALIDATED"}</span>
              </div>
              <button
                type="submit"
                className="btn btn-secondary"
                disabled={isSavingConfig || !canManage}
                id="save-config-btn"
              >
                <Save size={15} />
                {isSavingConfig ? "Saving..." : "Save Settings"}
              </button>
            </div>
          </form>
        </div>
      </div>

      {/* Card 3: Inventory Sync Section */}
      <div className="glass-card" style={{ padding: "24px", marginBottom: "24px" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "20px" }}>
          <div>
            <h3 style={{ fontSize: "1.1rem", fontWeight: 600 }}>Property Inventory Synchronization</h3>
            <p style={{ fontSize: "0.85rem", color: "var(--text-secondary)" }}>
              Pulls rows from Google Sheets, validates required schema (title, type, price, location), stages incomplete rows, and merges verified properties.
            </p>
          </div>
          <button
            className="btn btn-primary"
            onClick={handleSyncInventory}
            disabled={!isConnected || isSyncing || !canManage}
            id="sync-inventory-btn-secondary"
          >
            <RefreshCw size={16} className={isSyncing ? "spin-icon" : ""} />
            {isSyncing ? "Syncing Now..." : "Sync Inventory Now"}
          </button>
        </div>

        {/* Sync Status Banner */}
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(3, 1fr)",
            gap: "16px",
            backgroundColor: "rgba(15, 23, 42, 0.4)",
            padding: "16px",
            borderRadius: "var(--radius-md)",
            border: "1px solid var(--border-subtle)",
            marginBottom: "20px",
          }}
        >
          <div>
            <div style={{ fontSize: "0.75rem", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.04em" }}>
              Last Sync Attempt
            </div>
            <div style={{ fontSize: "0.95rem", fontWeight: 600, marginTop: "4px" }}>
              {status?.last_sync_at ? new Date(status.last_sync_at).toLocaleString() : "Never Synced"}
            </div>
          </div>
          <div>
            <div style={{ fontSize: "0.75rem", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.04em" }}>
              Last Sync Result
            </div>
            <div style={{ marginTop: "4px" }}>
              {status?.last_sync_status === "SUCCESS" && <span className="badge badge-success">SUCCESS</span>}
              {status?.last_sync_status === "PARTIAL" && <span className="badge badge-warm">PARTIAL</span>}
              {status?.last_sync_status === "FAILED" && <span className="badge badge-danger">FAILED</span>}
              {status?.last_sync_status === "RUNNING" && <span className="badge badge-warm">RUNNING</span>}
              {!status?.last_sync_status && <span className="badge badge-cold">NOT STARTED</span>}
            </div>
          </div>
          <div>
            <div style={{ fontSize: "0.75rem", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.04em" }}>
              Target Worksheet
            </div>
            <div style={{ fontSize: "0.95rem", fontWeight: 600, marginTop: "4px", color: "var(--accent-primary)" }}>
              {status?.properties_sheet_name || "Properties"}
            </div>
          </div>
        </div>

        {/* Last Sync Error Display if present */}
        {status?.last_sync_error && (
          <div
            style={{
              padding: "12px",
              backgroundColor: "rgba(239, 68, 68, 0.1)",
              border: "1px solid rgba(239, 68, 68, 0.25)",
              borderRadius: "var(--radius-md)",
              marginBottom: "20px",
              display: "flex",
              alignItems: "center",
              gap: "10px",
              color: "#fca5a5",
              fontSize: "0.85rem",
            }}
          >
            <AlertTriangle size={16} color="#ef4444" />
            <div>
              <strong>Last Sync Issue:</strong> {sanitizeError(status.last_sync_error)}
            </div>
          </div>
        )}

        {/* Sync Summary Metrics (Shown if syncResult or status.last_sync_stats is present) */}
        {(syncResult || (status?.last_sync_stats && Object.keys(status.last_sync_stats).length > 0)) && (
          <div>
            <h4 style={{ fontSize: "0.9rem", fontWeight: 600, marginBottom: "12px", color: "var(--text-secondary)" }}>
              {syncResult ? "Latest Sync Execution Results" : "Previous Sync Statistics"}
            </h4>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(6, 1fr)", gap: "12px" }}>
              <div
                style={{
                  background: "rgba(255, 255, 255, 0.03)",
                  padding: "14px",
                  borderRadius: "var(--radius-md)",
                  border: "1px solid var(--border-subtle)",
                  textAlign: "center",
                }}
              >
                <div style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>Total Rows</div>
                <div style={{ fontSize: "1.25rem", fontWeight: 700, marginTop: "4px" }} id="stat-total-rows">
                  {syncResult ? syncResult.total_rows : status?.last_sync_stats?.total_rows ?? 0}
                </div>
              </div>

              <div
                style={{
                  background: "rgba(16, 185, 129, 0.05)",
                  padding: "14px",
                  borderRadius: "var(--radius-md)",
                  border: "1px solid rgba(16, 185, 129, 0.2)",
                  textAlign: "center",
                }}
              >
                <div style={{ fontSize: "0.75rem", color: "#34d399" }}>Created</div>
                <div style={{ fontSize: "1.25rem", fontWeight: 700, color: "#34d399", marginTop: "4px" }} id="stat-created">
                  {syncResult ? syncResult.created : status?.last_sync_stats?.created ?? 0}
                </div>
              </div>

              <div
                style={{
                  background: "rgba(99, 102, 241, 0.05)",
                  padding: "14px",
                  borderRadius: "var(--radius-md)",
                  border: "1px solid rgba(99, 102, 241, 0.2)",
                  textAlign: "center",
                }}
              >
                <div style={{ fontSize: "0.75rem", color: "#a5b4fc" }}>Updated</div>
                <div style={{ fontSize: "1.25rem", fontWeight: 700, color: "#a5b4fc", marginTop: "4px" }} id="stat-updated">
                  {syncResult ? syncResult.updated : status?.last_sync_stats?.updated ?? 0}
                </div>
              </div>

              <div
                style={{
                  background: "rgba(245, 158, 11, 0.05)",
                  padding: "14px",
                  borderRadius: "var(--radius-md)",
                  border: "1px solid rgba(245, 158, 11, 0.2)",
                  textAlign: "center",
                }}
              >
                <div style={{ fontSize: "0.75rem", color: "#fbbf24" }}>Staged</div>
                <div style={{ fontSize: "1.25rem", fontWeight: 700, color: "#fbbf24", marginTop: "4px" }} id="stat-staged">
                  {syncResult ? syncResult.staged : status?.last_sync_stats?.staged ?? 0}
                </div>
              </div>

              <div
                style={{
                  background: "rgba(148, 163, 184, 0.05)",
                  padding: "14px",
                  borderRadius: "var(--radius-md)",
                  border: "1px solid rgba(148, 163, 184, 0.2)",
                  textAlign: "center",
                }}
              >
                <div style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>Skipped</div>
                <div style={{ fontSize: "1.25rem", fontWeight: 700, marginTop: "4px" }}>
                  {syncResult ? syncResult.skipped : status?.last_sync_stats?.skipped ?? 0}
                </div>
              </div>

              <div
                style={{
                  background: "rgba(239, 68, 68, 0.05)",
                  padding: "14px",
                  borderRadius: "var(--radius-md)",
                  border: "1px solid rgba(239, 68, 68, 0.2)",
                  textAlign: "center",
                }}
              >
                <div style={{ fontSize: "0.75rem", color: "#f87171" }}>Errors</div>
                <div style={{ fontSize: "1.25rem", fontWeight: 700, color: "#f87171", marginTop: "4px" }} id="stat-errors">
                  {syncResult ? syncResult.error_count : status?.last_sync_stats?.errors_count ?? 0}
                </div>
              </div>
            </div>

            {/* Error table if errors were encountered during sync */}
            {syncResult?.errors && syncResult.errors.length > 0 && (
              <div style={{ marginTop: "16px" }}>
                <div style={{ fontSize: "0.8125rem", fontWeight: 600, color: "#f87171", marginBottom: "8px" }}>
                  Row Validation Errors ({syncResult.errors.length})
                </div>
                <div
                  style={{
                    maxHeight: "160px",
                    overflowY: "auto",
                    backgroundColor: "rgba(0, 0, 0, 0.3)",
                    borderRadius: "var(--radius-md)",
                    border: "1px solid var(--border-subtle)",
                    fontSize: "0.8rem",
                  }}
                >
                  <table style={{ width: "100%", borderCollapse: "collapse" }}>
                    <thead>
                      <tr style={{ borderBottom: "1px solid var(--border-subtle)", textAlign: "left" }}>
                        <th style={{ padding: "8px 12px", color: "var(--text-muted)" }}>Row</th>
                        <th style={{ padding: "8px 12px", color: "var(--text-muted)" }}>Identifier</th>
                        <th style={{ padding: "8px 12px", color: "var(--text-muted)" }}>Reason</th>
                      </tr>
                    </thead>
                    <tbody>
                      {syncResult.errors.map((err, idx) => (
                        <tr key={idx} style={{ borderBottom: "1px solid rgba(255, 255, 255, 0.03)" }}>
                          <td style={{ padding: "8px 12px", color: "var(--text-secondary)" }}>{err.row ?? "N/A"}</td>
                          <td style={{ padding: "8px 12px", color: "var(--text-secondary)" }}>{err.external_id || err.field || "—"}</td>
                          <td style={{ padding: "8px 12px", color: "#fca5a5" }}>{sanitizeError(err.error || err.reason || "Validation failed")}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Security & Pipeline Guarantee Notice */}
      <div
        className="glass-card"
        style={{
          padding: "16px 20px",
          display: "flex",
          alignItems: "center",
          gap: "14px",
          background: "rgba(99, 102, 241, 0.04)",
          border: "1px solid rgba(99, 102, 241, 0.15)",
        }}
      >
        <ShieldCheck size={24} color="var(--accent-primary)" style={{ flexShrink: 0 }} />
        <div style={{ fontSize: "0.8125rem", color: "var(--text-secondary)", lineHeight: 1.5 }}>
          <strong style={{ color: "var(--text-primary)" }}>Zero Credential Leakage & Lease Lock Guarantee:</strong> All OAuth tokens are encrypted at rest using AES-GCM and never sent to browser clients. Synchronization uses distributed PostgreSQL row-level lease locks to ensure single-flight execution and prevent duplicate inventory writes.
        </div>
      </div>
    </div>
  );
};
