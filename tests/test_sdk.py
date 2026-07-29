from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from atlas_core import ActionExecutionResult, IndustryAdapter
from atlas_core.adapters import HotelAdapter
from atlas_core_sdk import (
    AtlasClient,
    ExecutionConflictError,
    InvalidRequestError,
    ProposalConflictError,
)


class SdkSyntheticExecutor:
    executor_id = "sdk-synthetic"
    action_types = frozenset({"review_cancelled_reservation"})
    supports_rollback = False

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, request):
        self.calls += 1
        return ActionExecutionResult(
            succeeded=True,
            summary="SDK synthetic action completed.",
            external_reference=request.idempotency_key,
        )

    def rollback(self, request, receipt):
        raise AssertionError("Rollback must not be called.")


class AtlasSdkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.client = AtlasClient.local(
            database=Path(self.temp.name) / "sdk.db",
            adapters=(HotelAdapter(),),
        )
        self.base = {
            "reservation_id": "SDK-1",
            "version": "1",
            "updated_at": "2026-07-24T12:00:00Z",
            "arrival_date": "2026-08-10",
            "departure_date": "2026-08-13",
            "guest_count": 2,
            "room_type": "king",
            "status": "confirmed",
            "total": "729",
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def ingest(self, record: dict[str, object]):
        return self.client.ingest(
            domain="hotel",
            tenant_id="hotel-sdk",
            source_system="fixture-pms",
            record=record,
        )

    def test_public_adapter_contract_is_exported(self) -> None:
        self.assertTrue(issubclass(HotelAdapter, IndustryAdapter))
        from atlas_core_sdk import (
            CredentialStrategy,
            HotelPmsSandbox,
            HotelSandboxReservation,
            ProviderCertificationRunner,
            ProviderEnvironment,
            ProviderManifest,
        )

        self.assertTrue(HotelPmsSandbox)
        self.assertTrue(HotelSandboxReservation)
        self.assertTrue(CredentialStrategy)
        self.assertTrue(ProviderCertificationRunner)
        self.assertTrue(ProviderEnvironment)
        self.assertTrue(ProviderManifest)

    def test_sdk_certifies_local_sandbox_and_exposes_safe_receipt(self) -> None:
        from atlas_core_sdk import HotelPmsSandbox

        provider = HotelPmsSandbox(Path(self.temp.name) / "sdk-provider.db")
        client = AtlasClient.local(
            database=Path(self.temp.name) / "sdk-provider-memory.db",
            adapters=(HotelAdapter(),),
            executors=(provider,),
        )
        static = client.provider_readiness()
        certified = client.provider_readiness(certify_local_sandboxes=True)
        self.assertEqual(static[0].status, "ready_for_conformance")
        self.assertEqual(certified[0].status, "sandbox_certified")
        self.assertTrue(certified[0].manifest_sha256)
        self.assertTrue(
            all(check.status == "passed" for check in certified[0].checks)
        )
        self.assertFalse(hasattr(certified[0], "credential"))
        self.assertFalse(hasattr(certified[0], "endpoint"))

    def test_sdk_maps_ingestion_without_exposing_database_objects(self) -> None:
        response = self.ingest(self.base)
        self.assertTrue(response.observation_id.startswith("obs_"))
        self.assertFalse(hasattr(response, "connection"))
        self.assertEqual(response.proposals, ())

    def test_sdk_returns_structured_cited_answer(self) -> None:
        self.ingest(self.base)
        self.ingest(
            {
                **self.base,
                "version": "2",
                "updated_at": "2026-07-24T12:05:00Z",
                "status": "cancelled",
            }
        )
        answer = self.client.answer(
            tenant_id="hotel-sdk",
            query="What changed with SDK-1?",
        )
        self.assertEqual(len(answer.sections), 3)
        self.assertTrue(answer.citations)
        self.assertTrue(
            all(citation.entity_id == "SDK-1" for citation in answer.citations)
        )

    def test_sdk_cancellation_can_be_approved_once(self) -> None:
        self.ingest(self.base)
        response = self.ingest(
            {
                **self.base,
                "version": "2",
                "updated_at": "2026-07-24T12:05:00Z",
                "status": "cancelled",
            }
        )
        proposal = response.proposals[0]
        self.assertEqual(proposal.approval_status, "pending")
        receipt = self.client.approve(
            proposal.id,
            reason="Manager verified the cancellation.",
            decided_by="manager@example.test",
        )
        self.assertEqual(receipt.status, "approved")
        with self.assertRaises(ProposalConflictError):
            self.client.approve(
                proposal.id,
                reason="Duplicate decision.",
                decided_by="manager@example.test",
            )

    def test_sdk_translates_validation_errors(self) -> None:
        with self.assertRaises(InvalidRequestError):
            self.ingest({**self.base, "reservation_id": ""})

    def test_sdk_filters_audit_events_by_tenant(self) -> None:
        self.ingest(self.base)
        events = self.client.audit_events(tenant_id="hotel-sdk")
        self.assertGreaterEqual(len(events), 2)
        self.assertTrue(all(event.tenant_id == "hotel-sdk" for event in events))
        self.assertEqual(self.client.audit_events(tenant_id="other"), ())

    def test_sdk_lists_proposals_and_overview_by_tenant(self) -> None:
        self.ingest(self.base)
        self.ingest(
            {
                **self.base,
                "version": "2",
                "updated_at": "2026-07-24T12:05:00Z",
                "status": "cancelled",
            }
        )
        proposals = self.client.proposals(tenant_id="hotel-sdk", status="pending")
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].tenant_id, "hotel-sdk")
        self.assertEqual(self.client.proposals(tenant_id="other"), ())
        overview = self.client.overview(tenant_id="hotel-sdk")
        self.assertEqual(overview.observations, 2)
        self.assertEqual(overview.pending, 1)
        self.assertEqual(overview.cartography_cards, 2)

    def test_tenant_bound_decision_does_not_cross_company_boundary(self) -> None:
        self.ingest(self.base)
        response = self.ingest(
            {
                **self.base,
                "version": "2",
                "updated_at": "2026-07-24T12:05:00Z",
                "status": "cancelled",
            }
        )
        proposal_id = response.proposals[0].id
        with self.assertRaises(ProposalConflictError):
            self.client.approve(
                proposal_id,
                reason="Wrong tenant.",
                decided_by="operator",
                tenant_id="other-company",
            )
        self.assertEqual(
            len(self.client.proposals(tenant_id="hotel-sdk", status="pending")),
            1,
        )

    def test_cartographer_records_mapping_lineage_and_unresolved_approval(self) -> None:
        self.ingest(self.base)
        self.ingest(
            {
                **self.base,
                "version": "2",
                "updated_at": "2026-07-24T12:05:00Z",
                "status": "cancelled",
            }
        )
        cards = self.client.search_cartography(
            tenant_id="hotel-sdk",
            lane="hotel_operations",
            query="1 field(s) changed",
        )
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertTrue(card.card_id.startswith("map_"))
        self.assertIn("fixture-pms:SDK-1:2", card.source_pointers)
        self.assertIn('"status": "attributes.status"', card.details)
        self.assertEqual(card.risk_level, "medium")
        self.assertEqual(len(card.unresolved), 1)

    def test_official_sdk_controls_execution_outbox_without_payload_receipts(self) -> None:
        executor = SdkSyntheticExecutor()
        database = Path(self.temp.name) / "execution-sdk.db"
        client = AtlasClient.local(
            database=database,
            adapters=(HotelAdapter(),),
            executors=(executor,),
        )
        first = client.ingest(
            domain="hotel",
            tenant_id="hotel-sdk-execution",
            source_system="fixture-pms",
            record={**self.base, "reservation_id": "SDK-EXEC-1"},
        )
        self.assertEqual(first.proposals, ())
        changed = client.ingest(
            domain="hotel",
            tenant_id="hotel-sdk-execution",
            source_system="fixture-pms",
            record={
                **self.base,
                "reservation_id": "SDK-EXEC-1",
                "version": "2",
                "updated_at": "2026-07-24T12:05:00Z",
                "status": "cancelled",
            },
        )
        proposal_id = changed.proposals[0].id
        client.approve(
            proposal_id,
            tenant_id="hotel-sdk-execution",
            reason="SDK synthetic review passed.",
            decided_by="sdk-reviewer",
        )
        with self.assertRaises(ExecutionConflictError):
            client.queue_execution(
                proposal_id,
                tenant_id="hotel-sdk-execution",
                executor_id=executor.executor_id,
                requested_by="sdk-operator",
                confirmation="queue it",
            )
        queued = client.queue_execution(
            proposal_id,
            tenant_id="hotel-sdk-execution",
            executor_id=executor.executor_id,
            requested_by="sdk-operator",
            confirmation=(
                f"QUEUE EXECUTION {proposal_id} WITH {executor.executor_id}"
            ),
        )
        completed = client.run_execution(
            queued.execution_id,
            tenant_id="hotel-sdk-execution",
            confirmation=f"EXECUTE {queued.execution_id}",
        )
        self.assertEqual(completed.status, "succeeded")
        self.assertEqual(executor.calls, 1)
        self.assertEqual(
            client.executions(tenant_id="hotel-sdk-execution"),
            (completed,),
        )
        self.assertFalse(hasattr(completed, "payload"))
        overview = client.overview(tenant_id="hotel-sdk-execution")
        self.assertEqual(overview.executions, 1)
        self.assertEqual(overview.execution_attention, 0)


if __name__ == "__main__":
    unittest.main()
