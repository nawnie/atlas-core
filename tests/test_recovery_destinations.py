from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from atlas_core import AtlasAutomationRunner, AtlasCore, load_automation_config
from atlas_core.adapters import HotelAdapter
from atlas_core.recovery_destinations import LocalFolderRecoveryDestination


PASSPHRASE = "portable destination test passphrase"
SECRET_ENV = "ATLAS_TEST_PORTABLE_PASSPHRASE"


class PortableRecoveryDestinationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "company.db"
        self.destination_path = self.root / "portable"
        self.destination = LocalFolderRecoveryDestination(
            destination_id="test-portable",
            path=self.destination_path,
        )
        self.engine = AtlasCore(self.database, (HotelAdapter(),))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def artifact(self):
        backup = self.engine.memory.create_backup()
        return self.engine.memory.export_encrypted_backup(
            backup.backup_id,
            passphrase=PASSPHRASE,
        )

    def automation_config(self, *, destination: Path | None = None) -> Path:
        config = self.root / "automation.json"
        config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "inboxes": [],
                    "backup": {
                        "enabled": True,
                        "interval_minutes": 1440,
                        "keep_last": 2,
                        "apply_retention": False,
                    },
                    "portable_destination": {
                        "enabled": True,
                        "destination_id": "test-portable",
                        "kind": "local_folder",
                        "path": str(destination or self.destination_path),
                        "passphrase_env": SECRET_ENV,
                        "keep_last": 2,
                        "apply_retention": False,
                    },
                }
            ),
            encoding="utf-8",
        )
        return config

    def test_atomic_store_read_replay_and_manifest_minimization(self) -> None:
        artifact = self.artifact()
        stored = self.destination.store(artifact, passphrase=PASSPHRASE)
        self.assertEqual(stored.status, "verified")
        self.assertEqual(stored.sha256, artifact.sha256)
        receipt, data = self.destination.read(
            artifact.export_id,
            passphrase=PASSPHRASE,
        )
        self.assertEqual(receipt.backup_id, artifact.backup_id)
        self.assertEqual(data, artifact.data)
        replay = self.destination.store(artifact, passphrase=PASSPHRASE)
        self.assertTrue(replay.replayed)
        manifest = (
            self.destination_path / f"{artifact.export_id}.json"
        ).read_text(encoding="utf-8")
        self.assertNotIn(str(self.database), manifest)
        self.assertNotIn(str(self.destination_path), manifest)
        self.assertNotIn(PASSPHRASE, manifest)
        self.assertNotIn(SECRET_ENV, manifest)
        with self.assertRaisesRegex(ValueError, "authentication failed"):
            self.destination.read(
                artifact.export_id,
                passphrase="incorrect portable passphrase",
            )

    def test_checksum_tamper_is_visible_and_original_manifest_is_preserved(self) -> None:
        artifact = self.artifact()
        stored = self.destination.store(artifact, passphrase=PASSPHRASE)
        artifact_path = self.destination_path / stored.filename
        with artifact_path.open("r+b") as stream:
            stream.seek(-1, 2)
            final_byte = stream.read(1)
            stream.seek(-1, 2)
            stream.write(bytes([final_byte[0] ^ 0x01]))
        listed = self.destination.list()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].status, "invalid")
        self.assertIn("checksum", listed[0].error)
        self.assertTrue(
            (self.destination_path / f"{stored.export_id}.json").is_file()
        )

    def test_retention_is_preview_first_confirmation_guarded_and_keeps_two(self) -> None:
        stored = [
            self.destination.store(self.artifact(), passphrase=PASSPHRASE)
            for _ in range(3)
        ]
        preview = self.destination.prune(keep_last=2)
        self.assertTrue(preview.dry_run)
        self.assertEqual(len(preview.candidate_export_ids), 1)
        self.assertEqual(len(self.destination.list()), 3)
        with self.assertRaisesRegex(ValueError, "confirmation"):
            self.destination.prune(
                keep_last=2,
                confirmation="yes",
                dry_run=False,
            )
        applied = self.destination.prune(
            keep_last=2,
            confirmation="PRUNE PORTABLE DESTINATION test-portable KEEP 2",
            dry_run=False,
        )
        self.assertEqual(applied.deleted_export_ids, preview.candidate_export_ids)
        self.assertEqual(len(self.destination.list()), 2)
        self.assertNotIn(
            stored[0].export_id,
            {receipt.export_id for receipt in self.destination.list()},
        )

    def test_automation_retries_latest_backup_after_missing_runtime_secret(self) -> None:
        config = self.automation_config()
        runner = AtlasAutomationRunner(
            database=self.database,
            config_path=config,
        )
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(SECRET_ENV, None)
            failed = runner.run_once()
        self.assertEqual(failed.status, "attention_required")
        self.assertTrue(failed.backup_created)
        self.assertFalse(failed.portable_exported)
        failure = next(
            event
            for event in failed.events
            if event.event_type == "backup.portable"
        )
        self.assertEqual(failure.status, "failed")
        self.assertIn(SECRET_ENV, failure.error)
        self.assertNotIn(PASSPHRASE, json.dumps(failure.__dict__))

        with patch.dict(os.environ, {SECRET_ENV: PASSPHRASE}):
            recovered = runner.run_once()
        self.assertEqual(recovered.status, "completed")
        self.assertFalse(recovered.backup_created)
        self.assertTrue(recovered.portable_exported)
        self.assertEqual(len(runner.portable_receipts()), 1)
        event = next(
            item
            for item in recovered.events
            if item.event_type == "backup.portable"
        )
        self.assertEqual(event.status, "verified")
        self.assertNotIn(PASSPHRASE, json.dumps(event.__dict__))

    def test_cli_preview_then_confirmed_import_registers_destination_backup(self) -> None:
        config = self.automation_config()
        with patch.dict(os.environ, {SECRET_ENV: PASSPHRASE}):
            first = AtlasAutomationRunner(
                database=self.database,
                config_path=config,
            ).run_once()
        export_id = next(
            event.detail["export_id"]
            for event in first.events
            if event.event_type == "backup.portable"
        )
        recovered_database = self.root / "recovered.db"
        environment = {**os.environ, SECRET_ENV: PASSPHRASE}
        base = [
            sys.executable,
            "-m",
            "atlas_core.cli",
            "portable-import",
            export_id,
            "--config",
            str(config),
            "--database",
            str(recovered_database),
        ]
        preview = subprocess.run(
            base,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertEqual(json.loads(preview.stdout)["mode"], "preview")
        self.assertFalse(recovered_database.exists())
        applied = subprocess.run(
            [
                *base,
                "--apply",
                "--confirmation",
                f"IMPORT PORTABLE {export_id}",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        payload = json.loads(applied.stdout)
        self.assertEqual(payload["backup"]["status"], "verified")
        self.assertTrue(recovered_database.exists())
        self.assertTrue(Path(f"{recovered_database}.backups").is_dir())

    def test_config_rejects_inline_secret_and_unknown_top_level_fields(self) -> None:
        config = self.automation_config()
        payload = json.loads(config.read_text(encoding="utf-8"))
        payload["portable_destination"]["passphrase"] = PASSPHRASE
        config.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unknown fields") as rejected:
            load_automation_config(config)
        self.assertNotIn(PASSPHRASE, str(rejected.exception))

        del payload["portable_destination"]["passphrase"]
        payload["unrecognized_setting"] = True
        config.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            load_automation_config(config)


if __name__ == "__main__":
    unittest.main()
