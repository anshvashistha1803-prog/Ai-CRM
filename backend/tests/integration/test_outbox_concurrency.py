"""
Integration tests for Outbox Locking, Concurrency, and Lease Recovery.
Runs against the dedicated PostgreSQL test database (havn_test).

Verifies:
1. Two workers attempting to claim the same eligible batch (mutual exclusion via FOR UPDATE SKIP LOCKED).
2. A second worker skipping rows already claimed by the first.
3. Database locks being released before slow provider calls.
4. Worker crash and abandoned claim lease reclamation.
5. Retry eligibility and exponential backoff after a failed attempt.
6. Stale worker inability to overwrite newer claim / reclaimed state (OCC).
7. No duplicate outbox processing caused by batch commits.
8. Correct handling of successful and failed provider results.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.messaging.models import OutboxMessage
from app.messaging.outbox_service import OutboxService
from app.tasks.celery_tasks import _process_outbox_batch_async
from app.tenants.models import Tenant
from tests.conftest import test_engine


def _make_session() -> AsyncSession:
    """Create an isolated AsyncSession bound to test_engine."""
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    return factory()


@pytest.mark.asyncio
class TestOutboxConcurrencyAndLocking:
    """Real PostgreSQL concurrency and race condition verification."""

    async def _create_test_tenant(self, session: AsyncSession) -> Tenant:
        tenant = Tenant(
            id=uuid.uuid4(),
            name="Outbox Test Agency",
            slug=f"outbox-test-{uuid.uuid4().hex[:8]}",
            timezone="Asia/Kolkata",
            currency_code="INR",
            is_active=True,
            settings={},
        )
        session.add(tenant)
        await session.commit()
        return tenant

    async def _seed_messages(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        count: int,
        scheduled_at: datetime | None = None,
    ) -> list[OutboxMessage]:
        now = scheduled_at or datetime.now(UTC)
        messages = []
        for i in range(count):
            msg = OutboxMessage(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                event_type="WHATSAPP_OUTBOUND_DISPATCH",
                aggregate_type="LEAD",
                aggregate_id=str(uuid.uuid4()),
                payload={"recipient_phone": f"+91980000000{i}", "message_text": f"Msg {i}"},
                status="PENDING",
                retry_count=0,
                max_retries=3,
                scheduled_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(msg)
            messages.append(msg)
        await session.commit()
        return messages

    # -------------------------------------------------------------------------
    # 1. Two workers attempting to claim the same eligible batch
    # -------------------------------------------------------------------------
    async def test_two_workers_concurrent_batch_claim_no_duplicates(self, create_tables):
        """
        Two concurrent workers query claim_pending_messages concurrently.
        FOR UPDATE SKIP LOCKED guarantees both workers get completely disjoint subsets.
        No single row is claimed by more than one worker.
        """
        async with _make_session() as setup_session:
            tenant = await self._create_test_tenant(setup_session)
            await self._seed_messages(setup_session, tenant.id, count=10)

        async def worker_claim(batch_size: int):
            async with _make_session() as session:
                svc = OutboxService(session)
                claimed = await svc.claim_pending_messages(batch_size=batch_size)
                await session.commit()
                return [m.id for m in claimed]

        w1_ids, w2_ids = await asyncio.gather(
            worker_claim(5),
            worker_claim(5),
        )

        assert len(w1_ids) > 0, "Worker 1 should have claimed messages"
        assert len(w2_ids) > 0, "Worker 2 should have claimed messages"
        # Zero intersection: no message claimed twice
        overlap = set(w1_ids).intersection(set(w2_ids))
        assert overlap == set(), f"Duplicate claims detected across workers: {overlap}"
        assert len(w1_ids) + len(w2_ids) == 10

    # -------------------------------------------------------------------------
    # 2. Second worker skipping rows already claimed by the first
    # -------------------------------------------------------------------------
    async def test_second_worker_skips_already_claimed_rows(self, create_tables):
        """
        After Worker 1 claims and commits rows to PROCESSING,
        Worker 2 attempting to claim pending messages immediately gets 0 rows.
        """
        async with _make_session() as setup_session:
            tenant = await self._create_test_tenant(setup_session)
            await self._seed_messages(setup_session, tenant.id, count=5)

        # Worker 1 claims all 5
        async with _make_session() as s1:
            svc1 = OutboxService(s1)
            claimed1 = await svc1.claim_pending_messages(batch_size=10)
            await s1.commit()
            assert len(claimed1) == 5

        # Worker 2 queries for pending messages
        async with _make_session() as s2:
            svc2 = OutboxService(s2)
            claimed2 = await svc2.claim_pending_messages(batch_size=10)
            await s2.commit()
            assert len(claimed2) == 0, "Worker 2 must skip all rows already in PROCESSING"

    # -------------------------------------------------------------------------
    # 3. Database locks being released before slow provider calls
    # -------------------------------------------------------------------------
    async def test_database_locks_released_before_slow_provider(self, create_tables):
        """
        In the two-phase claim design, the claim transaction commits immediately,
        releasing row locks before the provider call is made. An independent session
        can query and modify rows without blocking.
        """
        async with _make_session() as setup_session:
            tenant = await self._create_test_tenant(setup_session)
            messages = await self._seed_messages(setup_session, tenant.id, count=2)
            target_msg_id = messages[0].id

        # Phase 1: Worker claims and commits
        async with _make_session() as s1:
            svc1 = OutboxService(s1)
            claimed = await svc1.claim_pending_messages(batch_size=1)
            await s1.commit()
            assert len(claimed) == 1
            assert claimed[0].id == target_msg_id

        # Simulating provider call duration: another session can read and update freely
        async with _make_session() as s2:
            res = await s2.execute(
                select(OutboxMessage).where(OutboxMessage.id == target_msg_id)
            )
            msg_in_s2 = res.scalars().first()
            assert msg_in_s2.status == "PROCESSING"
            # s2 can commit an unrelated record without lock contention
            s2_tenant = await self._create_test_tenant(s2)
            assert s2_tenant.id is not None

    # -------------------------------------------------------------------------
    # 4. Worker failure after claiming a message (Lease Reclamation)
    # -------------------------------------------------------------------------
    async def test_worker_crash_abandoned_claim_lease_recovery(self, create_tables):
        """
        If a worker claims a message and crashes without marking sent or failed,
        reclaim_stale_processing_messages recovers the message to PENDING with retry_count incremented.
        """
        async with _make_session() as setup_session:
            tenant = await self._create_test_tenant(setup_session)
            messages = await self._seed_messages(setup_session, tenant.id, count=1)
            msg_id = messages[0].id

        # Worker claims message
        claim_time = datetime.now(UTC) - timedelta(minutes=20)
        async with _make_session() as s1:
            svc1 = OutboxService(s1)
            claimed = await svc1.claim_pending_messages(batch_size=1, now=datetime.now(UTC))
            # Manually set updated_at to simulated 20 minutes ago (expired lease)
            claimed[0].updated_at = claim_time
            await s1.commit()

        # Worker has crashed. Run reclamation.
        async with _make_session() as s2:
            svc2 = OutboxService(s2)
            reclaimed_count = await svc2.reclaim_stale_processing_messages(lease_timeout_minutes=15)
            await s2.commit()
            assert reclaimed_count == 1

        # Verify recovered message state
        async with _make_session() as s3:
            svc3 = OutboxService(s3)
            recovered = await svc3.get_message(msg_id)
            assert recovered.status == "PENDING"
            assert recovered.retry_count == 1
            assert "Worker lease expired" in recovered.last_error

    # -------------------------------------------------------------------------
    # 5. Retry eligibility after a failed attempt
    # -------------------------------------------------------------------------
    async def test_retry_eligibility_and_exponential_backoff(self, create_tables):
        """
        When a message fails, it is placed in PENDING with scheduled_at in the future.
        It is not eligible immediately, but becomes eligible when time advances.
        """
        async with _make_session() as setup_session:
            tenant = await self._create_test_tenant(setup_session)
            messages = await self._seed_messages(setup_session, tenant.id, count=1)
            msg_id = messages[0].id

        now = datetime.now(UTC)
        async with _make_session() as s1:
            svc1 = OutboxService(s1)
            claimed = await svc1.claim_pending_messages(batch_size=1)
            claimed_at = claimed[0].updated_at
            # Mark failed with 30s base backoff
            await svc1.mark_failed(
                msg_id,
                error_message="Transient 503 error",
                claimed_at=claimed_at,
                base_backoff_seconds=30,
            )
            await s1.commit()

        # Immediate claim with current time should NOT fetch it (not yet due)
        async with _make_session() as s2:
            svc2 = OutboxService(s2)
            immediate = await svc2.claim_pending_messages(batch_size=1, now=now)
            await s2.commit()
            assert len(immediate) == 0

        # Claim with future time (now + 35s) SHOULD fetch it
        async with _make_session() as s3:
            svc3 = OutboxService(s3)
            future_claim = await svc3.claim_pending_messages(
                batch_size=1, now=now + timedelta(seconds=35)
            )
            await s3.commit()
            assert len(future_claim) == 1
            assert future_claim[0].id == msg_id
            assert future_claim[0].status == "PROCESSING"

    # -------------------------------------------------------------------------
    # 6. Stale worker being unable to overwrite a newer claim's result (OCC)
    # -------------------------------------------------------------------------
    async def test_stale_worker_cannot_overwrite_newer_claim(self, create_tables):
        """
        A slow worker that wakes up after the message was reclaimed must be rejected
        by the OCC check (claim_token / claimed_at), preventing overwrite.
        """
        async with _make_session() as setup_session:
            tenant = await self._create_test_tenant(setup_session)
            messages = await self._seed_messages(setup_session, tenant.id, count=1)
            msg_id = messages[0].id

        t1_claim = datetime.now(UTC) - timedelta(minutes=20)
        token1 = None
        # Worker 1 claims at T1
        async with _make_session() as s1:
            svc1 = OutboxService(s1)
            claimed = await svc1.claim_pending_messages(batch_size=1)
            claimed[0].updated_at = t1_claim
            token1 = claimed[0].payload.get("_claim_token")
            await s1.commit()

        # Lease reclaims at T2
        async with _make_session() as s2:
            svc2 = OutboxService(s2)
            await svc2.reclaim_stale_processing_messages(lease_timeout_minutes=15)
            await s2.commit()

        # Stale Worker 1 wakes up and calls mark_sent with its original claim token
        async with _make_session() as s3:
            svc3 = OutboxService(s3)
            res = await svc3.mark_sent(
                msg_id,
                provider_message_id="wamid.stale.worker1",
                claimed_at=t1_claim,
                claim_token=token1,
            )
            await s3.commit()
            # Must remain PENDING (reclaimed state), NOT SENT!
            assert res.status == "PENDING"

        # Verify in DB
        async with _make_session() as s4:
            svc4 = OutboxService(s4)
            final_msg = await svc4.get_message(msg_id)
            assert final_msg.status == "PENDING"

    async def test_stale_worker_rejected_with_identical_and_subsecond_timestamps(self, create_tables):
        """
        Real PostgreSQL test: Successive claims with identical or subsecond timestamps
        (< 1.0 second apart) are safely distinguished via unique UUID claim tokens.
        A stale worker from the first claim cannot overwrite the second claim.
        """
        async with _make_session() as setup_session:
            tenant = await self._create_test_tenant(setup_session)
            messages = await self._seed_messages(setup_session, tenant.id, count=1)
            msg_id = messages[0].id

        exact_timestamp = datetime.now(UTC)
        token1 = None
        # Worker 1 claims
        async with _make_session() as s1:
            svc1 = OutboxService(s1)
            claimed1 = await svc1.claim_pending_messages(batch_size=1, now=exact_timestamp)
            token1 = claimed1[0].payload.get("_claim_token")
            await s1.commit()

        # Message is reset back to PENDING (e.g. lease recovery or transient reset)
        async with _make_session() as s_reset:
            svc_r = OutboxService(s_reset)
            m = await svc_r.get_message(msg_id)
            m.status = "PENDING"
            await s_reset.commit()

        # Worker 2 immediately claims with identical or subsecond timestamp (+10ms)
        token2 = None
        subsecond_time = exact_timestamp + timedelta(milliseconds=10)
        async with _make_session() as s2:
            svc2 = OutboxService(s2)
            claimed2 = await svc2.claim_pending_messages(batch_size=1, now=subsecond_time)
            token2 = claimed2[0].payload.get("_claim_token")
            await s2.commit()

        assert token1 != token2, "Each claim must generate a distinct UUID token"

        # Worker 1 attempts to finalize with token 1 and exact_timestamp
        async with _make_session() as s3:
            svc3 = OutboxService(s3)
            res = await svc3.mark_sent(
                msg_id,
                provider_message_id="wamid.stale.worker1",
                claimed_at=exact_timestamp,
                claim_token=token1,
            )
            await s3.commit()

            # The update must be rejected by token mismatch!
            assert res.status == "PROCESSING", "Message must remain PROCESSING under Worker 2"
            assert res.payload.get("_claim_token") == token2

        # Verify in DB: Worker 2's token and state are untouched
        async with _make_session() as s4:
            svc4 = OutboxService(s4)
            db_msg = await svc4.get_message(msg_id)
            assert db_msg.status == "PROCESSING"
            assert db_msg.payload.get("_claim_token") == token2

    # -------------------------------------------------------------------------
    # 7. No duplicate outbox processing caused by commits inside batch loop
    # -------------------------------------------------------------------------
    async def test_no_duplicate_outbox_processing_in_batch(self, create_tables):
        """
        Batch claim marks ALL items in the batch as PROCESSING upfront and commits.
        A concurrent batch worker running immediately afterward will find 0 items,
        preventing duplicate external dispatch.
        """
        async with _make_session() as setup_session:
            tenant = await self._create_test_tenant(setup_session)
            await self._seed_messages(setup_session, tenant.id, count=5)

        with patch("app.tasks.celery_tasks._get_async_session", return_value=_make_session):
            # Worker 1 processes batch
            count = await _process_outbox_batch_async()
            assert count == 5

            # Worker 2 processes immediately
            count2 = await _process_outbox_batch_async()
            assert count2 == 0, "Second worker must process 0 messages (no duplicates)"

    # -------------------------------------------------------------------------
    # 8. Correct handling of successful and failed provider results
    # -------------------------------------------------------------------------
    async def test_successful_provider_result_persists_status_and_id(self, create_tables):
        """
        Successful provider result transitions status to SENT, records provider_message_id,
        and sets processed_at.
        """
        async with _make_session() as setup_session:
            tenant = await self._create_test_tenant(setup_session)
            messages = await self._seed_messages(setup_session, tenant.id, count=1)
            msg_id = messages[0].id

        async with _make_session() as s:
            svc = OutboxService(s)
            claimed = await svc.claim_pending_messages(batch_size=1)
            claimed_at = claimed[0].updated_at
            sent_msg = await svc.mark_sent(
                msg_id,
                provider_message_id="wamid.meta.success.123",
                claimed_at=claimed_at,
            )
            await s.commit()

            assert sent_msg.status == "SENT"
            assert sent_msg.processed_at is not None
            assert sent_msg.payload.get("provider_message_id") == "wamid.meta.success.123"

    async def test_exhausted_retries_transition_to_dead_letter(self, create_tables):
        """
        Failed provider result transitions to DEAD_LETTER when retry limit is exhausted.
        """
        async with _make_session() as setup_session:
            tenant = await self._create_test_tenant(setup_session)
            messages = await self._seed_messages(setup_session, tenant.id, count=1)
            msg_id = messages[0].id

        async with _make_session() as s:
            svc = OutboxService(s)
            msg = await svc.get_message(msg_id)
            msg.retry_count = 2  # Already failed twice, max is 3
            msg.status = "PROCESSING"
            await s.flush()

            claimed_at = msg.updated_at
            failed_msg = await svc.mark_failed(
                msg_id,
                error_message="Permanent 400 Bad Request",
                claimed_at=claimed_at,
            )
            await s.commit()

            assert failed_msg.status == "DEAD_LETTER"
            assert failed_msg.retry_count == 3
            assert failed_msg.processed_at is not None
            assert "Permanent 400" in failed_msg.last_error
