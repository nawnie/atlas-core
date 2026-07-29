from __future__ import annotations

import http.client
import json
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

from atlas_core import (
    ActionExecutionResult,
    AutomationEvent,
    AutomationRunReceipt,
    AutomationStore,
)
from atlas_core.adapters import HotelAdapter
from atlas_core_sdk import AtlasClient
from atlas_operator import create_server


class OperatorSyntheticExecutor:
    executor_id = "operator-synthetic"
    action_types = frozenset({"review_cancelled_reservation"})
    supports_rollback = False

    def execute(self, request):
        return ActionExecutionResult(
            succeeded=True,
            summary="Operator fixture completed.",
            external_reference=request.idempotency_key,
        )

    def rollback(self, request, receipt):
        raise AssertionError("Rollback must not be called.")


class OperatorApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "operator.db"
        self.server = create_server(
            database=self.database,
            port=0,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        token: str = "",
        tenant: str = "",
        origin: str | None = None,
        content_type: str = "application/json",
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        body = json.dumps(payload) if payload is not None else None
        headers: dict[str, str] = {}
        if body is not None:
            headers.update(
                {
                    "Content-Type": content_type,
                    "Content-Length": str(len(body.encode())),
                    "X-Atlas-Request-Token": token,
                    "X-Atlas-Tenant": tenant,
                }
            )
        if origin is not None:
            headers["Origin"] = origin
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        result = json.loads(raw) if raw else {}
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        connection.close()
        return response.status, result, response_headers

    def bootstrap(self) -> str:
        status, payload, _ = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        return payload["request_token"]

    def binary_request(
        self,
        path: str,
        body: bytes,
        *,
        token: str,
        tenant: str,
        content_type: str,
    ) -> tuple[int, bytes, dict[str, str]]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request(
            "POST",
            path,
            body=body,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
                "X-Atlas-Request-Token": token,
                "X-Atlas-Tenant": tenant,
            },
        )
        response = connection.getresponse()
        raw = response.read()
        headers = {key.lower(): value for key, value in response.getheaders()}
        connection.close()
        return response.status, raw, headers

    def test_static_dashboard_and_security_headers(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("GET", "/")
        response = connection.getresponse()
        body = response.read().decode()
        headers = {key.lower(): value for key, value in response.getheaders()}
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertIn("Atlas Core Operator", body)
        self.assertIn("Import encrypted backup", body)
        self.assertIn("Automation health", body)
        self.assertIn("Execution outbox", body)
        self.assertIn("Provider readiness", body)
        app_script = (
            Path(__file__).parents[1]
            / "apps"
            / "operator"
            / "src"
            / "atlas_operator"
            / "web"
            / "app.js"
        ).read_text(encoding="utf-8")
        self.assertIn("Portable copy", app_script)
        self.assertIn("idempotency", app_script)
        self.assertIn("manifest_sha256", app_script)
        self.assertIn("default-src 'self'", headers["content-security-policy"])
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertNotIn("access-control-allow-origin", headers)

    def test_automation_status_is_read_only_and_payload_safe(self) -> None:
        status, payload, _ = self.request("GET", "/api/automation")
        self.assertEqual(status, 200)
        self.assertFalse(payload["configured"])
        self.assertFalse(payload["locked"])
        self.assertIsNone(payload["last_run"])
        self.assertEqual(payload["recent_events"], [])
        event = AutomationEvent(
            event_id="autoevt_" + "a" * 20,
            run_id="autorun_" + "b" * 20,
            event_type="backup.portable",
            status="verified",
            occurred_at="2026-07-24T20:30:00+00:00",
            detail={
                "destination_id": "operator-portable",
                "filename": "exp_" + "c" * 20 + ".atlasbackup",
                "cipher": "AES-256-GCM",
            },
        )
        AutomationStore(self.database).append_event(event)
        AutomationStore(self.database).save_run(
            AutomationRunReceipt(
                run_id=event.run_id,
                started_at=event.occurred_at,
                completed_at=event.occurred_at,
                status="completed",
                imported_files=0,
                rejected_files=0,
                replayed_files=0,
                skipped_files=0,
                backup_created=True,
                events=(event,),
                portable_exported=True,
            )
        )
        status, payload, _ = self.request("GET", "/api/automation")
        self.assertEqual(status, 200)
        self.assertTrue(payload["last_run"]["portable_exported"])
        self.assertEqual(
            payload["recent_events"][0]["detail"]["destination_id"],
            "operator-portable",
        )
        self.assertNotIn("passphrase", json.dumps(payload).lower())

    def test_execution_status_is_tenant_scoped_payload_free_and_live_executor_denied(
        self,
    ) -> None:
        executor = OperatorSyntheticExecutor()
        client = AtlasClient.local(
            database=self.database,
            adapters=(HotelAdapter(),),
            executors=(executor,),
        )
        base = {
            "reservation_id": "OP-EXEC-1",
            "updated_at": "2026-07-24T20:00:00Z",
            "arrival_date": "2026-08-01",
            "departure_date": "2026-08-03",
            "guest_count": 2,
            "room_type": "king",
            "status": "confirmed",
            "total": "500.00",
            "currency": "USD",
        }
        client.ingest(
            domain="hotel",
            tenant_id="operator-execution",
            source_system="operator-fixture",
            record=base,
        )
        changed = client.ingest(
            domain="hotel",
            tenant_id="operator-execution",
            source_system="operator-fixture",
            record={
                **base,
                "updated_at": "2026-07-24T20:05:00Z",
                "status": "cancelled",
                "total": "0.00",
            },
        )
        proposal_id = changed.proposals[0].id
        client.approve(
            proposal_id,
            tenant_id="operator-execution",
            reason="Operator fixture reviewed.",
            decided_by="operator-reviewer",
        )
        queued = client.queue_execution(
            proposal_id,
            tenant_id="operator-execution",
            executor_id=executor.executor_id,
            requested_by="operator-fixture",
            confirmation=(
                f"QUEUE EXECUTION {proposal_id} WITH {executor.executor_id}"
            ),
        )
        client.run_execution(
            queued.execution_id,
            tenant_id="operator-execution",
            confirmation=f"EXECUTE {queued.execution_id}",
        )
        status, payload, _ = self.request(
            "GET",
            "/api/executions?tenant_id=operator-execution",
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"][0]["status"], "succeeded")
        encoded = json.dumps(payload)
        self.assertNotIn("payload", encoded)
        self.assertNotIn("reservation_id", encoded)
        status, other, _ = self.request(
            "GET",
            "/api/executions?tenant_id=other-company",
        )
        self.assertEqual(status, 200)
        self.assertEqual(other["items"], [])
        status, _, _ = self.request(
            "POST",
            f"/api/executions/{queued.execution_id}/run",
            {"tenant_id": "operator-execution"},
            token=self.bootstrap(),
            tenant="operator-execution",
        )
        self.assertEqual(status, 403)

    def test_encrypted_recovery_download_upload_and_retention_api(self) -> None:
        token = self.bootstrap()
        tenant = "recovery-portable"
        status, backup, _ = self.request(
            "POST",
            "/api/backups",
            {"tenant_id": tenant},
            token=token,
            tenant=tenant,
        )
        self.assertEqual(status, 201)
        passphrase = "operator portable passphrase"
        status, artifact, headers = self.binary_request(
            f"/api/backups/{backup['backup_id']}/export",
            json.dumps(
                {"tenant_id": tenant, "passphrase": passphrase}
            ).encode(),
            token=token,
            tenant=tenant,
            content_type="application/json",
        )
        self.assertEqual(status, 200)
        self.assertTrue(artifact.startswith(b"ATLASBK1\n"))
        self.assertEqual(
            headers["content-type"],
            "application/vnd.atlas.encrypted-backup",
        )
        passphrase_bytes = passphrase.encode()
        envelope = (
            struct.pack(">I", len(passphrase_bytes))
            + passphrase_bytes
            + artifact
        )
        status, raw, _ = self.binary_request(
            "/api/backups/import",
            envelope,
            token=token,
            tenant=tenant,
            content_type="application/vnd.atlas.encrypted-backup",
        )
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(raw)["backup_id"], backup["backup_id"])

        self.request(
            "POST",
            "/api/backups",
            {"tenant_id": tenant},
            token=token,
            tenant=tenant,
        )
        status, preview, _ = self.request(
            "POST",
            "/api/backups/retention",
            {"tenant_id": tenant, "keep_last": 1, "dry_run": True},
            token=token,
            tenant=tenant,
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(preview["candidate_backup_ids"]), 1)

    def test_file_import_preview_hides_payload_then_applies_replay_safe_batch(self) -> None:
        token = self.bootstrap()
        tenant = "operator-file"
        base = {
            "reservation_id": "UPLOAD-1",
            "updated_at": "2026-07-24T20:00:00Z",
            "arrival_date": "2026-08-10",
            "departure_date": "2026-08-13",
            "guest_count": 2,
            "room_type": "king",
            "status": "confirmed",
            "total": "729",
            "currency": "USD",
        }
        rows = [
            {"record_id": "UPLOAD-1", "version": "1", "payload": base},
            {
                "record_id": "UPLOAD-1",
                "version": "2",
                "payload": {
                    **base,
                    "updated_at": "2026-07-24T20:05:00Z",
                    "status": "cancelled",
                },
            },
        ]
        content = ("\n".join(json.dumps(row) for row in rows) + "\n").encode()
        metadata = json.dumps(
            {
                "format": "jsonl",
                "source_name": "hotel-upload.jsonl",
                "connector_id": "operator-file-drop",
                "source_system": "operator-export",
                "domain": "hotel",
            },
            separators=(",", ":"),
        ).encode()
        envelope = struct.pack(">I", len(metadata)) + metadata + content
        status, raw, _ = self.binary_request(
            "/api/file-imports/preview",
            envelope,
            token=token,
            tenant=tenant,
            content_type="application/vnd.atlas.import-file",
        )
        self.assertEqual(status, 200)
        preview = json.loads(raw)
        self.assertEqual(preview["record_count"], 2)
        self.assertNotIn("payload", preview["records"][0])
        self.assertEqual(preview["records"][1]["record_id"], "UPLOAD-1")

        status, raw, _ = self.binary_request(
            "/api/file-imports/apply",
            envelope,
            token=token,
            tenant=tenant,
            content_type="application/vnd.atlas.import-file",
        )
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(raw)["imported_count"], 2)
        status, raw, _ = self.binary_request(
            "/api/file-imports/apply",
            envelope,
            token=token,
            tenant=tenant,
            content_type="application/vnd.atlas.import-file",
        )
        self.assertEqual(status, 201)
        self.assertTrue(json.loads(raw)["replayed"])

    def test_mutation_requires_token_tenant_match_and_same_origin(self) -> None:
        payload = {"tenant_id": "demo-hotel"}
        status, _, _ = self.request(
            "POST", "/api/demo/seed", payload, tenant="demo-hotel"
        )
        self.assertEqual(status, 403)
        token = self.bootstrap()
        status, _, _ = self.request(
            "POST",
            "/api/demo/seed",
            payload,
            token=token,
            tenant="different",
        )
        self.assertEqual(status, 403)
        status, _, _ = self.request(
            "POST",
            "/api/demo/seed",
            payload,
            token=token,
            tenant="demo-hotel",
            origin="https://malicious.example",
        )
        self.assertEqual(status, 403)

    def test_end_to_end_seed_map_approve_and_audit(self) -> None:
        token = self.bootstrap()
        status, seeded, _ = self.request(
            "POST",
            "/api/demo/seed",
            {"tenant_id": "demo-hotel"},
            token=token,
            tenant="demo-hotel",
        )
        self.assertEqual(status, 201)
        self.assertEqual(seeded["result"]["summary"], "2 field(s) changed: status, total.")

        status, overview, _ = self.request(
            "GET", "/api/overview?tenant_id=demo-hotel"
        )
        self.assertEqual(status, 200)
        self.assertEqual(overview["observations"], 2)
        self.assertEqual(overview["pending"], 1)
        self.assertEqual(overview["cartography_cards"], 2)

        status, answer, _ = self.request(
            "GET",
            "/api/answer?tenant_id=demo-hotel&query=cancelled&limit=5",
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(answer["sections"]), 3)
        self.assertTrue(answer["citations"])

        status, proposals, _ = self.request(
            "GET", "/api/proposals?tenant_id=demo-hotel&status=pending"
        )
        self.assertEqual(status, 200)
        proposal_id = proposals["items"][0]["id"]

        status, maps, _ = self.request(
            "GET", "/api/cartography?tenant_id=demo-hotel&lane=hotel_operations"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(maps["items"]), 2)
        self.assertIn(
            "hotel-pms-sandbox:DEMO-2048:2",
            maps["items"][0]["source_pointers"],
        )

        status, _, _ = self.request(
            "POST",
            f"/api/proposals/{proposal_id}/decision",
            {
                "tenant_id": "other-company",
                "approved": True,
                "reason": "Cross-company attempt.",
                "decided_by": "operator",
            },
            token=token,
            tenant="other-company",
        )
        self.assertEqual(status, 400)

        status, receipt, _ = self.request(
            "POST",
            f"/api/proposals/{proposal_id}/decision",
            {
                "tenant_id": "demo-hotel",
                "approved": True,
                "reason": "Cancellation source and refund terms verified.",
                "decided_by": "local-operator",
            },
            token=token,
            tenant="demo-hotel",
        )
        self.assertEqual(status, 200)
        self.assertEqual(receipt["status"], "approved")

        status, audit, _ = self.request(
            "GET", "/api/audit?tenant_id=demo-hotel&limit=100"
        )
        self.assertEqual(status, 200)
        event_types = {event["event_type"] for event in audit["items"]}
        self.assertIn("proposal.decided", event_types)
        self.assertIn("cartography.recorded", event_types)

    def test_hotel_workspace_completes_guarded_sandbox_execution_and_rollback(
        self,
    ) -> None:
        token = self.bootstrap()
        tenant = "hotel-workspace"
        status, _, _ = self.request(
            "POST",
            "/api/demo/seed",
            {"tenant_id": tenant},
            token=token,
            tenant=tenant,
        )
        self.assertEqual(status, 201)
        status, provider, _ = self.request(
            "GET",
            f"/api/hotel-sandbox/reservations?tenant_id={tenant}",
        )
        self.assertEqual(status, 200)
        self.assertTrue(provider["sandbox"])
        self.assertEqual(provider["items"][0]["inventory_status"], "held")

        status, proposals, _ = self.request(
            "GET", f"/api/proposals?tenant_id={tenant}&status=pending"
        )
        self.assertEqual(status, 200)
        proposal_id = proposals["items"][0]["id"]
        status, _, _ = self.request(
            "POST",
            f"/api/proposals/{proposal_id}/decision",
            {
                "tenant_id": tenant,
                "approved": True,
                "reason": "Synthetic cancellation source reviewed.",
                "decided_by": "hotel-operator",
            },
            token=token,
            tenant=tenant,
        )
        self.assertEqual(status, 200)

        queue_phrase = (
            f"QUEUE EXECUTION {proposal_id} WITH hotel-pms-sandbox"
        )
        status, _, _ = self.request(
            "POST",
            f"/api/proposals/{proposal_id}/sandbox-execution",
            {"tenant_id": tenant, "confirmation": "queue it"},
            token=token,
            tenant=tenant,
        )
        self.assertEqual(status, 400)
        status, queued, _ = self.request(
            "POST",
            f"/api/proposals/{proposal_id}/sandbox-execution",
            {"tenant_id": tenant, "confirmation": queue_phrase},
            token=token,
            tenant=tenant,
        )
        self.assertEqual(status, 201)
        self.assertEqual(queued["status"], "queued")
        execution_id = queued["execution_id"]

        status, completed, _ = self.request(
            "POST",
            f"/api/executions/{execution_id}/run",
            {
                "tenant_id": tenant,
                "confirmation": f"EXECUTE {execution_id}",
            },
            token=token,
            tenant=tenant,
        )
        self.assertEqual(status, 200)
        self.assertEqual(completed["status"], "succeeded")
        self.assertTrue(
            completed["external_reference"].startswith("hotel-sandbox:")
        )
        status, provider, _ = self.request(
            "GET",
            f"/api/hotel-sandbox/reservations?tenant_id={tenant}",
        )
        self.assertEqual(provider["items"][0]["inventory_status"], "released")
        self.assertEqual(
            provider["items"][0]["cancellation_review_status"],
            "completed",
        )

        status, rolled_back, _ = self.request(
            "POST",
            f"/api/executions/{execution_id}/rollback",
            {
                "tenant_id": tenant,
                "confirmation": f"ROLLBACK EXECUTION {execution_id}",
            },
            token=token,
            tenant=tenant,
        )
        self.assertEqual(status, 200)
        self.assertEqual(rolled_back["status"], "rolled_back")
        status, provider, _ = self.request(
            "GET",
            f"/api/hotel-sandbox/reservations?tenant_id={tenant}",
        )
        self.assertEqual(provider["items"][0]["inventory_status"], "held")

    def test_provider_readiness_is_certified_payload_safe_and_read_only(
        self,
    ) -> None:
        status, payload, _ = self.request("GET", "/api/providers")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["items"]), 1)
        provider = payload["items"][0]
        self.assertEqual(provider["provider_id"], "hotel-pms-sandbox")
        self.assertEqual(provider["status"], "sandbox_certified")
        self.assertEqual(provider["credential_strategy"], "none")
        self.assertFalse(provider["network_access"])
        self.assertTrue(provider["synthetic_only"])
        self.assertTrue(provider["manifest_sha256"])
        self.assertTrue(provider["checks"])
        self.assertTrue(
            all(check["status"] == "passed" for check in provider["checks"])
        )
        encoded = json.dumps(payload).lower()
        self.assertNotIn("password", encoded)
        self.assertNotIn("api_key", encoded)
        self.assertNotIn("bearer", encoded)
        token = self.bootstrap()
        status, _, _ = self.request(
            "POST",
            "/api/providers",
            {"tenant_id": "demo-hotel"},
            token=token,
            tenant="demo-hotel",
        )
        self.assertEqual(status, 404)

    def test_hotel_sandbox_mutation_routes_enforce_token_and_tenant(self) -> None:
        token = self.bootstrap()
        tenant = "hotel-sandbox-auth"
        status, _, _ = self.request(
            "POST",
            "/api/demo/seed",
            {"tenant_id": tenant},
            token=token,
            tenant=tenant,
        )
        self.assertEqual(status, 201)
        status, proposals, _ = self.request(
            "GET", f"/api/proposals?tenant_id={tenant}&status=pending"
        )
        proposal_id = proposals["items"][0]["id"]
        status, _, _ = self.request(
            "POST",
            f"/api/proposals/{proposal_id}/sandbox-execution",
            {
                "tenant_id": tenant,
                "confirmation": (
                    f"QUEUE EXECUTION {proposal_id} WITH hotel-pms-sandbox"
                ),
            },
            tenant=tenant,
        )
        self.assertEqual(status, 403)
        status, _, _ = self.request(
            "POST",
            f"/api/proposals/{proposal_id}/sandbox-execution",
            {
                "tenant_id": tenant,
                "confirmation": (
                    f"QUEUE EXECUTION {proposal_id} WITH hotel-pms-sandbox"
                ),
            },
            token=token,
            tenant="other-hotel",
        )
        self.assertEqual(status, 403)

    def test_apartment_demo_uses_import_batch_and_exposes_receipt(self) -> None:
        token = self.bootstrap()
        status, result, _ = self.request(
            "POST",
            "/api/demo/apartment",
            {"tenant_id": "apartment-demo"},
            token=token,
            tenant="apartment-demo",
        )
        self.assertEqual(status, 201)
        self.assertEqual(result["receipt"]["status"], "completed")
        self.assertEqual(result["receipt"]["imported_count"], 2)

        status, imports, _ = self.request(
            "GET", "/api/imports?tenant_id=apartment-demo"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(imports["items"]), 1)
        self.assertEqual(imports["items"][0]["domain"], "apartment")

        status, proposals, _ = self.request(
            "GET", "/api/proposals?tenant_id=apartment-demo&status=pending"
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            proposals["items"][0]["action_type"],
            "review_delinquent_lease",
        )

    def test_all_first_wave_industry_demos_reach_maps_and_approval(self) -> None:
        token = self.bootstrap()
        expected = {
            "construction": "review_construction_exception",
            "manufacturing": "review_manufacturing_quality_exception",
            "healthcare": "review_healthcare_authorization_exception",
        }
        for domain, action_type in expected.items():
            tenant = f"{domain}-demo"
            with self.subTest(domain=domain):
                status, result, _ = self.request(
                    "POST",
                    f"/api/demo/{domain}",
                    {"tenant_id": tenant},
                    token=token,
                    tenant=tenant,
                )
                self.assertEqual(status, 201)
                self.assertEqual(result["receipt"]["imported_count"], 2)
                self.assertEqual(result["receipt"]["domain"], domain)

                status, proposals, _ = self.request(
                    "GET",
                    f"/api/proposals?tenant_id={tenant}&status=pending",
                )
                self.assertEqual(status, 200)
                self.assertEqual(proposals["items"][0]["action_type"], action_type)

                status, maps, _ = self.request(
                    "GET",
                    f"/api/cartography?tenant_id={tenant}&lane={domain}_operations",
                )
                self.assertEqual(status, 200)
                self.assertEqual(len(maps["items"]), 2)

    def test_guarded_backup_and_restore_workflow(self) -> None:
        token = self.bootstrap()
        tenant = "recovery-demo"
        status, _, _ = self.request(
            "POST",
            "/api/backups",
            {"tenant_id": tenant},
            tenant=tenant,
        )
        self.assertEqual(status, 403)

        status, backup, _ = self.request(
            "POST",
            "/api/backups",
            {"tenant_id": tenant},
            token=token,
            tenant=tenant,
        )
        self.assertEqual(status, 201)
        self.assertEqual(backup["status"], "verified")

        status, _, _ = self.request(
            "POST",
            "/api/demo/construction",
            {"tenant_id": tenant},
            token=token,
            tenant=tenant,
        )
        self.assertEqual(status, 201)
        status, overview, _ = self.request(
            "GET",
            f"/api/overview?tenant_id={tenant}",
        )
        self.assertEqual(status, 200)
        self.assertEqual(overview["observations"], 2)

        backup_id = backup["backup_id"]
        status, _, _ = self.request(
            "POST",
            f"/api/backups/{backup_id}/restore",
            {"tenant_id": tenant, "confirmation": "RESTORE something-else"},
            token=token,
            tenant=tenant,
        )
        self.assertEqual(status, 400)
        status, overview, _ = self.request(
            "GET",
            f"/api/overview?tenant_id={tenant}",
        )
        self.assertEqual(overview["observations"], 2)

        status, restored, _ = self.request(
            "POST",
            f"/api/backups/{backup_id}/restore",
            {
                "tenant_id": tenant,
                "confirmation": f"RESTORE {backup_id}",
            },
            token=token,
            tenant=tenant,
        )
        self.assertEqual(status, 200)
        self.assertEqual(restored["restored_backup_id"], backup_id)
        self.assertNotEqual(restored["safety_backup_id"], backup_id)

        status, overview, _ = self.request(
            "GET",
            f"/api/overview?tenant_id={tenant}",
        )
        self.assertEqual(status, 200)
        self.assertEqual(overview["observations"], 0)
        status, backups, _ = self.request("GET", "/api/backups")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(backups["items"]), 2)

    def test_server_refuses_non_local_binding(self) -> None:
        with self.assertRaisesRegex(ValueError, "localhost"):
            create_server(
                database=Path(self.temp.name) / "unsafe.db",
                host="0.0.0.0",
                port=0,
            )


if __name__ == "__main__":
    unittest.main()
