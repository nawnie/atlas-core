from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from atlas_core import ApprovalDecision, AtlasCore
from atlas_core.adapters import HotelAdapter
from atlas_core.models import ApprovalStatus


class AtlasCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "atlas.db"
        self.engine = AtlasCore(self.database, (HotelAdapter(),))
        self.base = {
            "reservation_id": "R-1",
            "version": "1",
            "updated_at": "2026-07-24T12:00:00Z",
            "arrival_date": "2026-08-10",
            "departure_date": "2026-08-13",
            "guest_count": 2,
            "room_type": "king",
            "status": "confirmed",
            "total": "729",
            "currency": "usd",
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def ingest(self, record: dict[str, object]):
        return self.engine.ingest(
            "hotel",
            record,
            tenant_id="hotel-1",
            source_system="fixture-pms",
        )

    def test_first_ingestion_creates_memory_without_action(self) -> None:
        result = self.ingest(self.base)
        self.assertEqual(result.observation.attributes["total"], "729.00")
        self.assertEqual(result.proposals, ())
        self.assertIn("First observation", result.changes.summary)

    def test_cancellation_explains_change_and_requires_approval(self) -> None:
        self.ingest(self.base)
        changed = {
            **self.base,
            "version": "2",
            "updated_at": "2026-07-24T12:05:00Z",
            "status": "cancelled",
            "total": "0",
        }
        result = self.ingest(changed)
        fields = {change.field for change in result.changes.changes}
        self.assertEqual(fields, {"status", "total"})
        self.assertEqual(len(result.proposals), 1)
        proposal = result.proposals[0]
        self.assertEqual(proposal.action_type, "review_cancelled_reservation")
        events = self.engine.memory.audit_events()
        proposal_events = [
            event for event in events if event["event_type"] == "proposal.created"
        ]
        self.assertEqual(len(proposal_events), 1)
        self.assertEqual(proposal_events[0]["detail"]["status"], "pending")
        self.assertEqual(events[-1]["event_type"], "cartography.recorded")

    def test_human_can_approve_pending_proposal_once(self) -> None:
        self.ingest(self.base)
        result = self.ingest(
            {
                **self.base,
                "version": "2",
                "updated_at": "2026-07-24T12:05:00Z",
                "status": "cancelled",
            }
        )
        proposal_id = result.proposals[0].id
        assert proposal_id
        decision = ApprovalDecision(
            proposal_id=proposal_id,
            status=ApprovalStatus.APPROVED,
            reason="Manager confirmed source and refund terms.",
            decided_by="manager@example.test",
        )
        self.engine.memory.decide(decision)
        with self.assertRaises(ValueError):
            self.engine.memory.decide(decision)

    def test_invalid_record_is_rejected_before_storage(self) -> None:
        invalid = {**self.base}
        del invalid["reservation_id"]
        with self.assertRaisesRegex(ValueError, "reservation_id"):
            self.ingest(invalid)
        self.assertEqual(self.engine.memory.audit_events(), [])

    def test_unknown_adapter_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "No adapter"):
            self.engine.ingest(
                "manufacturing",
                {},
                tenant_id="factory-1",
                source_system="fixture",
            )


if __name__ == "__main__":
    unittest.main()
