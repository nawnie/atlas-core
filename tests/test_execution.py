from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

from atlas_core import (
    ActionExecutionCoordinator,
    ActionExecutionResult,
    ApprovalDecision,
    AtlasCore,
)
from atlas_core.adapters import HotelAdapter
from atlas_core.models import ApprovalStatus


class SyntheticHotelExecutor:
    executor_id = "synthetic-hotel"
    action_types = frozenset({"review_cancelled_reservation"})
    supports_rollback = True

    def __init__(self, *, uncertain: bool = False) -> None:
        self.uncertain = uncertain
        self.executed = []
        self.rolled_back = []

    def execute(self, request):
        self.executed.append(request)
        if self.uncertain:
            raise TimeoutError("synthetic provider response was lost")
        return ActionExecutionResult(
            succeeded=True,
            summary="Synthetic cancellation review was recorded.",
            external_reference=f"synthetic:{request.idempotency_key}",
        )

    def rollback(self, request, receipt):
        self.rolled_back.append((request, receipt))
        return ActionExecutionResult(
            succeeded=True,
            summary="Synthetic cancellation review was reversed.",
            external_reference=receipt.external_reference,
        )


class BlockingHotelExecutor(SyntheticHotelExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()

    def execute(self, request):
        self.executed.append(request)
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test did not release executor")
        return ActionExecutionResult(
            succeeded=True,
            summary="Concurrent synthetic action completed once.",
            external_reference=request.idempotency_key,
        )

class ExecutionOutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "company.db"
        self.engine = AtlasCore(self.database, (HotelAdapter(),))
        self.engine.ingest(
            "hotel",
            {
                "reservation_id": "EXEC-1",
                "updated_at": "2026-07-24T12:00:00Z",
                "arrival_date": "2026-08-01",
                "departure_date": "2026-08-03",
                "guest_count": 2,
                "room_type": "king",
                "status": "confirmed",
                "total": "500.00",
                "currency": "USD",
            },
            tenant_id="hotel-execution",
            source_system="synthetic-pms",
        )
        changed = self.engine.ingest(
            "hotel",
            {
                "reservation_id": "EXEC-1",
                "updated_at": "2026-07-24T13:00:00Z",
                "arrival_date": "2026-08-01",
                "departure_date": "2026-08-03",
                "guest_count": 2,
                "room_type": "king",
                "status": "cancelled",
                "total": "0.00",
                "currency": "USD",
            },
            tenant_id="hotel-execution",
            source_system="synthetic-pms",
        )
        self.proposal_id = changed.proposals[0].id
        assert self.proposal_id

    def tearDown(self) -> None:
        self.temp.cleanup()

    def approve(self) -> None:
        self.engine.memory.decide(
            ApprovalDecision(
                proposal_id=self.proposal_id,
                status=ApprovalStatus.APPROVED,
                reason="Synthetic execution fixture reviewed.",
                decided_by="test-reviewer",
            ),
            expected_tenant_id="hotel-execution",
        )

    def coordinator(self, executor=None):
        executor = executor or SyntheticHotelExecutor()
        return ActionExecutionCoordinator(self.engine.memory, (executor,)), executor

    def queue(self, coordinator):
        return coordinator.queue(
            self.proposal_id,
            tenant_id="hotel-execution",
            executor_id="synthetic-hotel",
            requested_by="test-operator",
            confirmation=(
                f"QUEUE EXECUTION {self.proposal_id} WITH synthetic-hotel"
            ),
        )

    def test_only_approved_proposal_can_be_queued_with_exact_confirmation(self) -> None:
        coordinator, _ = self.coordinator()
        with self.assertRaisesRegex(ValueError, "confirmation"):
            coordinator.queue(
                self.proposal_id,
                tenant_id="hotel-execution",
                executor_id="synthetic-hotel",
                requested_by="test-operator",
                confirmation="yes",
            )
        with self.assertRaisesRegex(ValueError, "human-approved"):
            self.queue(coordinator)
        self.approve()
        receipt = self.queue(coordinator)
        self.assertEqual(receipt.status, "queued")
        self.assertTrue(receipt.idempotency_key.startswith("idem_"))
        self.assertNotIn("payload", receipt.__dict__)
        replay = self.queue(coordinator)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.execution_id, receipt.execution_id)

    def test_execute_is_idempotent_and_rollback_is_separately_confirmed(self) -> None:
        self.approve()
        coordinator, executor = self.coordinator()
        queued = self.queue(coordinator)
        completed = coordinator.run(
            queued.execution_id,
            tenant_id="hotel-execution",
            confirmation=f"EXECUTE {queued.execution_id}",
        )
        self.assertEqual(completed.status, "succeeded")
        self.assertEqual(completed.attempt_count, 1)
        self.assertEqual(len(executor.executed), 1)
        self.assertEqual(
            executor.executed[0].idempotency_key,
            completed.idempotency_key,
        )
        replay = coordinator.run(
            queued.execution_id,
            tenant_id="hotel-execution",
            confirmation=f"EXECUTE {queued.execution_id}",
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(len(executor.executed), 1)
        with self.assertRaisesRegex(ValueError, "confirmation"):
            coordinator.rollback(
                queued.execution_id,
                tenant_id="hotel-execution",
                confirmation="rollback",
            )
        rolled_back = coordinator.rollback(
            queued.execution_id,
            tenant_id="hotel-execution",
            confirmation=f"ROLLBACK EXECUTION {queued.execution_id}",
        )
        self.assertEqual(rolled_back.status, "rolled_back")
        self.assertEqual(rolled_back.rollback_status, "succeeded")
        self.assertEqual(len(executor.rolled_back), 1)

    def test_ambiguous_executor_failure_is_not_automatically_retried(self) -> None:
        self.approve()
        coordinator, executor = self.coordinator(
            SyntheticHotelExecutor(uncertain=True)
        )
        queued = self.queue(coordinator)
        uncertain = coordinator.run(
            queued.execution_id,
            tenant_id="hotel-execution",
            confirmation=f"EXECUTE {queued.execution_id}",
        )
        self.assertEqual(uncertain.status, "uncertain")
        self.assertEqual(len(executor.executed), 1)
        with self.assertRaisesRegex(ValueError, "reconciled"):
            coordinator.run(
                queued.execution_id,
                tenant_id="hotel-execution",
                confirmation=f"EXECUTE {queued.execution_id}",
            )
        reconciled = coordinator.reconcile(
            queued.execution_id,
            tenant_id="hotel-execution",
            outcome="succeeded",
            note="Verified synthetic provider receipt out of band.",
            reconciled_by="test-reviewer",
            external_reference="synthetic:reconciled",
            confirmation=(
                f"RECONCILE EXECUTION {queued.execution_id} AS SUCCEEDED"
            ),
        )
        self.assertEqual(reconciled.status, "succeeded")
        self.assertEqual(len(executor.executed), 1)

    def test_tenant_scope_and_action_allowlist_are_enforced(self) -> None:
        self.approve()
        coordinator, _ = self.coordinator()
        with self.assertRaises(KeyError):
            coordinator.queue(
                self.proposal_id,
                tenant_id="another-tenant",
                executor_id="synthetic-hotel",
                requested_by="test-operator",
                confirmation=(
                    f"QUEUE EXECUTION {self.proposal_id} WITH synthetic-hotel"
                ),
            )
        disallowed = SyntheticHotelExecutor()
        disallowed.action_types = frozenset({"different_action"})
        coordinator = ActionExecutionCoordinator(self.engine.memory, (disallowed,))
        with self.assertRaisesRegex(ValueError, "does not allow"):
            self.queue(coordinator)

    def test_execution_receipts_and_audit_are_payload_free(self) -> None:
        self.approve()
        coordinator, _ = self.coordinator()
        queued = self.queue(coordinator)
        coordinator.run(
            queued.execution_id,
            tenant_id="hotel-execution",
            confirmation=f"EXECUTE {queued.execution_id}",
        )
        listed = coordinator.list(tenant_id="hotel-execution")
        self.assertEqual(len(listed), 1)
        event_details = [
            event["detail"]
            for event in self.engine.memory.audit_events()
            if event["event_type"].startswith("execution.")
        ]
        self.assertTrue(event_details)
        for detail in event_details:
            self.assertNotIn("payload", detail)
            self.assertNotIn("reservation_id", str(detail))
        overview = self.engine.memory.overview(tenant_id="hotel-execution")
        self.assertEqual(overview["executions"], 1)
        self.assertEqual(overview["execution_attention"], 0)

    def test_removed_executor_becomes_definite_failure_not_stuck_running(self) -> None:
        self.approve()
        coordinator, _ = self.coordinator()
        queued = self.queue(coordinator)
        unavailable = ActionExecutionCoordinator(self.engine.memory)
        failed = unavailable.run(
            queued.execution_id,
            tenant_id="hotel-execution",
            confirmation=f"EXECUTE {queued.execution_id}",
        )
        self.assertEqual(failed.status, "failed")
        self.assertIn("not registered", failed.error)
        self.assertNotEqual(failed.status, "running")

    def test_transactional_claim_prevents_concurrent_provider_calls(self) -> None:
        self.approve()
        executor = BlockingHotelExecutor()
        coordinator, _ = self.coordinator(executor)
        queued = self.queue(coordinator)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                coordinator.run,
                queued.execution_id,
                tenant_id="hotel-execution",
                confirmation=f"EXECUTE {queued.execution_id}",
            )
            self.assertTrue(executor.started.wait(timeout=5))
            second = pool.submit(
                coordinator.run,
                queued.execution_id,
                tenant_id="hotel-execution",
                confirmation=f"EXECUTE {queued.execution_id}",
            )
            with self.assertRaisesRegex(ValueError, "cannot run from running"):
                second.result(timeout=5)
            executor.release.set()
            completed = first.result(timeout=5)
        self.assertEqual(completed.status, "succeeded")
        self.assertEqual(len(executor.executed), 1)

    def test_running_state_survives_for_manual_crash_reconciliation(self) -> None:
        self.approve()
        coordinator, _ = self.coordinator()
        queued = self.queue(coordinator)
        _, claimed = self.engine.memory.claim_execution(
            execution_id=queued.execution_id,
            tenant_id="hotel-execution",
        )
        self.assertEqual(claimed.status, "running")
        with self.assertRaisesRegex(ValueError, "cannot run from running"):
            coordinator.run(
                queued.execution_id,
                tenant_id="hotel-execution",
                confirmation=f"EXECUTE {queued.execution_id}",
            )
        listed = coordinator.list(tenant_id="hotel-execution")
        self.assertEqual(listed[0].status, "running")


if __name__ == "__main__":
    unittest.main()
