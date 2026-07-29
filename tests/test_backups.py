from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from atlas_core.adapters import HotelAdapter
from atlas_core_sdk import AtlasClient, InvalidRequestError


class BackupRestoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "company-memory.db"
        self.client = AtlasClient.local(
            database=self.database,
            adapters=(HotelAdapter(),),
        )
        self.base = {
            "reservation_id": "RECOVERY-1",
            "version": "1",
            "updated_at": "2026-07-24T17:00:00Z",
            "arrival_date": "2026-08-10",
            "departure_date": "2026-08-13",
            "guest_count": 2,
            "room_type": "king",
            "status": "confirmed",
            "total": "729.00",
            "currency": "USD",
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def ingest(self, record: dict[str, object]) -> None:
        self.client.ingest(
            domain="hotel",
            tenant_id="recovery-company",
            source_system="fixture-pms",
            record=record,
        )

    def test_backup_restore_recovers_state_and_creates_safety_backup(self) -> None:
        self.ingest(self.base)
        backup = self.client.create_backup()
        self.assertEqual(backup.status, "verified")
        self.assertEqual(backup.integrity_check, "ok")
        self.assertEqual(backup.schema_version, 4)
        self.assertEqual(len(backup.sha256), 64)

        self.ingest(
            {
                **self.base,
                "version": "2",
                "updated_at": "2026-07-24T17:05:00Z",
                "status": "cancelled",
            }
        )
        self.assertEqual(
            self.client.overview(tenant_id="recovery-company").observations,
            2,
        )
        with self.assertRaisesRegex(InvalidRequestError, "exactly"):
            self.client.restore_backup(backup.backup_id, confirmation="yes")

        restored = self.client.restore_backup(
            backup.backup_id,
            confirmation=f"RESTORE {backup.backup_id}",
        )
        self.assertEqual(restored.restored_backup_id, backup.backup_id)
        self.assertNotEqual(restored.safety_backup_id, backup.backup_id)
        self.assertEqual(restored.integrity_check, "ok")
        overview = self.client.overview(tenant_id="recovery-company")
        self.assertEqual(overview.observations, 1)
        self.assertEqual(overview.proposals, 0)
        backup_ids = {item.backup_id for item in self.client.backups()}
        self.assertIn(backup.backup_id, backup_ids)
        self.assertIn(restored.safety_backup_id, backup_ids)

    def test_checksum_tampering_is_visible_and_restore_preserves_live_state(self) -> None:
        self.ingest(self.base)
        backup = self.client.create_backup()
        self.ingest(
            {
                **self.base,
                "version": "2",
                "updated_at": "2026-07-24T17:05:00Z",
                "status": "cancelled",
            }
        )
        backup_file = Path(f"{self.database}.backups") / backup.filename
        with backup_file.open("ab") as stream:
            stream.write(b"tamper")

        listed = self.client.backups()
        invalid = next(item for item in listed if item.backup_id == backup.backup_id)
        self.assertEqual(invalid.status, "invalid")
        self.assertIn("size", invalid.error.lower())
        with self.assertRaises(InvalidRequestError):
            self.client.restore_backup(
                backup.backup_id,
                confirmation=f"RESTORE {backup.backup_id}",
            )
        self.assertEqual(
            self.client.overview(tenant_id="recovery-company").observations,
            2,
        )

    def test_restore_rejects_unknown_and_path_like_identifiers(self) -> None:
        for backup_id in ("../../company-memory", "bkp_unknown", "C:\\memory.db"):
            with self.subTest(backup_id=backup_id):
                with self.assertRaises(InvalidRequestError):
                    self.client.restore_backup(
                        backup_id,
                        confirmation=f"RESTORE {backup_id}",
                    )

    def test_manifest_contains_no_absolute_database_path(self) -> None:
        backup = self.client.create_backup()
        manifest = (
            Path(f"{self.database}.backups") / f"{backup.backup_id}.json"
        )
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertNotIn("database", payload)
        self.assertNotIn(str(self.database), manifest.read_text(encoding="utf-8"))

    def test_databases_in_same_folder_have_isolated_backup_catalogs(self) -> None:
        first = self.client.create_backup()
        other = AtlasClient.local(
            database=Path(self.temp.name) / "other-company-memory.db",
            adapters=(HotelAdapter(),),
        )
        second = other.create_backup()
        self.assertEqual(
            {receipt.backup_id for receipt in self.client.backups()},
            {first.backup_id},
        )
        self.assertEqual(
            {receipt.backup_id for receipt in other.backups()},
            {second.backup_id},
        )
        with self.assertRaises(InvalidRequestError):
            other.restore_backup(
                first.backup_id,
                confirmation=f"RESTORE {first.backup_id}",
            )

    def test_encrypted_export_import_round_trip_and_authentication(self) -> None:
        self.ingest(self.base)
        backup = self.client.create_backup()
        passphrase = "correct horse battery staple"
        artifact = self.client.export_encrypted_backup(
            backup.backup_id,
            passphrase=passphrase,
        )
        self.assertEqual(artifact.cipher, "AES-256-GCM")
        self.assertEqual(artifact.kdf, "scrypt")
        self.assertTrue(artifact.data.startswith(b"ATLASBK1\n"))
        self.assertNotIn(b"SQLite format 3", artifact.data)
        self.assertNotIn(passphrase.encode(), artifact.data)
        self.assertEqual(len(artifact.sha256), 64)

        imported_client = AtlasClient.local(
            database=Path(self.temp.name) / "import-target.db",
            adapters=(HotelAdapter(),),
        )
        with self.assertRaisesRegex(InvalidRequestError, "authentication"):
            imported_client.import_encrypted_backup(
                artifact.data,
                passphrase="this is the wrong passphrase",
            )
        tampered = artifact.data[:-1] + bytes([artifact.data[-1] ^ 1])
        with self.assertRaisesRegex(InvalidRequestError, "authentication"):
            imported_client.import_encrypted_backup(
                tampered,
                passphrase=passphrase,
            )

        imported = imported_client.import_encrypted_backup(
            artifact.data,
            passphrase=passphrase,
        )
        self.assertEqual(imported.backup_id, backup.backup_id)
        restored = imported_client.restore_backup(
            imported.backup_id,
            confirmation=f"RESTORE {imported.backup_id}",
        )
        self.assertEqual(restored.integrity_check, "ok")
        self.assertEqual(
            imported_client.overview(tenant_id="recovery-company").observations,
            1,
        )

    def test_retention_preview_requires_exact_confirmation_and_keeps_newest(self) -> None:
        backups = [self.client.create_backup() for _ in range(3)]
        preview = self.client.prune_backups(keep_last=1)
        self.assertTrue(preview.dry_run)
        self.assertEqual(
            set(preview.candidate_backup_ids),
            {backups[0].backup_id, backups[1].backup_id},
        )
        self.assertGreater(preview.reclaimed_bytes, 0)
        self.assertEqual(len(self.client.backups()), 3)
        with self.assertRaisesRegex(InvalidRequestError, "exactly"):
            self.client.prune_backups(
                keep_last=1,
                confirmation="yes",
                dry_run=False,
            )
        applied = self.client.prune_backups(
            keep_last=1,
            confirmation="PRUNE BACKUPS KEEP 1",
            dry_run=False,
        )
        self.assertFalse(applied.dry_run)
        self.assertEqual(
            set(applied.deleted_backup_ids),
            {backups[0].backup_id, backups[1].backup_id},
        )
        self.assertEqual(
            tuple(receipt.backup_id for receipt in self.client.backups()),
            (backups[2].backup_id,),
        )


if __name__ == "__main__":
    unittest.main()
