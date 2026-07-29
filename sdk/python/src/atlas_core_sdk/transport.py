from __future__ import annotations

from typing import Protocol

from .contracts import (
    ApprovalReceipt,
    AutomationStatus,
    AuditEvent,
    BackupReceipt,
    BackupRetentionReceipt,
    CartographyCard,
    IntelligenceAnswer,
    IngestRequest,
    IngestResponse,
    EncryptedBackupArtifact,
    ExecutionReceipt,
    ImportReceipt,
    Proposal,
    ProviderCertificationReceipt,
    RestoreReceipt,
    TenantOverview,
)


class AtlasTransport(Protocol):
    def provider_readiness(
        self,
        *,
        certify_local_sandboxes: bool = False,
    ) -> tuple[ProviderCertificationReceipt, ...]: ...

    def executions(
        self,
        *,
        tenant_id: str,
        status: str | None = None,
        limit: int = 100,
    ) -> tuple[ExecutionReceipt, ...]: ...

    def queue_execution(
        self,
        proposal_id: str,
        *,
        tenant_id: str,
        executor_id: str,
        requested_by: str,
        confirmation: str,
    ) -> ExecutionReceipt: ...

    def run_execution(
        self,
        execution_id: str,
        *,
        tenant_id: str,
        confirmation: str,
    ) -> ExecutionReceipt: ...

    def rollback_execution(
        self,
        execution_id: str,
        *,
        tenant_id: str,
        confirmation: str,
    ) -> ExecutionReceipt: ...

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
    ) -> ExecutionReceipt: ...

    def automation_status(self) -> AutomationStatus: ...

    def answer(
        self,
        *,
        tenant_id: str,
        query: str,
        limit: int = 5,
    ) -> IntelligenceAnswer: ...

    def ingest(self, request: IngestRequest) -> IngestResponse: ...

    def decide(
        self,
        proposal_id: str,
        *,
        approved: bool,
        reason: str,
        decided_by: str,
        tenant_id: str | None = None,
    ) -> ApprovalReceipt: ...

    def audit_events(self, *, tenant_id: str | None = None) -> tuple[AuditEvent, ...]: ...

    def search_cartography(
        self,
        *,
        tenant_id: str,
        query: str = "",
        lane: str | None = None,
        limit: int = 20,
    ) -> tuple[CartographyCard, ...]: ...

    def proposals(
        self,
        *,
        tenant_id: str,
        status: str | None = None,
        limit: int = 100,
    ) -> tuple[Proposal, ...]: ...

    def overview(self, *, tenant_id: str) -> TenantOverview: ...

    def import_batch(
        self,
        *,
        tenant_id: str,
        connector_id: str,
        domain: str,
        source_system: str,
        external_batch_id: str,
        records: tuple[dict[str, object], ...],
    ) -> ImportReceipt: ...

    def imports(
        self,
        *,
        tenant_id: str,
        limit: int = 100,
    ) -> tuple[ImportReceipt, ...]: ...

    def create_backup(self) -> BackupReceipt: ...

    def backups(self) -> tuple[BackupReceipt, ...]: ...

    def restore_backup(
        self,
        backup_id: str,
        *,
        confirmation: str,
    ) -> RestoreReceipt: ...

    def export_encrypted_backup(
        self,
        backup_id: str,
        *,
        passphrase: str,
    ) -> EncryptedBackupArtifact: ...

    def import_encrypted_backup(
        self,
        artifact: bytes,
        *,
        passphrase: str,
    ) -> BackupReceipt: ...

    def prune_backups(
        self,
        *,
        keep_last: int,
        confirmation: str = "",
        dry_run: bool = True,
    ) -> BackupRetentionReceipt: ...
