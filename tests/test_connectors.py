from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path

from atlas_core.adapters import ApartmentAdapter, HotelAdapter
from atlas_core.memory import CompanyMemory
from atlas_core_sdk import AtlasClient, InvalidRequestError


class ConnectorImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "connectors.db"
        self.client = AtlasClient.local(
            database=self.database,
            adapters=(HotelAdapter(), ApartmentAdapter()),
        )
        self.base = {
            "lease_id": "L-101",
            "updated_at": "2026-07-24T13:00:00Z",
            "unit_id": "A-14",
            "resident_count": 2,
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
            "rent": "1800",
            "balance_due": "0",
            "status": "active",
            "currency": "USD",
        }
        self.records = (
            {"record_id": "L-101", "version": "1", "payload": self.base},
            {
                "record_id": "L-101",
                "version": "2",
                "payload": {
                    **self.base,
                    "updated_at": "2026-07-24T13:05:00Z",
                    "balance_due": "900",
                    "status": "delinquent",
                },
            },
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def import_batch(
        self,
        external_batch_id: str,
        records: tuple[dict[str, object], ...] | None = None,
        tenant_id: str = "apartment-company",
    ):
        return self.client.import_batch(
            tenant_id=tenant_id,
            connector_id="property-feed",
            domain="apartment",
            source_system="fixture-property-manager",
            external_batch_id=external_batch_id,
            records=records or self.records,
        )

    def test_apartment_import_creates_receipt_change_proposal_and_maps(self) -> None:
        receipt = self.import_batch("batch-001")
        self.assertEqual(receipt.status, "completed")
        self.assertEqual(receipt.imported_count, 2)
        self.assertEqual(receipt.duplicate_count, 0)
        self.assertEqual([item.status for item in receipt.records], ["imported", "imported"])
        proposals = self.client.proposals(
            tenant_id="apartment-company",
            status="pending",
        )
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].action_type, "review_delinquent_lease")
        self.assertEqual(proposals[0].payload["balance_due"], "900.00")
        cards = self.client.search_cartography(
            tenant_id="apartment-company",
            lane="apartment_operations",
        )
        self.assertEqual(len(cards), 2)
        self.assertIn('"balance_due": "attributes.balance_due"', cards[0].details)
        overview = self.client.overview(tenant_id="apartment-company")
        self.assertEqual(overview.import_batches, 1)
        self.assertEqual(overview.observations, 2)

    def test_exact_batch_replay_returns_original_without_duplicate_writes(self) -> None:
        first = self.import_batch("batch-replay")
        replay = self.import_batch("batch-replay")
        self.assertEqual(replay.batch_id, first.batch_id)
        self.assertTrue(replay.replayed)
        overview = self.client.overview(tenant_id="apartment-company")
        self.assertEqual(overview.observations, 2)
        self.assertEqual(overview.cartography_cards, 2)
        self.assertEqual(overview.proposals, 1)
        event_types = {
            event.event_type
            for event in self.client.audit_events(tenant_id="apartment-company")
        }
        self.assertIn("import.replayed", event_types)

    def test_new_batch_deduplicates_records_across_batches(self) -> None:
        self.import_batch("batch-first")
        duplicate = self.import_batch("batch-second")
        self.assertEqual(duplicate.imported_count, 0)
        self.assertEqual(duplicate.duplicate_count, 2)
        self.assertTrue(all(item.status == "duplicate" for item in duplicate.records))
        overview = self.client.overview(tenant_id="apartment-company")
        self.assertEqual(overview.import_batches, 2)
        self.assertEqual(overview.observations, 2)

    def test_concurrent_exact_batch_is_committed_once_and_replayed_once(self) -> None:
        barrier = Barrier(2)

        def run() -> object:
            barrier.wait(timeout=5)
            return self.import_batch("batch-concurrent")

        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(lambda _: run(), range(2)))
        self.assertEqual(sum(receipt.replayed for receipt in receipts), 1)
        self.assertEqual({receipt.batch_id for receipt in receipts}, {receipts[0].batch_id})
        overview = self.client.overview(tenant_id="apartment-company")
        self.assertEqual(overview.import_batches, 1)
        self.assertEqual(overview.observations, 2)

    def test_batch_id_content_conflict_is_rejected(self) -> None:
        self.import_batch("batch-conflict")
        changed = (
            {
                **self.records[0],
                "payload": {**self.base, "rent": "1900"},
            },
        )
        with self.assertRaisesRegex(InvalidRequestError, "different content"):
            self.import_batch("batch-conflict", changed)

    def test_failed_batch_rolls_back_all_business_writes_and_keeps_receipt(self) -> None:
        bad_records = (
            self.records[0],
            {
                "record_id": "L-BAD",
                "version": "1",
                "payload": {
                    "updated_at": "2026-07-24T13:06:00Z",
                    "unit_id": "A-15",
                },
            },
        )
        with self.assertRaises(InvalidRequestError):
            self.import_batch("batch-atomic-failure", bad_records)
        overview = self.client.overview(tenant_id="apartment-company")
        self.assertEqual(overview.observations, 0)
        self.assertEqual(overview.changes, 0)
        self.assertEqual(overview.proposals, 0)
        self.assertEqual(overview.cartography_cards, 0)
        receipts = self.client.imports(tenant_id="apartment-company")
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0].status, "failed")
        self.assertIn("lease_id", receipts[0].error)

    def test_import_receipts_are_tenant_scoped(self) -> None:
        self.import_batch("batch-tenant")
        self.assertEqual(self.client.imports(tenant_id="other-company"), ())

    def test_schema_two_migrates_additively_to_four(self) -> None:
        legacy = Path(self.temp.name) / "schema-two.db"
        memory = CompanyMemory(legacy)
        with closing(sqlite3.connect(legacy)) as db:
            db.execute("UPDATE schema_meta SET version = 2")
            db.commit()
        CompanyMemory(legacy)
        with closing(sqlite3.connect(legacy)) as db:
            self.assertEqual(db.execute("SELECT version FROM schema_meta").fetchone()[0], 4)
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        self.assertIn("import_batches", tables)
        self.assertIn("connector_record_ledger", tables)


if __name__ == "__main__":
    unittest.main()
