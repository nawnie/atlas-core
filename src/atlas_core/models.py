from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    BLOCKED = "blocked"


class ExecutionStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_FAILED = "rollback_failed"


@dataclass(frozen=True)
class CanonicalObservation:
    tenant_id: str
    domain: str
    entity_type: str
    entity_id: str
    source_system: str
    source_record_id: str
    observed_at: str
    attributes: dict[str, Any]
    source_version: str | None = None
    id: str | None = None
    ingested_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class FieldChange:
    field: str
    before: Any
    after: Any
    kind: str


@dataclass(frozen=True)
class ChangeSet:
    tenant_id: str
    domain: str
    entity_type: str
    entity_id: str
    previous_observation_id: str | None
    current_observation_id: str
    changes: tuple[FieldChange, ...]
    summary: str


@dataclass(frozen=True)
class ActionProposal:
    tenant_id: str
    domain: str
    action_type: str
    entity_type: str
    entity_id: str
    reason: str
    payload: dict[str, Any]
    risk: RiskLevel
    requested_by: str = "atlas-core"
    id: str | None = None


@dataclass(frozen=True)
class ApprovalDecision:
    proposal_id: str
    status: ApprovalStatus
    reason: str
    decided_by: str
    decided_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class StoredProposal:
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
    status: str
    policy_reason: str
    created_at: str


@dataclass(frozen=True)
class ActionExecutionRequest:
    execution_id: str
    proposal_id: str
    tenant_id: str
    domain: str
    action_type: str
    entity_type: str
    entity_id: str
    payload: dict[str, Any] = field(repr=False)
    idempotency_key: str = ""
    attempt: int = 1


@dataclass(frozen=True)
class ActionExecutionResult:
    succeeded: bool
    summary: str
    external_reference: str = ""
    error: str = ""


@dataclass(frozen=True)
class ActionExecutionReceipt:
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
class ConnectorRecord:
    record_id: str
    version: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class ImportRecordReceipt:
    record_id: str
    version: str
    status: str
    observation_id: str | None = None
    fingerprint: str = ""


@dataclass(frozen=True)
class ImportBatchReceipt:
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
    records: tuple[ImportRecordReceipt, ...] = ()
    replayed: bool = False
    error: str = ""


@dataclass(frozen=True)
class BackupReceipt:
    backup_id: str
    created_at: str
    filename: str
    size_bytes: int
    sha256: str
    schema_version: int
    integrity_check: str
    status: str = "verified"
    error: str = ""


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
    generated_at: str = field(default_factory=utc_now)
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True)
class CartographyCard:
    tenant_id: str
    project: str
    lane: str
    title: str
    summary: str
    source_pointers: tuple[str, ...]
    tags: tuple[str, ...] = ()
    must_retrieve: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    verification: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    details: str = ""
    confidence: float = 1.0
    risk_level: str = "low"
    current_verification_required: bool = False
    card_id: str | None = None
    created_at_utc: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0.")
        if self.risk_level not in {"low", "medium", "high"}:
            raise ValueError("risk_level must be low, medium, or high.")
        if not self.lane or not self.lane.replace("_", "").isalnum():
            raise ValueError("lane must be a stable snake_case identifier.")
