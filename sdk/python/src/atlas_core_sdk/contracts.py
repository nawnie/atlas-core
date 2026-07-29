from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class IngestRequest:
    domain: str
    tenant_id: str
    source_system: str
    record: dict[str, Any]

    def __post_init__(self) -> None:
        for name in ("domain", "tenant_id", "source_system"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be blank.")
        if not self.record:
            raise ValueError("record must not be empty.")


@dataclass(frozen=True)
class Change:
    field: str
    before: Any
    after: Any
    kind: str


@dataclass(frozen=True)
class Proposal:
    id: str
    tenant_id: str
    domain: str
    action_type: str
    entity_type: str
    entity_id: str
    reason: str
    payload: dict[str, Any]
    risk: str
    requested_by: str
    approval_status: str
    policy_reason: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class IngestResponse:
    observation_id: str
    tenant_id: str
    domain: str
    entity_type: str
    entity_id: str
    summary: str
    changes: tuple[Change, ...] = ()
    proposals: tuple[Proposal, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ApprovalReceipt:
    proposal_id: str
    status: str
    reason: str
    decided_by: str
    decided_at: str


@dataclass(frozen=True)
class ExecutionReceipt:
    execution_id: str
    proposal_id: str
    tenant_id: str
    executor_id: str
    action_type: str
    idempotency_key: str
    status: str
    attempt_count: int
    requested_by: str
    queued_at: str
    started_at: str = ""
    completed_at: str = ""
    result_summary: str = ""
    external_reference: str = ""
    error: str = ""
    rollback_status: str = ""
    rollback_at: str = ""
    replayed: bool = False


@dataclass(frozen=True)
class ProviderCertificationCheck:
    name: str
    status: str
    detail: str


@dataclass(frozen=True)
class ProviderCertificationReceipt:
    certification_id: str
    provider_id: str
    display_name: str
    domain: str
    environment: str
    status: str
    manifest_sha256: str
    action_types: tuple[str, ...]
    supports_rollback: bool
    network_access: bool
    credential_strategy: str
    synthetic_only: bool
    live_action_authorized: bool
    checks: tuple[ProviderCertificationCheck, ...]
    generated_at: str


@dataclass(frozen=True)
class AuditEvent:
    sequence: int
    event_type: str
    tenant_id: str
    subject_id: str
    detail: dict[str, Any] = field(repr=False)
    occurred_at: str = ""


@dataclass(frozen=True)
class CartographyCard:
    card_id: str
    tenant_id: str
    project: str
    lane: str
    title: str
    summary: str
    source_pointers: tuple[str, ...]
    tags: tuple[str, ...]
    must_retrieve: tuple[str, ...]
    decisions: tuple[str, ...]
    verification: tuple[str, ...]
    unresolved: tuple[str, ...]
    details: str
    confidence: float
    risk_level: str
    current_verification_required: bool
    created_at_utc: str


@dataclass(frozen=True)
class TenantOverview:
    tenant_id: str
    observations: int
    changes: int
    proposals: int
    pending: int
    approved: int
    rejected: int
    cartography_cards: int
    audit_events: int
    import_batches: int
    executions: int = 0
    execution_attention: int = 0


@dataclass(frozen=True)
class ImportRecord:
    record_id: str
    version: str
    status: str
    observation_id: str | None
    fingerprint: str


@dataclass(frozen=True)
class ImportReceipt:
    batch_id: str
    external_batch_id: str
    tenant_id: str
    connector_id: str
    domain: str
    source_system: str
    status: str
    record_count: int
    imported_count: int
    duplicate_count: int
    started_at: str
    completed_at: str
    payload_hash: str
    records: tuple[ImportRecord, ...]
    replayed: bool
    error: str


@dataclass(frozen=True)
class FileRecordPreview:
    record_id: str
    version: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class FileImportPreview:
    format: str
    source_name: str
    sha256: str
    size_bytes: int
    record_count: int
    fields: tuple[str, ...]
    records: tuple[FileRecordPreview, ...]
    external_batch_id: str


@dataclass(frozen=True)
class BackupReceipt:
    backup_id: str
    created_at: str
    filename: str
    size_bytes: int
    sha256: str
    schema_version: int
    integrity_check: str
    status: str
    error: str


@dataclass(frozen=True)
class RestoreReceipt:
    restored_backup_id: str
    safety_backup_id: str
    restored_at: str
    sha256: str
    integrity_check: str


@dataclass(frozen=True)
class EncryptedBackupArtifact:
    export_id: str
    backup_id: str
    created_at: str
    filename: str
    size_bytes: int
    sha256: str
    cipher: str
    kdf: str
    data: bytes = field(repr=False)


@dataclass(frozen=True)
class BackupRetentionReceipt:
    keep_last: int
    candidate_backup_ids: tuple[str, ...]
    deleted_backup_ids: tuple[str, ...]
    reclaimed_bytes: int
    dry_run: bool
    applied_at: str


@dataclass(frozen=True)
class AutomationEvent:
    event_id: str
    run_id: str
    event_type: str
    status: str
    occurred_at: str
    job_id: str
    source_name: str
    sha256: str
    detail: dict[str, Any]
    error: str


@dataclass(frozen=True)
class AutomationRunReceipt:
    run_id: str
    started_at: str
    completed_at: str
    status: str
    imported_files: int
    rejected_files: int
    replayed_files: int
    skipped_files: int
    backup_created: bool
    events: tuple[AutomationEvent, ...]
    portable_exported: bool = False


@dataclass(frozen=True)
class AutomationStatus:
    configured: bool
    config_path: str
    state_directory: str
    locked: bool
    last_run: AutomationRunReceipt | None
    recent_events: tuple[AutomationEvent, ...]


@dataclass(frozen=True)
class AnswerCitation:
    citation_id: str
    evidence_type: str
    subject_id: str
    domain: str
    entity_type: str
    entity_id: str
    source_pointer: str
    occurred_at: str
    excerpt: str


@dataclass(frozen=True)
class AnswerClaim:
    text: str
    citation_ids: tuple[str, ...]


@dataclass(frozen=True)
class AnswerSection:
    title: str
    claims: tuple[AnswerClaim, ...]


@dataclass(frozen=True)
class IntelligenceAnswer:
    tenant_id: str
    query: str
    headline: str
    sections: tuple[AnswerSection, ...]
    citations: tuple[AnswerCitation, ...]
    confidence: float
    generated_at: str
    limitations: tuple[str, ...]
