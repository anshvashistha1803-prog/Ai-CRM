import { useState, useEffect, useCallback } from "react";
import { Sidebar } from "./components/Sidebar";
import { Navbar } from "./components/Navbar";
import { LoginModal } from "./components/LoginModal";
import { DashboardView } from "./views/DashboardView";
import { LeadsView } from "./views/LeadsView";
import { ProposalsView } from "./views/ProposalsView";
import { PropertiesView } from "./views/PropertiesView";
import { VisitsView } from "./views/VisitsView";
import { TeamView } from "./views/TeamView";
import { GoogleIntegrationView } from "./views/GoogleIntegrationView";
import { api } from "./api";
import type { Lead, Property, SiteVisit, UserSummary, TenantInfo } from "./types";

export function App() {
  const [currentUser, setCurrentUser] = useState<UserSummary | null>(null);
  const [agency, setAgency] = useState<TenantInfo | null>(null);
  const [currentView, setCurrentView] = useState("dashboard");
  const [searchTerm, setSearchTerm] = useState("");
  const [openCreateLead, setOpenCreateLead] = useState(false);

  // Core Data Collections
  const [leads, setLeads] = useState<Lead[]>([]);
  const [properties, setProperties] = useState<Property[]>([]);
  const [visits, setVisits] = useState<SiteVisit[]>([]);
  const [teamUsers, setTeamUsers] = useState<UserSummary[]>([]);
  const [isInitializing, setIsInitializing] = useState(true);

  // Load app data
  const refreshAllData = useCallback(async () => {
    try {
      const [leadsRes, propsRes, visitsRes, teamRes, agencyRes] = await Promise.allSettled([
        api.getLeads(),
        api.getProperties(),
        api.getVisits(),
        api.getTeamUsers(),
        api.getAgency(),
      ]);

      if (leadsRes.status === "fulfilled") setLeads(leadsRes.value.data || []);
      if (propsRes.status === "fulfilled") setProperties(propsRes.value.data || []);
      if (visitsRes.status === "fulfilled") setVisits(visitsRes.value || []);
      if (teamRes.status === "fulfilled") setTeamUsers(teamRes.value || []);
      if (agencyRes.status === "fulfilled") setAgency(agencyRes.value);
    } catch {
      // Handled
    }
  }, []);

  // Check existing session on mount
  useEffect(() => {
    const initSession = async () => {
      const token = api.getToken();
      if (token) {
        try {
          const user = await api.getCurrentUser();
          setCurrentUser(user);
          await refreshAllData();
        } catch {
          api.logout();
          setCurrentUser(null);
        }
      }
      setIsInitializing(false);
    };

    initSession();
  }, [refreshAllData]);

  const handleLoginSuccess = async (user: UserSummary) => {
    setCurrentUser(user);
    await refreshAllData();
  };

  const handleLogout = () => {
    api.logout();
    setCurrentUser(null);
  };

  if (isInitializing) {
    return (
      <div style={{ height: "100vh", display: "flex", alignItems: "center", justifyContent: "center", backgroundColor: "var(--bg-dark)" }}>
        <div style={{ color: "var(--text-muted)", fontSize: "0.875rem" }}>Initializing RealEstate.AI CRM...</div>
      </div>
    );
  }

  return (
    <div className="app-container">
      {/* If not authenticated, display login modal */}
      {!currentUser && <LoginModal onSuccess={handleLoginSuccess} />}

      {/* Main Authenticated Layout */}
      <Sidebar
        currentView={currentView}
        onSelectView={setCurrentView}
        currentUser={currentUser}
        agency={agency}
        onLogout={handleLogout}
      />

      <div className="main-content">
        <Navbar
          agency={agency}
          searchTerm={searchTerm}
          onSearchChange={setSearchTerm}
        />

        <main className="content-body">
          {currentView === "dashboard" && (
            <DashboardView
              leads={leads}
              properties={properties}
              visits={visits}
              onNavigate={setCurrentView}
              onOpenNewLead={() => setOpenCreateLead(true)}
            />
          )}

          {currentView === "leads" && (
            <LeadsView
              leads={leads}
              teamUsers={teamUsers}
              currentUser={currentUser}
              onRefresh={refreshAllData}
              openCreateModal={openCreateLead}
              onOpenCreateModal={() => setOpenCreateLead(true)}
              onCloseCreateModal={() => setOpenCreateLead(false)}
            />
          )}

          {currentView === "proposals" && (
            <ProposalsView
              leads={leads}
              onRefresh={refreshAllData}
            />
          )}

          {currentView === "properties" && (
            <PropertiesView
              properties={properties}
              onRefresh={refreshAllData}
            />
          )}

          {currentView === "visits" && (
            <VisitsView
              visits={visits}
              leads={leads}
              properties={properties}
              teamUsers={teamUsers}
              onRefresh={refreshAllData}
            />
          )}

          {currentView === "team" && (
            <TeamView
              teamUsers={teamUsers}
              agency={agency}
              onRefresh={refreshAllData}
            />
          )}

          {currentView === "integrations" && (
            <GoogleIntegrationView
              currentUser={currentUser}
              agency={agency}
              onInventorySynced={refreshAllData}
            />
          )}
        </main>
      </div>
    </div>
  );
}

export default App;
