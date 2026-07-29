from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from atlas_core import (
    ActionExecutionCoordinator,
    ApprovalDecision,
    AtlasCore,
    HotelPmsSandbox,
)
from atlas_core.adapters import HotelAdapter
from atlas_core.models import ApprovalStatus


class HotelPmsSandboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.engine = AtlasCore(root / "atlas.db", (HotelAdapter(),))
        self.provider = HotelPmsSandbox(root / "hotel-sandbox.db")
        self.tenant = "hotel-sandbox-test"
        base, cancelled = self.provider.seed_cancelled_reservation(
            tenant_id=self.tenant,
            reservation_id="SANDBOX-101",
        )
        self.engine.ingest(
            "hotel",
            base,
            tenant_id=self.tenant,
            source_system="hotel-pms-sandbox",
        )
        result = self.engine.ingest(
            "hotel",
            cancelled,
            tenant_id=self.tenant,
            source_system="hotel-pms-sandbox",
        )
        self.proposal_id = result.proposals[0].id
        assert self.proposal_id
        self.engine.memory.decide(
            ApprovalDecision(
                proposal_id=self.proposal_id,
                status=ApprovalStatus.APPROVED,
                reason="Synthetic cancellation verified.",
                decided_by="sandbox-reviewer",
            ),
            expected_tenant_id=self.tenant,
        )
        self.coordinator = ActionExecutionCoordinator(
            self.engine.memory, (self.provider,)
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _queue(self):
        return self.coordinator.queue(
            self.proposal_id,
            tenant_id=self.tenant,
            executor_id=self.provider.executor_id,
            requested_by="sandbox-operator",
            confirmation=(
                f"QUEUE EXECUTION {self.proposal_id} "
                f"WITH {self.provider.executor_id}"
            ),
        )

    def test_full_cancellation_execution_and_rollback_changes_provider_state(
        self,
    ) -> None:
        queued = self._queue()
        completed = self.coordinator.run(
            queued.execution_id,
            tenant_id=self.tenant,
            confirmation=f"EXECUTE {queued.execution_id}",
        )
        self.assertEqual(completed.status, "succeeded")
        self.assertTrue(completed.external_reference.startswith("hotel-sandbox:"))
        reservation = self.provider.reservations(tenant_id=self.tenant)[0]
        self.assertEqual(reservation.cancellation_review_status, "completed")
        self.assertEqual(reservation.inventory_status, "released")

        rolled_back = self.coordinator.rollback(
            queued.execution_id,
            tenant_id=self.tenant,
            confirmation=f"ROLLBACK EXECUTION {queued.execution_id}",
        )
        self.assertEqual(rolled_back.status, "rolled_back")
        reservation = self.provider.reservations(tenant_id=self.tenant)[0]
        self.assertEqual(reservation.cancellation_review_status, "pending")
        self.assertEqual(reservation.inventory_status, "held")

    def test_provider_idempotency_survives_a_new_provider_instance(self) -> None:
        queued = self._queue()
        request, _ = self.engine.memory.claim_execution(
            execution_id=queued.execution_id,
            tenant_id=self.tenant,
        )
        first = self.provider.execute(request)
        restarted = HotelPmsSandbox(self.provider.database)
        replay = restarted.execute(request)
        self.assertTrue(first.succeeded)
        self.assertTrue(replay.succeeded)
        self.assertEqual(first.external_reference, replay.external_reference)
        reservation = restarted.reservations(tenant_id=self.tenant)[0]
        self.assertEqual(reservation.inventory_status, "released")

    def test_tenant_state_is_isolated_and_identity_mismatch_is_rejected(self) -> None:
        self.assertEqual(
            self.provider.reservations(tenant_id="other-hotel"),
            (),
        )
        queued = self._queue()
        request, _ = self.engine.memory.claim_execution(
            execution_id=queued.execution_id,
            tenant_id=self.tenant,
        )
        bad_request = request.__class__(
            **{
                **request.__dict__,
                "entity_id": "SANDBOX-OTHER",
            }
        )
        with self.assertRaisesRegex(ValueError, "identity"):
            self.provider.execute(bad_request)

    def test_sandbox_has_no_endpoint_or_credential_configuration(self) -> None:
        self.assertTrue(self.provider.is_sandbox)
        self.assertFalse(hasattr(self.provider, "base_url"))
        self.assertFalse(hasattr(self.provider, "api_key"))


if __name__ == "__main__":
    unittest.main()
