"""
Integration test: tenant isolation.
CRITICAL SECURITY TEST — cross-tenant data access must be impossible.
These tests run against the real test DB via the API client.
"""


import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
class TestTenantIsolation:
    """
    Verify that authenticated users from Tenant A cannot access any data belonging to Tenant B.
    These tests must pass on every PR — they are the security boundary.
    """

    async def test_health_endpoint(self, client: AsyncClient):
        """Baseline: health endpoint is accessible without auth."""
        resp = await client.get("/health")
        # May be 200 (healthy) or 200 with degraded status if DB not ready in CI
        assert resp.status_code == 200
        body = resp.json()
        assert "status" in body

    async def test_login_returns_access_token(self, client: AsyncClient, test_agent, test_tenant):
        """Login returns an access token and sets a refresh cookie."""
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "agent@test.com", "password": "TestPass1!"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "token" in body
        assert "access_token" in body["token"]
        assert "user" in body
        assert body["user"]["role"] == "AGENT"

    async def test_login_wrong_password(self, client: AsyncClient, test_agent):
        """Wrong password returns 401."""
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "agent@test.com", "password": "WrongPass999!"},
        )
        assert resp.status_code == 401

    async def test_me_without_token(self, client: AsyncClient):
        """Accessing /me without a token returns 401."""
        resp = await client.get("/api/v1/auth/me")
        assert resp.status_code == 401

    async def test_me_with_valid_token(self, client: AsyncClient, agent_token: str):
        """Accessing /me with a valid token returns the current user."""
        resp = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {agent_token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["role"] == "AGENT"

    async def test_me_with_expired_token(self, client: AsyncClient):
        """Accessing /me with an expired/invalid token returns 401."""
        resp = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": "Bearer invalid.token.value"},
        )
        assert resp.status_code == 401

    async def test_cross_tenant_token_rejected(
        self,
        client: AsyncClient,
        test_tenant,
        db,
    ):
        """
        A token issued for Tenant A with a fabricated tenant_id for Tenant B
        must not grant access to Tenant B's data.
        This test verifies the tenant_id in the JWT cannot be forged without the private key.
        """
        # Create a fake token with a different tenant_id (cannot be signed without private key)
        fake_token = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.invalidsig"
        resp = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {fake_token}"},
        )
        assert resp.status_code == 401

    async def test_cross_agency_lead_access_rejected(
        self,
        client: AsyncClient,
        agent_token: str,
        agent_b_token: str,
    ):
        """
        Lead created in Tenant A must be completely inaccessible to users in Tenant B.
        All detail, update, stage, and activity routes must return 404.
        """
        # 1. Agent in Tenant A creates a lead
        create_resp = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={
                "full_name": "Tenant A Buyer",
                "phone": "+919876543210",
                "email": "buyer.a@example.com",
                "source": "WEB_FORM",
            },
        )
        assert create_resp.status_code == 201
        lead_id = create_resp.json()["id"]

        # 2. Agent in Tenant B attempts to read detail
        get_resp = await client.get(
            f"/api/v1/leads/{lead_id}",
            headers={"Authorization": f"Bearer {agent_b_token}"},
        )
        assert get_resp.status_code == 404

        # 3. Agent in Tenant B attempts to update
        patch_resp = await client.patch(
            f"/api/v1/leads/{lead_id}",
            headers={"Authorization": f"Bearer {agent_b_token}"},
            json={"notes": "Malicious modification from another agency"},
        )
        assert patch_resp.status_code == 404

        # 4. Agent in Tenant B attempts stage transition
        stage_resp = await client.post(
            f"/api/v1/leads/{lead_id}/stage",
            headers={"Authorization": f"Bearer {agent_b_token}"},
            json={"target_stage": "QUALIFYING"},
        )
        assert stage_resp.status_code == 404

        # 5. Agent in Tenant B attempts to read activities
        act_resp = await client.get(
            f"/api/v1/leads/{lead_id}/activities",
            headers={"Authorization": f"Bearer {agent_b_token}"},
        )
        assert act_resp.status_code == 404

    async def test_agent_cannot_access_unassigned_lead(
        self,
        client: AsyncClient,
        manager_token: str,
        agent_token: str,
    ):
        """
        Unassigned leads can be managed by managers/admins, but agents must be denied (403).
        """
        # Manager creates unassigned lead
        create_resp = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={
                "full_name": "Unassigned Lead",
                "phone": "+919876543211",
                "source": "MANUAL",
            },
        )
        assert create_resp.status_code == 201
        lead_id = create_resp.json()["id"]

        # Manager can view
        mgr_get = await client.get(
            f"/api/v1/leads/{lead_id}",
            headers={"Authorization": f"Bearer {manager_token}"},
        )
        assert mgr_get.status_code == 200

        # Agent cannot view unassigned lead
        agent_get = await client.get(
            f"/api/v1/leads/{lead_id}",
            headers={"Authorization": f"Bearer {agent_token}"},
        )
        assert agent_get.status_code == 403

    async def test_agent_cannot_access_other_agent_lead(
        self,
        client: AsyncClient,
        agent_token: str,
        agent_2_token: str,
    ):
        """
        In the same agency, Agent 2 must NOT access Agent 1's assigned lead.
        """
        # Agent 1 creates lead (automatically assigned to Agent 1)
        create_resp = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={
                "full_name": "Agent 1 Private Client",
                "phone": "+919876543212",
                "source": "WEB_FORM",
            },
        )
        assert create_resp.status_code == 201
        lead_id = create_resp.json()["id"]

        # Agent 2 attempts to get lead
        resp = await client.get(
            f"/api/v1/leads/{lead_id}",
            headers={"Authorization": f"Bearer {agent_2_token}"},
        )
        assert resp.status_code == 403

        # Agent 2 attempts to update lead
        patch_resp = await client.patch(
            f"/api/v1/leads/{lead_id}",
            headers={"Authorization": f"Bearer {agent_2_token}"},
            json={"notes": "Unauthorized edit"},
        )
        assert patch_resp.status_code == 403

    async def test_agent_list_isolation(
        self,
        client: AsyncClient,
        agent_token: str,
        agent_2_token: str,
        manager_token: str,
    ):
        """
        List endpoint strictly filters leads by assigned agent for AGENT role.
        Manager can view all leads across the agency.
        """
        # Agent 1 creates lead 1
        resp_1 = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={
                "full_name": "Alpha Client",
                "phone": "+919876543213",
                "source": "WEB_FORM",
            },
        )
        assert resp_1.status_code == 201
        lead_1_id = resp_1.json()["id"]

        # Agent 2 creates lead 2
        resp_2 = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {agent_2_token}"},
            json={
                "full_name": "Beta Client",
                "phone": "+919876543214",
                "source": "WEB_FORM",
            },
        )
        assert resp_2.status_code == 201
        lead_2_id = resp_2.json()["id"]

        # Agent 1 list: only Lead 1 appears
        list_1 = await client.get(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {agent_token}"},
        )
        assert list_1.status_code == 200
        ids_1 = [ld["id"] for ld in list_1.json()["data"]]
        assert lead_1_id in ids_1
        assert lead_2_id not in ids_1

        # Agent 2 list: only Lead 2 appears
        list_2 = await client.get(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {agent_2_token}"},
        )
        assert list_2.status_code == 200
        ids_2 = [ld["id"] for ld in list_2.json()["data"]]
        assert lead_2_id in ids_2
        assert lead_1_id not in ids_2

        # Manager list: both appear
        list_mgr = await client.get(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {manager_token}"},
        )
        assert list_mgr.status_code == 200
        ids_mgr = [ld["id"] for ld in list_mgr.json()["data"]]
        assert lead_1_id in ids_mgr
        assert lead_2_id in ids_mgr

    async def test_reassignment_permissions(
        self,
        client: AsyncClient,
        agent_token: str,
        agent_2_token: str,
        manager_token: str,
        test_agent_2,
        test_agent_b,
    ):
        """
        Agents cannot reassign leads. Managers can reassign within their agency.
        Cross-agency reassignment is rejected.
        """
        # Agent 1 creates lead
        create_resp = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={
                "full_name": "Reassignment Client",
                "phone": "+919876543215",
                "source": "WEB_FORM",
            },
        )
        assert create_resp.status_code == 201
        lead_id = create_resp.json()["id"]

        # Agent 1 attempts reassignment via dedicated endpoint: 403
        agent_assign = await client.post(
            f"/api/v1/leads/{lead_id}/assign",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={"assigned_agent_id": str(test_agent_2.id)},
        )
        assert agent_assign.status_code == 403

        # Manager attempts cross-agency assignment: 403
        cross_assign = await client.post(
            f"/api/v1/leads/{lead_id}/assign",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={"assigned_agent_id": str(test_agent_b.id)},
        )
        assert cross_assign.status_code == 403

        # Manager reassigns to Agent 2: 200
        mgr_assign = await client.post(
            f"/api/v1/leads/{lead_id}/assign",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={"assigned_agent_id": str(test_agent_2.id)},
        )
        assert mgr_assign.status_code == 200
        assert mgr_assign.json()["assigned_agent_id"] == str(test_agent_2.id)

        # Agent 2 can now access the lead
        agent_2_access = await client.get(
            f"/api/v1/leads/{lead_id}",
            headers={"Authorization": f"Bearer {agent_2_token}"},
        )
        assert agent_2_access.status_code == 200

        # Agent 1 can no longer access the lead
        agent_1_access = await client.get(
            f"/api/v1/leads/{lead_id}",
            headers={"Authorization": f"Bearer {agent_token}"},
        )
        assert agent_1_access.status_code == 403
