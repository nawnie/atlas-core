from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
from typing import Any

from .approval import ApprovalPolicy
from .cartographer import Cartographer
from .change import compare
from .connectors import ReadOnlyConnector
from .memory import CompanyMemory
from .models import (
    ActionProposal,
    CanonicalObservation,
    ChangeSet,
    ImportBatchReceipt,
    ImportRecordReceipt,
    IntelligenceAnswer,
    utc_now,
)
from .retrieval import IntelligenceRetriever
from .translator import IndustryAdapter


@dataclass(frozen=True)
class IngestionResult:
    observation: CanonicalObservation
    changes: ChangeSet
    proposals: tuple[ActionProposal, ...]


class AtlasCore:
    def __init__(
        self,
        database: str | Path,
        adapters: tuple[IndustryAdapter, ...],
        policy: ApprovalPolicy | None = None,
    ) -> None:
        self.memory = CompanyMemory(database)
        self.cartographer = Cartographer(self.memory)
        self.intelligence = IntelligenceRetriever(self.memory)
        self.adapters = {adapter.domain: adapter for adapter in adapters}
        self.policy = policy or ApprovalPolicy()

    def answer(
        self,
        *,
        tenant_id: str,
        query: str,
        limit: int = 5,
    ) -> IntelligenceAnswer:
        return self.intelligence.answer(
            tenant_id=tenant_id,
            query=query,
            limit=limit,
        )

    @staticmethod
    def _hash(payload: object) -> str:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def ingest(
        self,
        domain: str,
        record: dict[str, Any],
        *,
        tenant_id: str,
        source_system: str,
    ) -> IngestionResult:
        try:
            adapter = self.adapters[domain]
        except KeyError as error:
            raise ValueError(f"No adapter registered for domain: {domain}") from error
        translated = adapter.translate(
            record,
            tenant_id=tenant_id,
            source_system=source_system,
        )
        previous = self.memory.latest_observation(
            translated.tenant_id,
            translated.domain,
            translated.entity_type,
            translated.entity_id,
        )
        current = self.memory.append_observation(translated)
        changes = compare(previous, current)
        self.memory.append_change(changes)
        proposals = adapter.propose_actions(changes)
        stored_proposals = []
        for proposal in proposals:
            policy_result = self.policy.evaluate(proposal)
            stored_proposals.append(
                self.memory.append_proposal(
                    proposal,
                    policy_result.status,
                    policy_result.reason,
                )
            )
        self.cartographer.record_ingestion(
            observation=current,
            changes=changes,
            adapter=adapter,
            proposal_ids=tuple(
                proposal.id for proposal in stored_proposals if proposal.id is not None
            ),
        )
        return IngestionResult(current, changes, tuple(stored_proposals))

    def import_batch(
        self,
        connector: ReadOnlyConnector,
        *,
        tenant_id: str,
        external_batch_id: str,
    ) -> ImportBatchReceipt:
        tenant_id = tenant_id.strip()
        external_batch_id = external_batch_id.strip()
        if not tenant_id or not external_batch_id:
            raise ValueError("tenant_id and external_batch_id are required.")
        if len(tenant_id) > 100 or len(external_batch_id) > 200:
            raise ValueError("tenant_id or external_batch_id is too long.")
        records = tuple(connector.read())
        if not records:
            raise ValueError("Import batch must contain at least one record.")
        if len(records) > 1000:
            raise ValueError("Import batch may contain at most 1000 records.")
        keys = [(record.record_id, record.version) for record in records]
        if len(keys) != len(set(keys)):
            raise ValueError("Import batch contains duplicate record_id/version pairs.")
        batch_payload = [
            {
                "record_id": record.record_id,
                "version": record.version,
                "payload": record.payload,
            }
            for record in records
        ]
        encoded_size = len(
            json.dumps(batch_payload, sort_keys=True, default=str).encode()
        )
        if encoded_size > 5 * 1024 * 1024:
            raise ValueError("Import batch payload may not exceed 5 MiB.")
        payload_hash = self._hash(batch_payload)
        batch_id = f"imp_{self._hash({
            'tenant_id': tenant_id,
            'connector_id': connector.connector_id,
            'external_batch_id': external_batch_id,
            'payload_hash': payload_hash,
        })[:20]}"
        started_at = utc_now()
        initial = ImportBatchReceipt(
            batch_id=batch_id,
            external_batch_id=external_batch_id,
            tenant_id=tenant_id,
            connector_id=connector.connector_id,
            domain=connector.domain,
            source_system=connector.source_system,
            status="processing",
            record_count=len(records),
            imported_count=0,
            duplicate_count=0,
            started_at=started_at,
            completed_at="",
            payload_hash=payload_hash,
        )
        item_receipts: list[ImportRecordReceipt] = []
        imported_count = 0
        duplicate_count = 0
        try:
            with self.memory.transaction():
                existing = self.memory.find_import_batch(
                    tenant_id=tenant_id,
                    connector_id=connector.connector_id,
                    external_batch_id=external_batch_id,
                )
                if existing is not None:
                    if existing.payload_hash != payload_hash:
                        raise ValueError(
                            "external_batch_id was already used with different content."
                        )
                    self.memory.record_import_replay(existing)
                    return replace(existing, replayed=True)
                self.memory.start_import_batch(initial)
                for record in records:
                    fingerprint = self._hash(
                        {
                            "record_id": record.record_id,
                            "version": record.version,
                            "payload": record.payload,
                        }
                    )
                    previous_observation_id = self.memory.connector_record_observation(
                        tenant_id=tenant_id,
                        connector_id=connector.connector_id,
                        record_id=record.record_id,
                        version=record.version,
                        fingerprint=fingerprint,
                    )
                    if previous_observation_id:
                        item = ImportRecordReceipt(
                            record_id=record.record_id,
                            version=record.version,
                            status="duplicate",
                            observation_id=previous_observation_id,
                            fingerprint=fingerprint,
                        )
                        duplicate_count += 1
                    else:
                        payload = dict(record.payload)
                        payload_version = payload.get("version")
                        if payload_version is not None and str(payload_version) != record.version:
                            raise ValueError(
                                f"Record {record.record_id} payload version conflicts "
                                "with connector version."
                            )
                        payload.setdefault("version", record.version)
                        result = self.ingest(
                            connector.domain,
                            payload,
                            tenant_id=tenant_id,
                            source_system=connector.source_system,
                        )
                        item = ImportRecordReceipt(
                            record_id=record.record_id,
                            version=record.version,
                            status="imported",
                            observation_id=result.observation.id,
                            fingerprint=fingerprint,
                        )
                        imported_count += 1
                    self.memory.record_import_item(
                        batch_id=batch_id,
                        tenant_id=tenant_id,
                        connector_id=connector.connector_id,
                        receipt=item,
                    )
                    item_receipts.append(item)
                completed = replace(
                    initial,
                    status="completed",
                    imported_count=imported_count,
                    duplicate_count=duplicate_count,
                    completed_at=utc_now(),
                    records=tuple(item_receipts),
                )
                self.memory.complete_import_batch(completed)
            return completed
        except Exception as error:
            failed = replace(
                initial,
                status="failed",
                completed_at=utc_now(),
                error=str(error)[:1000],
            )
            try:
                with self.memory.transaction():
                    self.memory.start_import_batch(failed)
                    self.memory.complete_import_batch(failed)
            except Exception:
                pass
            raise
