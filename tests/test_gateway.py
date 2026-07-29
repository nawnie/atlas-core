from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

from atlas_core import ActionExecutionResult
from atlas_core.adapters import HotelAdapter
from atlas_core_sdk import (
    AtlasClient,
    AtlasTransportError,
    AuthenticationError,
    AuthorizationError,
    InvalidRequestError,
)
from atlas_gateway import AccessGrant, create_gateway


FULL_TOKEN = "full-service-token-" + "a" * 32
READ_TOKEN = "read-service-token-" + "b" * 32
ADMIN_TOKEN = "admin-service-token-" + "c" * 32
EXECUTE_TOKEN = "execute-service-token-" + "d" * 32


class GatewaySyntheticExecutor:
    executor_id = "gateway-synthetic"
    action_types = frozenset({"review_cancelled_reservation"})
    supports_rollback = True

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, request):
        self.calls += 1
        return ActionExecutionResult(
            succeeded=True,
            summary="Gateway synthetic action completed.",
            external_reference=request.idempotency_key,
        )

    def rollback(self, request, receipt):
        return ActionExecutionResult(
            succeeded=True,
            summary="Gateway synthetic action rolled back.",
            external_reference=receipt.external_reference,
        )


class GatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.executor = GatewaySyntheticExecutor()
        self.server = create_gateway(
            database=Path(self.temp.name) / "gateway.db",
            adapters=(HotelAdapter(),),
            grants=(
                AccessGrant.from_token(
                    name="hotel-application",
                    token=FULL_TOKEN,
                    tenants=frozenset({"hotel-a"}),
                    permissions=frozenset(
                        {"read", "ingest", "approve", "execute", "reconcile"}
                    ),
                ),
                AccessGrant.from_token(
                    name="hotel-reader",
                    token=READ_TOKEN,
                    tenants=frozenset({"hotel-a"}),
                    permissions=frozenset({"read"}),
                ),
                AccessGrant.from_token(
                    name="execution-worker",
                    token=EXECUTE_TOKEN,
                    tenants=frozenset({"hotel-a"}),
                    permissions=frozenset({"read", "execute"}),
                ),
                AccessGrant.from_token(
                    name="recovery-administrator",
                    token=ADMIN_TOKEN,
                    tenants=frozenset({"*"}),
                    permissions=frozenset({"read", "backup", "providers"}),
                ),
            ),
            executors=(self.executor,),
            port=0,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.client = AtlasClient.remote(
            base_url=self.base_url,
            api_token=FULL_TOKEN,
            allow_insecure_loopback=True,
        )
        self.admin_client = AtlasClient.remote(
            base_url=self.base_url,
            api_token=ADMIN_TOKEN,
            allow_insecure_loopback=True,
        )
        self.base = {
            "reservation_id": "REMOTE-1",
            "version": "1",
            "updated_at": "2026-07-24T18:00:00Z",
            "arrival_date": "2026-08-10",
            "departure_date": "2026-08-13",
            "guest_count": 2,
            "room_type": "king",
            "status": "confirmed",
            "total": "729.00",
            "currency": "USD",
        }

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def raw_request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        decoded = json.loads(raw) if raw else {}
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        connection.close()
        return response.status, decoded, response_headers

    def ingest(self, record: dict[str, object]) -> Any:
        return self.client.ingest(
            domain="hotel",
            tenant_id="hotel-a",
            source_system="remote-fixture-pms",
            record=record,
        )

    def test_remote_transport_requires_https_except_explicit_loopback(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            AtlasClient.remote(
                base_url=self.base_url,
                api_token=FULL_TOKEN,
            )
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            AtlasClient.remote(
                base_url="http://example.test",
                api_token=FULL_TOKEN,
                allow_insecure_loopback=True,
            )
        with self.assertRaisesRegex(ValueError, "api_token"):
            AtlasClient.remote(
                base_url="https://example.test",
                api_token="short",
            )

    def test_gateway_requires_auth_and_emits_no_cors_permission(self) -> None:
        status, payload, headers = self.raw_request(
            "GET",
            "/v1/tenants/hotel-a/overview",
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "unauthenticated")
        self.assertNotIn("access-control-allow-origin", headers)
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertIn("Bearer", headers["www-authenticate"])

        invalid = AtlasClient.remote(
            base_url=self.base_url,
            api_token="wrong-token-" + "z" * 32,
            allow_insecure_loopback=True,
        )
        with self.assertRaises(AuthenticationError):
            invalid.overview(tenant_id="hotel-a")

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.putrequest("GET", "/v1/tenants/hotel-a/overview")
        connection.putheader("Authorization", f"Bearer {FULL_TOKEN}")
        connection.putheader("Authorization", f"Bearer {READ_TOKEN}")
        connection.endheaders()
        response = connection.getresponse()
        response.read()
        connection.close()
        self.assertEqual(response.status, 401)

    def test_tenant_scoped_remote_full_path(self) -> None:
        first = self.ingest(self.base)
        self.assertEqual(first.proposals, ())
        changed = self.ingest(
            {
                **self.base,
                "version": "2",
                "updated_at": "2026-07-24T18:05:00Z",
                "status": "cancelled",
            }
        )
        self.assertEqual(len(changed.proposals), 1)
        overview = self.client.overview(tenant_id="hotel-a")
        self.assertEqual(overview.observations, 2)
        self.assertEqual(overview.pending, 1)
        cards = self.client.search_cartography(
            tenant_id="hotel-a",
            lane="hotel_operations",
        )
        self.assertEqual(len(cards), 2)
        proposals = self.client.proposals(tenant_id="hotel-a", status="pending")
        answer = self.client.answer(
            tenant_id="hotel-a",
            query="Why was REMOTE-1 cancelled?",
        )
        self.assertEqual(len(answer.sections), 3)
        self.assertIn(
            "remote-fixture-pms",
            {
                citation.source_pointer.split(":", 1)[0]
                for citation in answer.citations
                if citation.evidence_type == "observation"
            },
        )
        receipt = self.client.approve(
            proposals[0].id,
            tenant_id="hotel-a",
            reason="Remote reviewer verified the synthetic cancellation.",
            decided_by="gateway-test",
        )
        self.assertEqual(receipt.status, "approved")
        events = self.client.audit_events(tenant_id="hotel-a")
        self.assertIn("proposal.decided", {event.event_type for event in events})

    def test_cross_tenant_and_function_permissions_are_server_enforced(self) -> None:
        with self.assertRaises(AuthorizationError):
            self.client.overview(tenant_id="hotel-b")
        reader = AtlasClient.remote(
            base_url=self.base_url,
            api_token=READ_TOKEN,
            allow_insecure_loopback=True,
        )
        self.assertEqual(reader.overview(tenant_id="hotel-a").observations, 0)
        with self.assertRaises(AuthorizationError):
            reader.ingest(
                domain="hotel",
                tenant_id="hotel-a",
                source_system="fixture",
                record=self.base,
            )
        with self.assertRaises(AuthorizationError):
            self.client.create_backup()
        with self.assertRaises(AuthorizationError):
            self.client.export_encrypted_backup(
                "bkp_00000000000000000000",
                passphrase="not authorized passphrase",
            )

    def test_provider_readiness_requires_global_permission_and_is_static_only(
        self,
    ) -> None:
        receipt = self.admin_client.provider_readiness()[0]
        self.assertEqual(receipt.provider_id, self.executor.executor_id)
        self.assertEqual(receipt.status, "failed")
        self.assertEqual(receipt.checks[0].name, "manifest")
        self.assertFalse(hasattr(receipt, "credential"))
        with self.assertRaises(AuthorizationError):
            self.client.provider_readiness()
        with self.assertRaises(InvalidRequestError):
            self.admin_client.provider_readiness(
                certify_local_sandboxes=True
            )

    def test_remote_execution_requires_distinct_permission_and_server_executor(self) -> None:
        self.ingest({**self.base, "reservation_id": "REMOTE-EXEC-1"})
        changed = self.ingest(
            {
                **self.base,
                "reservation_id": "REMOTE-EXEC-1",
                "version": "2",
                "updated_at": "2026-07-24T18:05:00Z",
                "status": "cancelled",
            }
        )
        proposal_id = changed.proposals[0].id
        self.client.approve(
            proposal_id,
            tenant_id="hotel-a",
            reason="Gateway execution fixture reviewed.",
            decided_by="gateway-reviewer",
        )
        reader = AtlasClient.remote(
            base_url=self.base_url,
            api_token=READ_TOKEN,
            allow_insecure_loopback=True,
        )
        with self.assertRaises(AuthorizationError):
            reader.queue_execution(
                proposal_id,
                tenant_id="hotel-a",
                executor_id=self.executor.executor_id,
                requested_by="reader",
                confirmation=(
                    f"QUEUE EXECUTION {proposal_id} WITH "
                    f"{self.executor.executor_id}"
                ),
            )
        queued = self.client.queue_execution(
            proposal_id,
            tenant_id="hotel-a",
            executor_id=self.executor.executor_id,
            requested_by="gateway-operator",
            confirmation=(
                f"QUEUE EXECUTION {proposal_id} WITH {self.executor.executor_id}"
            ),
        )
        self.assertEqual(queued.requested_by, "gateway:hotel-application")
        completed = self.client.run_execution(
            queued.execution_id,
            tenant_id="hotel-a",
            confirmation=f"EXECUTE {queued.execution_id}",
        )
        self.assertEqual(completed.status, "succeeded")
        self.assertEqual(self.executor.calls, 1)
        receipts = reader.executions(tenant_id="hotel-a")
        self.assertEqual(receipts[0].execution_id, completed.execution_id)
        self.assertFalse(hasattr(receipts[0], "payload"))
        execution_worker = AtlasClient.remote(
            base_url=self.base_url,
            api_token=EXECUTE_TOKEN,
            allow_insecure_loopback=True,
        )
        with self.assertRaises(AuthorizationError):
            execution_worker.reconcile_execution(
                completed.execution_id,
                tenant_id="hotel-a",
                outcome="succeeded",
                note="Client assertion must not bypass permission.",
                reconciled_by="untrusted-client-claim",
                confirmation=(
                    f"RECONCILE EXECUTION {completed.execution_id} AS SUCCEEDED"
                ),
            )

    def test_automation_status_requires_admin_and_redacts_server_paths(self) -> None:
        with self.assertRaises(AuthorizationError):
            self.client.automation_status()
        administrator = AtlasClient.remote(
            base_url=self.base_url,
            api_token=ADMIN_TOKEN,
            allow_insecure_loopback=True,
        )
        status = administrator.automation_status()
        self.assertFalse(status.configured)
        self.assertFalse(status.locked)
        self.assertEqual(status.config_path, "")
        self.assertEqual(status.state_directory, "")

    def test_admin_backup_round_trip_uses_separate_permission(self) -> None:
        administrator = AtlasClient.remote(
            base_url=self.base_url,
            api_token=ADMIN_TOKEN,
            allow_insecure_loopback=True,
        )
        backup = administrator.create_backup()
        self.assertEqual(backup.status, "verified")
        artifact = administrator.export_encrypted_backup(
            backup.backup_id,
            passphrase="remote recovery passphrase",
        )
        self.assertTrue(artifact.data.startswith(b"ATLASBK1\n"))
        imported = administrator.import_encrypted_backup(
            artifact.data,
            passphrase="remote recovery passphrase",
        )
        self.assertEqual(imported.backup_id, backup.backup_id)
        self.ingest(self.base)
        self.assertEqual(
            self.client.overview(tenant_id="hotel-a").observations,
            1,
        )
        restored = administrator.restore_backup(
            backup.backup_id,
            confirmation=f"RESTORE {backup.backup_id}",
        )
        self.assertNotEqual(restored.safety_backup_id, backup.backup_id)
        self.assertEqual(
            self.client.overview(tenant_id="hotel-a").observations,
            0,
        )

    def test_remote_retention_is_preview_first_and_confirmation_guarded(self) -> None:
        administrator = AtlasClient.remote(
            base_url=self.base_url,
            api_token=ADMIN_TOKEN,
            allow_insecure_loopback=True,
        )
        backups = [administrator.create_backup() for _ in range(3)]
        preview = administrator.prune_backups(keep_last=1)
        self.assertEqual(
            set(preview.candidate_backup_ids),
            {backups[0].backup_id, backups[1].backup_id},
        )
        with self.assertRaisesRegex(InvalidRequestError, "exactly"):
            administrator.prune_backups(
                keep_last=1,
                confirmation="wrong",
                dry_run=False,
            )
        applied = administrator.prune_backups(
            keep_last=1,
            confirmation="PRUNE BACKUPS KEEP 1",
            dry_run=False,
        )
        self.assertEqual(len(applied.deleted_backup_ids), 2)
        self.assertEqual(len(administrator.backups()), 1)

    def test_remote_connector_import_and_replay_receipts(self) -> None:
        records = (
            {"record_id": "REMOTE-1", "version": "1", "payload": self.base},
            {
                "record_id": "REMOTE-1",
                "version": "2",
                "payload": {
                    **self.base,
                    "version": "2",
                    "updated_at": "2026-07-24T18:05:00Z",
                    "status": "cancelled",
                },
            },
        )
        first = self.client.import_batch(
            tenant_id="hotel-a",
            connector_id="remote-fixture-feed",
            domain="hotel",
            source_system="remote-fixture-pms",
            external_batch_id="remote-batch-1",
            records=records,
        )
        replay = self.client.import_batch(
            tenant_id="hotel-a",
            connector_id="remote-fixture-feed",
            domain="hotel",
            source_system="remote-fixture-pms",
            external_batch_id="remote-batch-1",
            records=records,
        )
        self.assertEqual(first.imported_count, 2)
        self.assertTrue(replay.replayed)
        listed = self.client.imports(tenant_id="hotel-a")
        self.assertEqual(len(listed), 1)
        self.assertEqual(len(listed[0].records), 2)

    def test_remote_sdk_file_import_uses_existing_authenticated_batch_route(self) -> None:
        row = {
            "record_id": "FILE-REMOTE-1",
            "version": "1",
            "payload": {
                **self.base,
                "reservation_id": "FILE-REMOTE-1",
            },
        }
        content = (json.dumps(row) + "\n").encode()
        receipt = self.client.import_file(
            content,
            format="jsonl",
            source_name="remote-export.jsonl",
            tenant_id="hotel-a",
            connector_id="remote-file-drop",
            domain="hotel",
            source_system="remote-file-export",
        )
        replay = self.client.import_file(
            content,
            format="jsonl",
            source_name="remote-export.jsonl",
            tenant_id="hotel-a",
            connector_id="remote-file-drop",
            domain="hotel",
            source_system="remote-file-export",
        )
        self.assertEqual(receipt.imported_count, 1)
        self.assertTrue(replay.replayed)

    def test_gateway_refuses_unencrypted_non_loopback_binding(self) -> None:
        with self.assertRaisesRegex(ValueError, "allow_remote"):
            create_gateway(
                database=Path(self.temp.name) / "unsafe-1.db",
                adapters=(HotelAdapter(),),
                grants=self.server.grants,
                host="0.0.0.0",
                port=0,
            )
        with self.assertRaisesRegex(ValueError, "TLS"):
            create_gateway(
                database=Path(self.temp.name) / "unsafe-2.db",
                adapters=(HotelAdapter(),),
                grants=self.server.grants,
                host="0.0.0.0",
                port=0,
                allow_remote=True,
            )

    def test_request_body_limit_is_enforced_before_read(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.putrequest("POST", "/v1/tenants/hotel-a/ingest")
        connection.putheader("Authorization", f"Bearer {FULL_TOKEN}")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(7 * 1024 * 1024))
        connection.endheaders()
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 400)
        self.assertIn("exceed", payload["error"]["message"])

    def test_per_identity_rate_limit_is_enforced(self) -> None:
        server = create_gateway(
            database=Path(self.temp.name) / "limited.db",
            adapters=(HotelAdapter(),),
            grants=(
                AccessGrant.from_token(
                    name="limited-reader",
                    token="limited-token-" + "d" * 32,
                    tenants=frozenset({"hotel-a"}),
                    permissions=frozenset({"read"}),
                ),
            ),
            port=0,
            requests_per_minute=2,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = AtlasClient.remote(
                base_url=f"http://127.0.0.1:{server.server_address[1]}",
                api_token="limited-token-" + "d" * 32,
                allow_insecure_loopback=True,
            )
            client.overview(tenant_id="hotel-a")
            client.overview(tenant_id="hotel-a")
            with self.assertRaises(AtlasTransportError):
                client.overview(tenant_id="hotel-a")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
