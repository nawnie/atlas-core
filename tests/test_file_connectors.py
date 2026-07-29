from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from atlas_core.adapters import HealthcareAdapter, HotelAdapter, ManufacturingAdapter
from atlas_core.file_connectors import MAX_FILE_BYTES, preview_file
from atlas_core_sdk import AtlasClient, InvalidRequestError


class FileConnectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.client = AtlasClient.local(
            database=Path(self.temp.name) / "files.db",
            adapters=(HotelAdapter(), ManufacturingAdapter(), HealthcareAdapter()),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def hotel_jsonl() -> bytes:
        base = {
            "reservation_id": "FILE-1",
            "updated_at": "2026-07-24T20:00:00Z",
            "arrival_date": "2026-08-10",
            "departure_date": "2026-08-13",
            "guest_count": 2,
            "room_type": "king",
            "status": "confirmed",
            "total": "729.00",
            "currency": "USD",
        }
        rows = [
            {"record_id": "FILE-1", "version": "1", "payload": base},
            {
                "record_id": "FILE-1",
                "version": "2",
                "payload": {
                    **base,
                    "updated_at": "2026-07-24T20:05:00Z",
                    "status": "cancelled",
                    "total": "0",
                },
            },
        ]
        return ("\n".join(json.dumps(row) for row in rows) + "\n").encode()

    def test_jsonl_preview_and_sdk_import_are_replay_safe(self) -> None:
        content = self.hotel_jsonl()
        preview = self.client.preview_file_import(
            content,
            format="jsonl",
            source_name="hotel-export.jsonl",
        )
        self.assertEqual(preview.record_count, 2)
        self.assertEqual(preview.records[1].payload["status"], "cancelled")
        self.assertTrue(preview.external_batch_id.startswith("file_"))
        first = self.client.import_file(
            content,
            format="jsonl",
            source_name="hotel-export.jsonl",
            tenant_id="file-hotel",
            connector_id="hotel-file-drop",
            domain="hotel",
            source_system="nightly-hotel-export",
        )
        replay = self.client.import_file(
            content,
            format="jsonl",
            source_name="hotel-export.jsonl",
            tenant_id="file-hotel",
            connector_id="hotel-file-drop",
            domain="hotel",
            source_system="nightly-hotel-export",
        )
        self.assertEqual(first.imported_count, 2)
        self.assertTrue(replay.replayed)
        self.assertEqual(
            self.client.overview(tenant_id="file-hotel").cartography_cards,
            2,
        )

    def test_csv_coerces_json_scalars_for_typed_adapters(self) -> None:
        content = (
            "record_id,version,work_order_id,updated_at,line_id,sku,status,"
            "quality_hold,quantity_planned,quantity_completed,defect_count,"
            "downtime_minutes\n"
            "WO-1,1,WO-1,2026-07-24T20:00:00Z,LINE-2,PART-9,running,"
            "false,100,20,0,0\n"
            "WO-1,2,WO-1,2026-07-24T20:05:00Z,LINE-2,PART-9,quality_hold,"
            "true,100,20,2,10\n"
        ).encode()
        preview = self.client.preview_file_import(
            content,
            format="csv",
            source_name="manufacturing.csv",
        )
        self.assertIs(preview.records[0].payload["quality_hold"], False)
        self.assertEqual(preview.records[0].payload["quantity_planned"], 100)
        receipt = self.client.import_file(
            content,
            format="csv",
            source_name="manufacturing.csv",
            tenant_id="factory-file",
            connector_id="manufacturing-file-drop",
            domain="manufacturing",
            source_system="mes-csv-export",
        )
        self.assertEqual(receipt.imported_count, 2)
        self.assertEqual(
            len(self.client.proposals(tenant_id="factory-file", status="pending")),
            1,
        )

    def test_healthcare_file_rejection_rolls_back_batch(self) -> None:
        row = {
            "record_id": "AUTH-1",
            "version": "1",
            "payload": {
                "authorization_id": "AUTH-1",
                "updated_at": "2026-07-24T20:00:00Z",
                "patient_ref": "opaque-1",
                "status": "pending",
                "service_type": "administrative",
                "documentation_complete": False,
                "patient_name": "Must Not Persist",
            },
        }
        with self.assertRaises(InvalidRequestError):
            self.client.import_file(
                (json.dumps(row) + "\n").encode(),
                format="jsonl",
                source_name="healthcare.jsonl",
                tenant_id="health-file",
                connector_id="health-file-drop",
                domain="healthcare",
                source_system="admin-export",
            )
        self.assertEqual(
            self.client.overview(tenant_id="health-file").observations,
            0,
        )
        receipts = self.client.imports(tenant_id="health-file")
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0].status, "failed")

    def test_untrusted_file_bounds_and_shapes_are_rejected(self) -> None:
        cases = (
            (b"\xff", "jsonl", "valid.jsonl"),
            (b"\x00{}", "jsonl", "valid.jsonl"),
            (b"record_id,record_id,version,x\n1,1,1,x\n", "csv", "valid.csv"),
            (b"record_id,version\n1,1\n", "csv", "valid.csv"),
            (b"{}\n", "jsonl", "valid.jsonl"),
        )
        for content, format, name in cases:
            with self.subTest(content=content[:20], format=format):
                with self.assertRaises(ValueError):
                    preview_file(content, format=format, source_name=name)
        with self.assertRaisesRegex(ValueError, "plain filename"):
            preview_file(
                self.hotel_jsonl(),
                format="jsonl",
                source_name="../secret.jsonl",
            )
        with self.assertRaisesRegex(ValueError, "exceed"):
            preview_file(
                b"x" * (MAX_FILE_BYTES + 1),
                format="jsonl",
                source_name="large.jsonl",
            )

    def test_cli_is_preview_first_and_apply_is_explicit(self) -> None:
        source = Path(self.temp.name) / "hotel.jsonl"
        source.write_bytes(self.hotel_jsonl())
        database = Path(self.temp.name) / "cli.db"
        base_command = [
            sys.executable,
            "-m",
            "atlas_core.cli",
            "import-file",
            str(source),
            "--format",
            "jsonl",
            "--domain",
            "hotel",
            "--tenant",
            "cli-hotel",
            "--connector-id",
            "cli-file-drop",
            "--source-system",
            "cli-export",
            "--database",
            str(database),
        ]
        environment = dict(os.environ)
        preview = subprocess.run(
            base_command,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        preview_payload = json.loads(preview.stdout)
        self.assertEqual(preview_payload["mode"], "preview")
        self.assertFalse(database.exists())
        applied = subprocess.run(
            [*base_command, "--apply"],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        applied_payload = json.loads(applied.stdout)
        self.assertEqual(applied_payload["receipt"]["imported_count"], 2)
        self.assertTrue(database.exists())


if __name__ == "__main__":
    unittest.main()
