from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from atlas_core import AtlasAutomationRunner, AutomationStore, load_automation_config


class AutomationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "company.db"
        self.inbox = self.root / "hotel-inbox"
        self.config = self.root / "automation.json"
        self.now = datetime(2026, 7, 24, 20, 0, tzinfo=UTC)
        self.write_config()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_config(
        self,
        *,
        apply_retention: bool = False,
        keep_last: int = 2,
    ) -> None:
        self.config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "inboxes": [
                        {
                            "job_id": "hotel-nightly",
                            "tenant_id": "hotel-company",
                            "domain": "hotel",
                            "connector_id": "hotel-inbox",
                            "source_system": "approved-pms-export",
                            "path": str(self.inbox),
                        }
                    ],
                    "backup": {
                        "enabled": True,
                        "interval_minutes": 15,
                        "keep_last": keep_last,
                        "apply_retention": apply_retention,
                    },
                }
            ),
            encoding="utf-8",
        )

    @staticmethod
    def hotel_file() -> bytes:
        base = {
            "reservation_id": "AUTO-1",
            "updated_at": "2026-07-24T20:00:00Z",
            "arrival_date": "2026-08-10",
            "departure_date": "2026-08-13",
            "guest_count": 2,
            "room_type": "king",
            "status": "confirmed",
            "total": "729.00",
            "currency": "USD",
        }
        rows = (
            {"record_id": "AUTO-1", "version": "1", "payload": base},
            {
                "record_id": "AUTO-1",
                "version": "2",
                "payload": {
                    **base,
                    "updated_at": "2026-07-24T20:05:00Z",
                    "status": "cancelled",
                    "total": "0.00",
                },
            },
        )
        return ("\n".join(json.dumps(row) for row in rows) + "\n").encode()

    def runner(self) -> AtlasAutomationRunner:
        return AtlasAutomationRunner(
            database=self.database,
            config_path=self.config,
            clock=lambda: self.now,
        )

    def test_inbox_import_rejection_replay_and_backup_receipts(self) -> None:
        self.inbox.mkdir()
        valid = self.inbox / "hotel.jsonl"
        invalid = self.inbox / "invalid.csv"
        valid.write_bytes(self.hotel_file())
        invalid.write_text("record_id,version\nx,1\n", encoding="utf-8")

        first = self.runner().run_once()
        self.assertEqual(first.status, "attention_required")
        self.assertEqual(first.imported_files, 1)
        self.assertEqual(first.rejected_files, 1)
        self.assertTrue(first.backup_created)
        processed = tuple((self.inbox / "processed").glob("*.jsonl"))
        rejected = tuple((self.inbox / "rejected").glob("*.csv"))
        self.assertEqual(len(processed), 1)
        self.assertEqual(len(rejected), 1)
        self.assertFalse(valid.exists())
        self.assertFalse(invalid.exists())
        serialized = json.dumps(
            [event.__dict__ for event in first.events],
            default=str,
        )
        self.assertNotIn("Synthetic", serialized)
        self.assertNotIn("guest_name", serialized)

        replay_source = self.inbox / "hotel-replay.jsonl"
        replay_source.write_bytes(processed[0].read_bytes())
        second = self.runner().run_once()
        self.assertEqual(second.status, "completed")
        self.assertEqual(second.replayed_files, 1)
        self.assertFalse(second.backup_created)
        self.assertFalse(replay_source.exists())
        status = AutomationStore(self.database).status(config_path=self.config)
        self.assertTrue(status.configured)
        self.assertFalse(status.locked)
        self.assertEqual(status.last_run.run_id, second.run_id)
        self.assertGreaterEqual(len(status.recent_events), 3)

    def test_explicit_automatic_retention_keeps_two_verified_backups(self) -> None:
        self.write_config(apply_retention=True, keep_last=2)
        runner = self.runner()
        first = runner.run_once()
        self.now += timedelta(minutes=16)
        second = runner.run_once()
        self.now += timedelta(minutes=16)
        third = runner.run_once()
        self.assertTrue(first.backup_created)
        self.assertTrue(second.backup_created)
        self.assertTrue(third.backup_created)
        backups = tuple(
            path for path in Path(f"{self.database}.backups").glob("bkp_*.json")
        )
        self.assertEqual(len(backups), 2)
        backup_event = next(
            event for event in third.events if event.event_type == "backup.create"
        )
        self.assertEqual(len(backup_event.detail["retention_deleted"]), 1)

    def test_lock_prevents_overlapping_runner(self) -> None:
        store = AutomationStore(self.database)
        store.acquire(config_path=self.config)
        try:
            with self.assertRaisesRegex(RuntimeError, "Another Atlas automation"):
                self.runner().run_once()
        finally:
            store.release()
        self.assertFalse(store.lock_path.exists())

    def test_config_validation_rejects_unsafe_or_ambiguous_values(self) -> None:
        payload = json.loads(self.config.read_text(encoding="utf-8"))
        payload["inboxes"].append(dict(payload["inboxes"][0]))
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Duplicate inbox job_id"):
            load_automation_config(self.config)

        payload["inboxes"] = []
        payload["backup"]["keep_last"] = 1
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "between 2 and 1000"):
            load_automation_config(self.config)

    def test_cli_demo_regression_and_automation_status(self) -> None:
        environment = dict(os.environ)
        demo_database = self.root / "demo.db"
        demo = subprocess.run(
            [
                sys.executable,
                "-m",
                "atlas_core.cli",
                "demo",
                "--database",
                str(demo_database),
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        demo_payload = json.loads(demo.stdout)
        self.assertEqual(
            demo_payload["answer"]["what_changed"],
            "2 field(s) changed: status, total.",
        )
        status = subprocess.run(
            [
                sys.executable,
                "-m",
                "atlas_core.cli",
                "automation-status",
                "--config",
                str(self.config),
                "--database",
                str(self.database),
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertTrue(json.loads(status.stdout)["configured"])


if __name__ == "__main__":
    unittest.main()
