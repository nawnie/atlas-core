from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import urllib.request
from dataclasses import asdict
from pathlib import Path

from atlas_core import AtlasAutomationRunner
from atlas_core.adapters import HotelAdapter
from atlas_core_sdk import AtlasClient, HotelPmsSandbox
from atlas_gateway import AccessGrant, create_gateway
from atlas_operator import create_server as create_operator_server


def main() -> None:
    token = secrets.token_urlsafe(48)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        database = root / "gateway-smoke.db"
        inbox = root / "automation-inbox"
        inbox.mkdir()
        automation_config = root / "automation.json"
        portable_destination = root / "portable-recovery"
        secret_name = "ATLAS_SMOKE_RECOVERY_PASSPHRASE"
        automation_config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "inboxes": [
                        {
                            "job_id": "installed-inbox",
                            "tenant_id": "automation-smoke",
                            "domain": "hotel",
                            "connector_id": "installed-inbox",
                            "source_system": "installed-export",
                            "path": str(inbox),
                        }
                    ],
                    "backup": {
                        "enabled": True,
                        "interval_minutes": 1440,
                        "keep_last": 2,
                        "apply_retention": False,
                    },
                    "portable_destination": {
                        "enabled": True,
                        "destination_id": "installed-portable",
                        "kind": "local_folder",
                        "path": str(portable_destination),
                        "passphrase_env": secret_name,
                        "keep_last": 2,
                        "apply_retention": False,
                    },
                }
            ),
            encoding="utf-8",
        )
        automation_record = {
            "record_id": "AUTO-BUNDLE-1",
            "version": "1",
            "payload": {
                "reservation_id": "AUTO-BUNDLE-1",
                "updated_at": "2026-07-24T19:00:00Z",
                "arrival_date": "2026-08-10",
                "departure_date": "2026-08-13",
                "guest_count": 2,
                "room_type": "king",
                "status": "confirmed",
                "total": "729.00",
                "currency": "USD",
            },
        }
        (inbox / "installed-automation.jsonl").write_text(
            json.dumps(automation_record) + "\n",
            encoding="utf-8",
        )
        os.environ[secret_name] = "installed portable recovery passphrase"
        automation_run = AtlasAutomationRunner(
            database=database,
            config_path=automation_config,
        ).run_once()
        if (
            automation_run.imported_files != 1
            or not automation_run.backup_created
            or not automation_run.portable_exported
            or len(tuple(portable_destination.glob("exp_*.atlasbackup"))) != 1
        ):
            raise RuntimeError("Installed automation runner smoke failed.")
        executor = HotelPmsSandbox(root / "hotel-pms-sandbox.db")
        server = create_gateway(
            database=database,
            adapters=(HotelAdapter(),),
            grants=(
                AccessGrant.from_token(
                    name="installed-bundle-smoke",
                    token=token,
                    tenants=frozenset({"*"}),
                    permissions=frozenset(
                        {
                            "read",
                            "ingest",
                            "approve",
                            "execute",
                            "backup",
                            "providers",
                        }
                    ),
                ),
            ),
            executors=(executor,),
            port=0,
            automation_config=automation_config,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = AtlasClient.remote(
                base_url=f"http://127.0.0.1:{server.server_address[1]}",
                api_token=token,
                allow_insecure_loopback=True,
            )
            record = {
                "record_id": "BUNDLE-1",
                "version": "1",
                "payload": {
                    "reservation_id": "BUNDLE-1",
                    "version": "1",
                    "updated_at": "2026-07-24T19:00:00Z",
                    "arrival_date": "2026-08-10",
                    "departure_date": "2026-08-13",
                    "guest_count": 2,
                    "room_type": "king",
                    "status": "confirmed",
                    "total": "729.00",
                    "currency": "USD",
                },
            }
            content = (json.dumps(record) + "\n").encode()
            imported_file = client.import_file(
                content,
                format="jsonl",
                source_name="installed-bundle.jsonl",
                tenant_id="smoke-hotel",
                connector_id="installed-file-drop",
                domain="hotel",
                source_system="installed-bundle-smoke",
            )
            replayed_file = client.import_file(
                content,
                format="jsonl",
                source_name="installed-bundle.jsonl",
                tenant_id="smoke-hotel",
                connector_id="installed-file-drop",
                domain="hotel",
                source_system="installed-bundle-smoke",
            )
            if imported_file.imported_count != 1 or not replayed_file.replayed:
                raise RuntimeError("Installed SDK file import replay contract failed.")
            _, cancelled = executor.seed_cancelled_reservation(
                tenant_id="smoke-hotel",
                reservation_id="BUNDLE-1",
            )
            changed = client.ingest(
                domain="hotel",
                tenant_id="smoke-hotel",
                source_system="hotel-pms-sandbox",
                record=cancelled,
            )
            proposal_id = changed.proposals[0].id
            client.approve(
                proposal_id,
                tenant_id="smoke-hotel",
                reason="Installed execution fixture reviewed.",
                decided_by="installed-smoke",
            )
            queued_execution = client.queue_execution(
                proposal_id,
                tenant_id="smoke-hotel",
                executor_id=executor.executor_id,
                requested_by="installed-smoke",
                confirmation=(
                    f"QUEUE EXECUTION {proposal_id} WITH {executor.executor_id}"
                ),
            )
            completed_execution = client.run_execution(
                queued_execution.execution_id,
                tenant_id="smoke-hotel",
                confirmation=f"EXECUTE {queued_execution.execution_id}",
            )
            provider_state = executor.reservations(tenant_id="smoke-hotel")
            if (
                completed_execution.status != "succeeded"
                or len(provider_state) != 1
                or provider_state[0].inventory_status != "released"
            ):
                raise RuntimeError("Installed execution outbox contract failed.")
            automation_status = client.automation_status()
            if (
                not automation_status.configured
                or automation_status.last_run is None
                or not automation_status.last_run.portable_exported
                or automation_status.config_path
                or automation_status.state_directory
            ):
                raise RuntimeError("Installed automation status contract failed.")
            provider_readiness = client.provider_readiness()
            if (
                len(provider_readiness) != 1
                or provider_readiness[0].provider_id != executor.executor_id
                or provider_readiness[0].status != "ready_for_conformance"
                or provider_readiness[0].network_access
            ):
                raise RuntimeError("Installed provider readiness contract failed.")
            answer = client.answer(
                tenant_id="smoke-hotel",
                query="What changed for BUNDLE-1?",
            )
            if not answer.citations:
                raise RuntimeError("Installed SDK did not return cited evidence.")
            first_backup = client.create_backup()
            encrypted = client.export_encrypted_backup(
                first_backup.backup_id,
                passphrase="installed bundle recovery passphrase",
            )
            imported = client.import_encrypted_backup(
                encrypted.data,
                passphrase="installed bundle recovery passphrase",
            )
            client.create_backup()
            retention = client.prune_backups(
                keep_last=1,
                confirmation="PRUNE BACKUPS KEEP 1",
                dry_run=False,
            )
            print(json.dumps({
                "overview": asdict(client.overview(tenant_id="smoke-hotel")),
                "answer": asdict(answer),
                "file_import": asdict(imported_file),
                "encrypted_export": {
                    "export_id": encrypted.export_id,
                    "backup_id": imported.backup_id,
                    "cipher": encrypted.cipher,
                    "kdf": encrypted.kdf,
                    "sha256": encrypted.sha256,
                },
                "retention": asdict(retention),
                "automation": asdict(automation_status),
                "execution": asdict(completed_execution),
                "provider_readiness": [
                    asdict(receipt) for receipt in provider_readiness
                ],
            }))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            os.environ.pop(secret_name, None)
        operator = create_operator_server(
            database=root / "installed-operator.db",
            port=0,
        )
        operator_thread = threading.Thread(
            target=operator.serve_forever,
            daemon=True,
        )
        operator_thread.start()
        try:
            with urllib.request.urlopen(
                (
                    "http://127.0.0.1:"
                    f"{operator.server_address[1]}/api/bootstrap"
                ),
                timeout=5,
            ) as response:
                bootstrap = json.loads(response.read())
            if "controlled-sandbox-execution" not in bootstrap["capabilities"]:
                raise RuntimeError("Installed operator sandbox capability is missing.")
        finally:
            operator.shutdown()
            operator.server_close()
            operator_thread.join(timeout=5)


if __name__ == "__main__":
    main()
