from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from atlas_core.adapters import (
    ConstructionAdapter,
    HealthcareAdapter,
    ManufacturingAdapter,
)
from atlas_core_sdk import AtlasClient, InvalidRequestError


class IndustryAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.client = AtlasClient.local(
            database=Path(self.temp.name) / "industries.db",
            adapters=(
                ConstructionAdapter(),
                ManufacturingAdapter(),
                HealthcareAdapter(),
            ),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def import_versions(
        self,
        *,
        domain: str,
        record_id: str,
        base: dict[str, object],
        changed: dict[str, object],
    ):
        return self.client.import_batch(
            tenant_id=f"{domain}-company",
            connector_id=f"fixture-{domain}-feed",
            domain=domain,
            source_system=f"fixture-{domain}-system",
            external_batch_id=f"{domain}-batch-1",
            records=(
                {"record_id": record_id, "version": "1", "payload": base},
                {
                    "record_id": record_id,
                    "version": "2",
                    "payload": {**base, **changed},
                },
            ),
        )

    def assert_full_path(
        self,
        *,
        domain: str,
        action_type: str,
        expected_changed_fields: set[str],
    ) -> None:
        proposals = self.client.proposals(
            tenant_id=f"{domain}-company",
            status="pending",
        )
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].action_type, action_type)
        cards = self.client.search_cartography(
            tenant_id=f"{domain}-company",
            lane=f"{domain}_operations",
        )
        self.assertEqual(len(cards), 2)
        overview = self.client.overview(tenant_id=f"{domain}-company")
        self.assertEqual(overview.observations, 2)
        self.assertEqual(overview.changes, 2)
        self.assertEqual(overview.proposals, 1)
        self.assertEqual(overview.cartography_cards, 2)
        events = self.client.audit_events(tenant_id=f"{domain}-company")
        changed = [
            event
            for event in events
            if event.event_type == "change.detected"
            and event.detail["summary"].startswith(f"{len(expected_changed_fields)} field")
        ]
        self.assertEqual(len(changed), 1)
        for field in expected_changed_fields:
            self.assertIn(field, changed[0].detail["summary"])

    def test_construction_full_path(self) -> None:
        base = {
            "work_item_id": "SITE-9-HVAC",
            "updated_at": "2026-07-24T14:00:00Z",
            "project_id": "SITE-9",
            "trade": "HVAC",
            "status": "in_progress",
            "percent_complete": 60,
            "budget": "30000",
            "actual_cost": "19000",
            "safety_hold": False,
            "currency": "usd",
        }
        receipt = self.import_versions(
            domain="construction",
            record_id="SITE-9-HVAC",
            base=base,
            changed={
                "updated_at": "2026-07-24T14:05:00Z",
                "status": "on_hold",
                "budget": "30500",
                "actual_cost": "31000",
                "safety_hold": True,
            },
        )
        self.assertEqual(receipt.imported_count, 2)
        self.assert_full_path(
            domain="construction",
            action_type="review_construction_exception",
            expected_changed_fields={"status", "budget", "actual_cost", "safety_hold"},
        )

    def test_manufacturing_full_path(self) -> None:
        base = {
            "work_order_id": "WO-22",
            "updated_at": "2026-07-24T15:00:00Z",
            "line_id": "LINE-2",
            "sku": "PART-7",
            "status": "running",
            "quantity_planned": 100,
            "quantity_completed": 45,
            "defect_count": 1,
            "downtime_minutes": 0,
            "quality_hold": False,
        }
        receipt = self.import_versions(
            domain="manufacturing",
            record_id="WO-22",
            base=base,
            changed={
                "updated_at": "2026-07-24T15:05:00Z",
                "status": "quality_hold",
                "quantity_completed": 50,
                "defect_count": 7,
                "downtime_minutes": 12,
                "quality_hold": True,
            },
        )
        self.assertEqual(receipt.imported_count, 2)
        self.assert_full_path(
            domain="manufacturing",
            action_type="review_manufacturing_quality_exception",
            expected_changed_fields={
                "status",
                "quantity_completed",
                "defect_count",
                "downtime_minutes",
                "quality_hold",
            },
        )

    def test_healthcare_administrative_full_path(self) -> None:
        base = {
            "encounter_id": "ENC-OPAQUE-1",
            "updated_at": "2026-07-24T16:00:00Z",
            "patient_ref": "PAT-OPAQUE-1",
            "care_setting": "outpatient",
            "status": "scheduled",
            "authorization_status": "approved",
            "documentation_complete": True,
            "service_code": "SYNTH-100",
        }
        receipt = self.import_versions(
            domain="healthcare",
            record_id="ENC-OPAQUE-1",
            base=base,
            changed={
                "updated_at": "2026-07-24T16:05:00Z",
                "authorization_status": "more_information_required",
                "documentation_complete": False,
            },
        )
        self.assertEqual(receipt.imported_count, 2)
        self.assert_full_path(
            domain="healthcare",
            action_type="review_healthcare_authorization_exception",
            expected_changed_fields={
                "authorization_status",
                "documentation_complete",
            },
        )

    def test_healthcare_rejects_direct_phi_and_rolls_back_batch(self) -> None:
        record = {
            "encounter_id": "ENC-OPAQUE-2",
            "updated_at": "2026-07-24T16:00:00Z",
            "patient_ref": "PAT-OPAQUE-2",
            "patient_name": "Synthetic Person",
            "care_setting": "outpatient",
            "status": "scheduled",
            "authorization_status": "approved",
            "documentation_complete": True,
            "service_code": "SYNTH-101",
        }
        with self.assertRaisesRegex(InvalidRequestError, "direct PHI"):
            self.client.import_batch(
                tenant_id="healthcare-company",
                connector_id="fixture-healthcare-feed",
                domain="healthcare",
                source_system="fixture-healthcare-system",
                external_batch_id="healthcare-phi-rejection",
                records=(
                    {
                        "record_id": "ENC-OPAQUE-2",
                        "version": "1",
                        "payload": record,
                    },
                ),
            )
        overview = self.client.overview(tenant_id="healthcare-company")
        self.assertEqual(overview.observations, 0)
        receipts = self.client.imports(tenant_id="healthcare-company")
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0].status, "failed")


if __name__ == "__main__":
    unittest.main()
