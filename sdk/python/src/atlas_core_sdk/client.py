from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from atlas_core import ActionExecutor, IndustryAdapter, preview_file

from .contracts import (
    ApprovalReceipt,
    AutomationStatus,
    AuditEvent,
    BackupReceipt,
    BackupRetentionReceipt,
    CartographyCard,
    IntelligenceAnswer,
    IngestRequest,
    EncryptedBackupArtifact,
    ExecutionReceipt,
    IngestResponse,
    ImportReceipt,
    FileImportPreview,
    FileRecordPreview,
    Proposal,
    ProviderCertificationReceipt,
    RestoreReceipt,
    TenantOverview,
)
from .transport import AtlasTransport


class AtlasClient:
    """Stable application entry point for Atlas Core."""

    def __init__(self, transport: AtlasTransport) -> None:
        self._transport = transport

    @classmethod
    def local(
        cls,
        *,
        database: str | Path,
        adapters: tuple[IndustryAdapter, ...],
        executors: tuple[ActionExecutor, ...] = (),
        automation_config: str | Path | None = None,
    ) -> AtlasClient:
        from .local import LocalAtlasTransport

        return cls(
            LocalAtlasTransport(
                database=database,
                adapters=adapters,
                executors=executors,
                automation_config=automation_config,
            )
        )

    @classmethod
    def remote(
        cls,
        *,
        base_url: str,
        api_token: str,
        timeout: float = 15.0,
        ca_file: str | None = None,
        allow_insecure_loopback: bool = False,
    ) -> AtlasClient:
        from .http import HttpAtlasTransport

        return cls(
            HttpAtlasTransport(
                base_url=base_url,
                api_token=api_token,
                timeout=timeout,
                ca_file=ca_file,
                allow_insecure_loopback=allow_insecure_loopback,
            )
        )

    def ingest(
        self,
        *,
        domain: str,
        tenant_id: str,
        source_system: str,
        record: Mapping[str, Any],
    ) -> IngestResponse:
        request = IngestRequest(
            domain=domain,
            tenant_id=tenant_id,
            source_system=source_system,
            record=dict(record),
        )
        return self._transport.ingest(request)

    def answer(
        self,
        *,
        tenant_id: str,
        query: str,
        limit: int = 5,
    ) -> IntelligenceAnswer:
        return self._transport.answer(
            tenant_id=tenant_id,
            query=query,
            limit=limit,
        )

    def approve(
        self,
        proposal_id: str,
        *,
        reason: str,
        decided_by: str,
        tenant_id: str | None = None,
    ) -> ApprovalReceipt:
        return self._transport.decide(
            proposal_id,
            approved=True,
            reason=reason,
            decided_by=decided_by,
            tenant_id=tenant_id,
        )

    def reject(
        self,
        proposal_id: str,
        *,
        reason: str,
        decided_by: str,
        tenant_id: str | None = None,
    ) -> ApprovalReceipt:
        return self._transport.decide(
            proposal_id,
            approved=False,
            reason=reason,
            decided_by=decided_by,
            tenant_id=tenant_id,
        )

    def audit_events(self, *, tenant_id: str | None = None) -> tuple[AuditEvent, ...]:
        return self._transport.audit_events(tenant_id=tenant_id)

    def automation_status(self) -> AutomationStatus:
        return self._transport.automation_status()

    def executions(
        self,
        *,
        tenant_id: str,
        status: str | None = None,
        limit: int = 100,
    ) -> tuple[ExecutionReceipt, ...]:
        return self._transport.executions(
            tenant_id=tenant_id,
            status=status,
            limit=limit,
        )

    def provider_readiness(
        self,
        *,
        certify_local_sandboxes: bool = False,
    ) -> tuple[ProviderCertificationReceipt, ...]:
        """Return safe adapter manifests and certification checks.

        Remote transports never permit dynamic certification; that work must
        run on the trusted Atlas runtime.
        """

        return self._transport.provider_readiness(
            certify_local_sandboxes=certify_local_sandboxes,
        )

    def queue_execution(
        self,
        proposal_id: str,
        *,
        tenant_id: str,
        executor_id: str,
        requested_by: str,
        confirmation: str,
    ) -> ExecutionReceipt:
        return self._transport.queue_execution(
            proposal_id,
            tenant_id=tenant_id,
            executor_id=executor_id,
            requested_by=requested_by,
            confirmation=confirmation,
        )

    def run_execution(
        self,
        execution_id: str,
        *,
        tenant_id: str,
        confirmation: str,
    ) -> ExecutionReceipt:
        return self._transport.run_execution(
            execution_id,
            tenant_id=tenant_id,
            confirmation=confirmation,
        )

    def rollback_execution(
        self,
        execution_id: str,
        *,
        tenant_id: str,
        confirmation: str,
    ) -> ExecutionReceipt:
        return self._transport.rollback_execution(
            execution_id,
            tenant_id=tenant_id,
            confirmation=confirmation,
        )

    def reconcile_execution(
        self,
        execution_id: str,
        *,
        tenant_id: str,
        outcome: str,
        note: str,
        reconciled_by: str,
        external_reference: str = "",
        confirmation: str,
    ) -> ExecutionReceipt:
        return self._transport.reconcile_execution(
            execution_id,
            tenant_id=tenant_id,
            outcome=outcome,
            note=note,
            reconciled_by=reconciled_by,
            external_reference=external_reference,
            confirmation=confirmation,
        )

    def search_cartography(
        self,
        *,
        tenant_id: str,
        query: str = "",
        lane: str | None = None,
        limit: int = 20,
    ) -> tuple[CartographyCard, ...]:
        return self._transport.search_cartography(
            tenant_id=tenant_id,
            query=query,
            lane=lane,
            limit=limit,
        )

    def proposals(
        self,
        *,
        tenant_id: str,
        status: str | None = None,
        limit: int = 100,
    ) -> tuple[Proposal, ...]:
        return self._transport.proposals(
            tenant_id=tenant_id,
            status=status,
            limit=limit,
        )

    def overview(self, *, tenant_id: str) -> TenantOverview:
        return self._transport.overview(tenant_id=tenant_id)

    def import_batch(
        self,
        *,
        tenant_id: str,
        connector_id: str,
        domain: str,
        source_system: str,
        external_batch_id: str,
        records: tuple[dict[str, object], ...],
    ) -> ImportReceipt:
        return self._transport.import_batch(
            tenant_id=tenant_id,
            connector_id=connector_id,
            domain=domain,
            source_system=source_system,
            external_batch_id=external_batch_id,
            records=records,
        )

    @staticmethod
    def preview_file_import(
        content: bytes,
        *,
        format: str,
        source_name: str,
    ) -> FileImportPreview:
        preview = preview_file(
            content,
            format=format,
            source_name=source_name,
        )
        return FileImportPreview(
            format=preview.format,
            source_name=preview.source_name,
            sha256=preview.sha256,
            size_bytes=preview.size_bytes,
            record_count=preview.record_count,
            fields=preview.fields,
            records=tuple(
                FileRecordPreview(
                    record_id=record.record_id,
                    version=record.version,
                    payload=record.payload,
                )
                for record in preview.records
            ),
            external_batch_id=f"file_{preview.sha256[:24]}",
        )

    def import_file(
        self,
        content: bytes,
        *,
        format: str,
        source_name: str,
        tenant_id: str,
        connector_id: str,
        domain: str,
        source_system: str,
        external_batch_id: str | None = None,
    ) -> ImportReceipt:
        preview = self.preview_file_import(
            content,
            format=format,
            source_name=source_name,
        )
        return self.import_batch(
            tenant_id=tenant_id,
            connector_id=connector_id,
            domain=domain,
            source_system=source_system,
            external_batch_id=external_batch_id or preview.external_batch_id,
            records=tuple(
                {
                    "record_id": record.record_id,
                    "version": record.version,
                    "payload": record.payload,
                }
                for record in preview.records
            ),
        )

    def imports(
        self,
        *,
        tenant_id: str,
        limit: int = 100,
    ) -> tuple[ImportReceipt, ...]:
        return self._transport.imports(tenant_id=tenant_id, limit=limit)

    def create_backup(self) -> BackupReceipt:
        return self._transport.create_backup()

    def backups(self) -> tuple[BackupReceipt, ...]:
        return self._transport.backups()

    def restore_backup(
        self,
        backup_id: str,
        *,
        confirmation: str,
    ) -> RestoreReceipt:
        return self._transport.restore_backup(
            backup_id,
            confirmation=confirmation,
        )

    def export_encrypted_backup(
        self,
        backup_id: str,
        *,
        passphrase: str,
    ) -> EncryptedBackupArtifact:
        return self._transport.export_encrypted_backup(
            backup_id,
            passphrase=passphrase,
        )

    def import_encrypted_backup(
        self,
        artifact: bytes,
        *,
        passphrase: str,
    ) -> BackupReceipt:
        return self._transport.import_encrypted_backup(
            artifact,
            passphrase=passphrase,
        )

    def prune_backups(
        self,
        *,
        keep_last: int,
        confirmation: str = "",
        dry_run: bool = True,
    ) -> BackupRetentionReceipt:
        return self._transport.prune_backups(
            keep_last=keep_last,
            confirmation=confirmation,
            dry_run=dry_run,
        )
