from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, replace
from pathlib import Path
from threading import RLock
from typing import Any, Iterator

from .models import (
    ActionExecutionReceipt,
    ActionExecutionRequest,
    ActionProposal,
    ApprovalDecision,
    ApprovalStatus,
    BackupReceipt,
    BackupRetentionReceipt,
    CanonicalObservation,
    CartographyCard,
    ChangeSet,
    ImportBatchReceipt,
    ImportRecordReceipt,
    RiskLevel,
    RestoreReceipt,
    EncryptedBackupArtifact,
    StoredProposal,
    utc_now,
)
from .recovery import decrypt_recovery_artifact, encrypt_recovery_artifact

SCHEMA_VERSION = 4
BACKUP_ID_PATTERN = re.compile(r"^bkp_[0-9a-f]{20}$")


class CompanyMemory:
    def __init__(self, database: str | Path) -> None:
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.backup_directory = self.database.with_suffix(
            self.database.suffix + ".backups"
        )
        self._lifecycle_lock = RLock()
        self._transaction_connection: ContextVar[sqlite3.Connection | None] = (
            ContextVar("atlas_transaction_connection", default=None)
        )
        self._migrate()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._lifecycle_lock:
            active = self._transaction_connection.get()
            if active is not None:
                yield active
                return
            connection = sqlite3.connect(self.database)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lifecycle_lock:
            if self._transaction_connection.get() is not None:
                yield
                return
            connection = sqlite3.connect(self.database)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            token = self._transaction_connection.set(connection)
            try:
                yield
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                self._transaction_connection.reset(token)
                connection.close()

    def _migrate(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observations (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    source_system TEXT NOT NULL,
                    source_record_id TEXT NOT NULL,
                    source_version TEXT,
                    observed_at TEXT NOT NULL,
                    ingested_at TEXT NOT NULL,
                    attributes_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_observation_entity
                    ON observations(tenant_id, domain, entity_type, entity_id, ingested_at);
                CREATE TABLE IF NOT EXISTS changes (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    current_observation_id TEXT NOT NULL REFERENCES observations(id),
                    previous_observation_id TEXT REFERENCES observations(id),
                    change_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS proposals (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    risk TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    status TEXT NOT NULL,
                    policy_reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approval_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    proposal_id TEXT NOT NULL REFERENCES proposals(id),
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    decided_by TEXT NOT NULL,
                    decided_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cartography_cards (
                    card_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    project TEXT NOT NULL,
                    lane TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    card_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_cartography_lane
                    ON cartography_cards(tenant_id, lane, created_at_utc);
                CREATE TABLE IF NOT EXISTS import_batches (
                    batch_id TEXT PRIMARY KEY,
                    external_batch_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    connector_id TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    source_system TEXT NOT NULL,
                    status TEXT NOT NULL,
                    record_count INTEGER NOT NULL,
                    imported_count INTEGER NOT NULL,
                    duplicate_count INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    error_text TEXT NOT NULL DEFAULT '',
                    UNIQUE(tenant_id, connector_id, external_batch_id)
                );
                CREATE INDEX IF NOT EXISTS idx_import_batches_tenant
                    ON import_batches(tenant_id, started_at);
                CREATE TABLE IF NOT EXISTS import_record_receipts (
                    batch_id TEXT NOT NULL REFERENCES import_batches(batch_id),
                    record_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    observation_id TEXT,
                    fingerprint TEXT NOT NULL,
                    PRIMARY KEY(batch_id, record_id, version)
                );
                CREATE TABLE IF NOT EXISTS connector_record_ledger (
                    tenant_id TEXT NOT NULL,
                    connector_id TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    observation_id TEXT NOT NULL REFERENCES observations(id),
                    first_batch_id TEXT NOT NULL REFERENCES import_batches(batch_id),
                    imported_at TEXT NOT NULL,
                    PRIMARY KEY(tenant_id, connector_id, record_id, version, fingerprint)
                );
                CREATE TABLE IF NOT EXISTS action_executions (
                    execution_id TEXT PRIMARY KEY,
                    proposal_id TEXT NOT NULL UNIQUE REFERENCES proposals(id),
                    tenant_id TEXT NOT NULL,
                    executor_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    requested_by TEXT NOT NULL,
                    queued_at TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT '',
                    completed_at TEXT NOT NULL DEFAULT '',
                    result_summary TEXT NOT NULL DEFAULT '',
                    external_reference TEXT NOT NULL DEFAULT '',
                    error_text TEXT NOT NULL DEFAULT '',
                    rollback_status TEXT NOT NULL DEFAULT '',
                    rollback_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_action_executions_tenant
                    ON action_executions(tenant_id, queued_at);
                """
            )
            row = db.execute("SELECT version FROM schema_meta").fetchone()
            if row is None:
                db.execute("INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION,))
            elif row["version"] in {1, 2, 3}:
                db.execute("UPDATE schema_meta SET version = ?", (SCHEMA_VERSION,))
            elif row["version"] != SCHEMA_VERSION:
                raise RuntimeError(f"Unsupported schema version: {row['version']}")

    @staticmethod
    def _stable_id(prefix: str, payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return f"{prefix}_{hashlib.sha256(encoded.encode()).hexdigest()[:20]}"

    def append_observation(self, observation: CanonicalObservation) -> CanonicalObservation:
        payload = asdict(observation)
        observation_id = self._stable_id("obs", payload)
        stored = replace(observation, id=observation_id)
        with self._connect() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stored.id,
                    stored.tenant_id,
                    stored.domain,
                    stored.entity_type,
                    stored.entity_id,
                    stored.source_system,
                    stored.source_record_id,
                    stored.source_version,
                    stored.observed_at,
                    stored.ingested_at,
                    json.dumps(stored.attributes, sort_keys=True),
                ),
            )
            self._audit(db, "observation.ingested", stored.tenant_id, stored.id or "", payload)
        return stored

    def latest_observation(
        self,
        tenant_id: str,
        domain: str,
        entity_type: str,
        entity_id: str,
        *,
        before_id: str | None = None,
    ) -> CanonicalObservation | None:
        query = """
            SELECT * FROM observations
            WHERE tenant_id = ? AND domain = ? AND entity_type = ? AND entity_id = ?
        """
        params: list[Any] = [tenant_id, domain, entity_type, entity_id]
        if before_id:
            query += " AND id <> ?"
            params.append(before_id)
        query += " ORDER BY ingested_at DESC, rowid DESC LIMIT 1"
        with self._connect() as db:
            row = db.execute(query, params).fetchone()
        if row is None:
            return None
        return CanonicalObservation(
            id=row["id"],
            tenant_id=row["tenant_id"],
            domain=row["domain"],
            entity_type=row["entity_type"],
            entity_id=row["entity_id"],
            source_system=row["source_system"],
            source_record_id=row["source_record_id"],
            source_version=row["source_version"],
            observed_at=row["observed_at"],
            ingested_at=row["ingested_at"],
            attributes=json.loads(row["attributes_json"]),
        )

    def append_change(self, changes: ChangeSet) -> str:
        payload = asdict(changes)
        change_id = self._stable_id("chg", payload)
        with self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO changes VALUES (?, ?, ?, ?, ?, ?)",
                (
                    change_id,
                    changes.tenant_id,
                    changes.current_observation_id,
                    changes.previous_observation_id,
                    json.dumps(payload, sort_keys=True),
                    utc_now(),
                ),
            )
            self._audit(db, "change.detected", changes.tenant_id, change_id, payload)
        return change_id

    def append_proposal(
        self,
        proposal: ActionProposal,
        status: ApprovalStatus,
        policy_reason: str,
    ) -> ActionProposal:
        payload = asdict(proposal)
        proposal_id = self._stable_id("act", payload)
        stored = replace(proposal, id=proposal_id)
        with self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO proposals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    stored.id,
                    stored.tenant_id,
                    stored.domain,
                    stored.action_type,
                    stored.entity_type,
                    stored.entity_id,
                    stored.reason,
                    json.dumps(stored.payload, sort_keys=True),
                    stored.risk.value,
                    stored.requested_by,
                    status.value,
                    policy_reason,
                    utc_now(),
                ),
            )
            self._audit(
                db,
                "proposal.created",
                stored.tenant_id,
                stored.id or "",
                {"status": status.value, "policy_reason": policy_reason, **payload},
            )
        return stored

    def decide(
        self,
        decision: ApprovalDecision,
        *,
        expected_tenant_id: str | None = None,
    ) -> None:
        if decision.status not in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}:
            raise ValueError("A human decision must be approved or rejected.")
        with self._connect() as db:
            row = db.execute(
                "SELECT tenant_id, status FROM proposals WHERE id = ?",
                (decision.proposal_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown proposal: {decision.proposal_id}")
            if expected_tenant_id is not None and row["tenant_id"] != expected_tenant_id:
                raise KeyError(f"Unknown proposal: {decision.proposal_id}")
            if row["status"] != ApprovalStatus.PENDING.value:
                raise ValueError(f"Proposal is not pending: {row['status']}")
            db.execute(
                "UPDATE proposals SET status = ? WHERE id = ?",
                (decision.status.value, decision.proposal_id),
            )
            db.execute(
                """
                INSERT INTO approval_decisions
                (proposal_id, status, reason, decided_by, decided_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    decision.proposal_id,
                    decision.status.value,
                    decision.reason,
                    decision.decided_by,
                    decision.decided_at,
                ),
            )
            self._audit(
                db,
                "proposal.decided",
                row["tenant_id"],
                decision.proposal_id,
                asdict(decision),
            )

    def list_proposals(
        self,
        *,
        tenant_id: str,
        status: str | None = None,
        limit: int = 100,
    ) -> tuple[StoredProposal, ...]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500.")
        allowed = {item.value for item in ApprovalStatus}
        if status is not None and status not in allowed:
            raise ValueError(f"Unknown proposal status: {status}")
        sql = "SELECT * FROM proposals WHERE tenant_id = ?"
        params: list[Any] = [tenant_id]
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        with self._connect() as db:
            rows = db.execute(sql, params).fetchall()
        return tuple(
            StoredProposal(
                id=row["id"],
                tenant_id=row["tenant_id"],
                domain=row["domain"],
                action_type=row["action_type"],
                entity_type=row["entity_type"],
                entity_id=row["entity_id"],
                reason=row["reason"],
                payload=json.loads(row["payload_json"]),
                risk=row["risk"],
                requested_by=row["requested_by"],
                status=row["status"],
                policy_reason=row["policy_reason"],
                created_at=row["created_at"],
            )
            for row in rows
        )

    @staticmethod
    def _execution_receipt(
        row: sqlite3.Row,
        *,
        replayed: bool = False,
    ) -> ActionExecutionReceipt:
        return ActionExecutionReceipt(
            execution_id=row["execution_id"],
            proposal_id=row["proposal_id"],
            tenant_id=row["tenant_id"],
            executor_id=row["executor_id"],
            action_type=row["action_type"],
            idempotency_key=row["idempotency_key"],
            status=row["status"],
            attempt_count=row["attempt_count"],
            requested_by=row["requested_by"],
            queued_at=row["queued_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            result_summary=row["result_summary"],
            external_reference=row["external_reference"],
            error=row["error_text"],
            rollback_status=row["rollback_status"],
            rollback_at=row["rollback_at"],
            replayed=replayed,
        )

    def queue_execution(
        self,
        *,
        proposal_id: str,
        tenant_id: str,
        executor_id: str,
        supported_action_types: frozenset[str],
        requested_by: str,
    ) -> ActionExecutionReceipt:
        requested_by = " ".join(requested_by.split())
        if not requested_by or len(requested_by) > 100:
            raise ValueError("Execution requester must be 1-100 characters.")
        with self.transaction():
            with self._connect() as db:
                proposal = db.execute(
                    """
                    SELECT tenant_id, action_type, status
                    FROM proposals WHERE id = ?
                    """,
                    (proposal_id,),
                ).fetchone()
                if proposal is None or proposal["tenant_id"] != tenant_id:
                    raise KeyError(f"Unknown proposal: {proposal_id}")
                existing = db.execute(
                    "SELECT * FROM action_executions WHERE proposal_id = ?",
                    (proposal_id,),
                ).fetchone()
                if existing is not None:
                    if existing["executor_id"] != executor_id:
                        raise ValueError(
                            "Proposal is already assigned to another executor."
                        )
                    return self._execution_receipt(existing, replayed=True)
                if proposal["status"] != ApprovalStatus.APPROVED.value:
                    raise ValueError(
                        "Only a human-approved proposal can enter execution."
                    )
                if proposal["action_type"] not in supported_action_types:
                    raise ValueError(
                        "Executor does not allow this proposal action type."
                    )
                identity = {
                    "proposal_id": proposal_id,
                    "tenant_id": tenant_id,
                    "executor_id": executor_id,
                    "action_type": proposal["action_type"],
                }
                execution_id = self._stable_id("exe", identity)
                idempotency_key = self._stable_id("idem", identity)
                queued_at = utc_now()
                db.execute(
                    """
                    INSERT INTO action_executions (
                        execution_id, proposal_id, tenant_id, executor_id,
                        action_type, idempotency_key, status, attempt_count,
                        requested_by, queued_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?)
                    """,
                    (
                        execution_id,
                        proposal_id,
                        tenant_id,
                        executor_id,
                        proposal["action_type"],
                        idempotency_key,
                        requested_by,
                        queued_at,
                    ),
                )
                self._audit(
                    db,
                    "execution.queued",
                    tenant_id,
                    execution_id,
                    {
                        "proposal_id": proposal_id,
                        "executor_id": executor_id,
                        "action_type": proposal["action_type"],
                        "idempotency_key": idempotency_key,
                        "requested_by": requested_by,
                    },
                )
                row = db.execute(
                    "SELECT * FROM action_executions WHERE execution_id = ?",
                    (execution_id,),
                ).fetchone()
                assert row is not None
                return self._execution_receipt(row)

    def list_executions(
        self,
        *,
        tenant_id: str,
        status: str | None = None,
        limit: int = 100,
    ) -> tuple[ActionExecutionReceipt, ...]:
        allowed = {
            "queued",
            "running",
            "succeeded",
            "failed",
            "uncertain",
            "rolled_back",
            "rollback_failed",
        }
        if status is not None and status not in allowed:
            raise ValueError(f"Unknown execution status: {status}")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500.")
        sql = "SELECT * FROM action_executions WHERE tenant_id = ?"
        parameters: list[Any] = [tenant_id]
        if status is not None:
            sql += " AND status = ?"
            parameters.append(status)
        sql += " ORDER BY queued_at DESC, rowid DESC LIMIT ?"
        parameters.append(limit)
        with self._connect() as db:
            rows = db.execute(sql, parameters).fetchall()
        return tuple(self._execution_receipt(row) for row in rows)

    def claim_execution(
        self,
        *,
        execution_id: str,
        tenant_id: str,
    ) -> tuple[ActionExecutionRequest, ActionExecutionReceipt]:
        with self.transaction():
            with self._connect() as db:
                row = db.execute(
                    """
                    SELECT e.*, p.domain, p.entity_type, p.entity_id,
                           p.payload_json
                    FROM action_executions e
                    JOIN proposals p ON p.id = e.proposal_id
                    WHERE e.execution_id = ? AND e.tenant_id = ?
                    """,
                    (execution_id, tenant_id),
                ).fetchone()
                if row is None:
                    raise KeyError(f"Unknown execution: {execution_id}")
                if row["status"] in {"succeeded", "rolled_back"}:
                    return (
                        self._execution_request(row),
                        self._execution_receipt(row, replayed=True),
                    )
                if row["status"] == "uncertain":
                    raise ValueError(
                        "Uncertain execution must be reconciled before any retry."
                    )
                if row["status"] not in {"queued", "failed"}:
                    raise ValueError(f"Execution cannot run from {row['status']}.")
                if row["attempt_count"] >= 3:
                    raise ValueError("Execution retry limit has been reached.")
                started_at = utc_now()
                attempt = row["attempt_count"] + 1
                updated = db.execute(
                    """
                    UPDATE action_executions
                    SET status = 'running', attempt_count = ?, started_at = ?,
                        completed_at = '', result_summary = '',
                        external_reference = '', error_text = ''
                    WHERE execution_id = ? AND status = ?
                    """,
                    (attempt, started_at, execution_id, row["status"]),
                )
                if updated.rowcount != 1:
                    raise ValueError("Execution was claimed by another runner.")
                self._audit(
                    db,
                    "execution.started",
                    tenant_id,
                    execution_id,
                    {
                        "proposal_id": row["proposal_id"],
                        "executor_id": row["executor_id"],
                        "action_type": row["action_type"],
                        "idempotency_key": row["idempotency_key"],
                        "attempt": attempt,
                    },
                )
                claimed = db.execute(
                    """
                    SELECT e.*, p.domain, p.entity_type, p.entity_id,
                           p.payload_json
                    FROM action_executions e
                    JOIN proposals p ON p.id = e.proposal_id
                    WHERE e.execution_id = ?
                    """,
                    (execution_id,),
                ).fetchone()
                assert claimed is not None
                return self._execution_request(claimed), self._execution_receipt(
                    claimed
                )

    @staticmethod
    def _execution_request(row: sqlite3.Row) -> ActionExecutionRequest:
        return ActionExecutionRequest(
            execution_id=row["execution_id"],
            proposal_id=row["proposal_id"],
            tenant_id=row["tenant_id"],
            domain=row["domain"],
            action_type=row["action_type"],
            entity_type=row["entity_type"],
            entity_id=row["entity_id"],
            payload=json.loads(row["payload_json"]),
            idempotency_key=row["idempotency_key"],
            attempt=row["attempt_count"],
        )

    def finish_execution(
        self,
        *,
        execution_id: str,
        status: str,
        summary: str,
        external_reference: str = "",
        error: str = "",
    ) -> ActionExecutionReceipt:
        if status not in {"succeeded", "failed", "uncertain"}:
            raise ValueError("Execution outcome is invalid.")
        with self.transaction():
            with self._connect() as db:
                completed_at = utc_now()
                updated = db.execute(
                    """
                    UPDATE action_executions
                    SET status = ?, completed_at = ?, result_summary = ?,
                        external_reference = ?, error_text = ?
                    WHERE execution_id = ? AND status = 'running'
                    """,
                    (
                        status,
                        completed_at,
                        summary,
                        external_reference,
                        error,
                        execution_id,
                    ),
                )
                if updated.rowcount != 1:
                    raise ValueError("Execution is not running.")
                row = db.execute(
                    "SELECT * FROM action_executions WHERE execution_id = ?",
                    (execution_id,),
                ).fetchone()
                assert row is not None
                self._audit(
                    db,
                    f"execution.{status}",
                    row["tenant_id"],
                    execution_id,
                    {
                        "proposal_id": row["proposal_id"],
                        "executor_id": row["executor_id"],
                        "action_type": row["action_type"],
                        "idempotency_key": row["idempotency_key"],
                        "attempt": row["attempt_count"],
                        "summary": summary,
                        "external_reference": external_reference,
                        "error": error,
                    },
                )
                return self._execution_receipt(row)

    def claim_rollback(
        self,
        *,
        execution_id: str,
        tenant_id: str,
    ) -> tuple[ActionExecutionRequest, ActionExecutionReceipt]:
        with self.transaction():
            with self._connect() as db:
                row = db.execute(
                    """
                    SELECT e.*, p.domain, p.entity_type, p.entity_id,
                           p.payload_json
                    FROM action_executions e
                    JOIN proposals p ON p.id = e.proposal_id
                    WHERE e.execution_id = ? AND e.tenant_id = ?
                    """,
                    (execution_id, tenant_id),
                ).fetchone()
                if row is None:
                    raise KeyError(f"Unknown execution: {execution_id}")
                if row["status"] == "rolled_back":
                    return (
                        self._execution_request(row),
                        self._execution_receipt(row, replayed=True),
                    )
                if row["status"] not in {"succeeded", "rollback_failed"}:
                    raise ValueError(
                        "Only a definitely succeeded execution can be rolled back."
                    )
                if row["rollback_status"] == "running":
                    raise ValueError("Rollback is already running.")
                db.execute(
                    """
                    UPDATE action_executions SET rollback_status = 'running'
                    WHERE execution_id = ?
                    """,
                    (execution_id,),
                )
                self._audit(
                    db,
                    "execution.rollback_started",
                    tenant_id,
                    execution_id,
                    {
                        "proposal_id": row["proposal_id"],
                        "executor_id": row["executor_id"],
                        "idempotency_key": row["idempotency_key"],
                    },
                )
                claimed = db.execute(
                    """
                    SELECT e.*, p.domain, p.entity_type, p.entity_id,
                           p.payload_json
                    FROM action_executions e
                    JOIN proposals p ON p.id = e.proposal_id
                    WHERE e.execution_id = ?
                    """,
                    (execution_id,),
                ).fetchone()
                assert claimed is not None
                return self._execution_request(claimed), self._execution_receipt(
                    claimed
                )

    def finish_rollback(
        self,
        *,
        execution_id: str,
        succeeded: bool,
        summary: str,
        external_reference: str = "",
        error: str = "",
    ) -> ActionExecutionReceipt:
        status = "rolled_back" if succeeded else "rollback_failed"
        rollback_status = "succeeded" if succeeded else "failed"
        with self.transaction():
            with self._connect() as db:
                rollback_at = utc_now()
                updated = db.execute(
                    """
                    UPDATE action_executions
                    SET status = ?, rollback_status = ?, rollback_at = ?,
                        result_summary = ?, external_reference = ?,
                        error_text = ?
                    WHERE execution_id = ? AND rollback_status = 'running'
                    """,
                    (
                        status,
                        rollback_status,
                        rollback_at,
                        summary,
                        external_reference,
                        error,
                        execution_id,
                    ),
                )
                if updated.rowcount != 1:
                    raise ValueError("Execution rollback is not running.")
                row = db.execute(
                    "SELECT * FROM action_executions WHERE execution_id = ?",
                    (execution_id,),
                ).fetchone()
                assert row is not None
                self._audit(
                    db,
                    f"execution.{status}",
                    row["tenant_id"],
                    execution_id,
                    {
                        "proposal_id": row["proposal_id"],
                        "executor_id": row["executor_id"],
                        "idempotency_key": row["idempotency_key"],
                        "summary": summary,
                        "external_reference": external_reference,
                        "error": error,
                    },
                )
                return self._execution_receipt(row)

    def reconcile_execution(
        self,
        *,
        execution_id: str,
        tenant_id: str,
        outcome: str,
        note: str,
        reconciled_by: str,
        external_reference: str = "",
    ) -> ActionExecutionReceipt:
        note = " ".join(note.split())
        reconciled_by = " ".join(reconciled_by.split())
        external_reference = " ".join(external_reference.split())
        if not note or len(note) > 500:
            raise ValueError("Reconciliation note must be 1-500 characters.")
        if not reconciled_by or len(reconciled_by) > 100:
            raise ValueError("Reconciler must be 1-100 characters.")
        if len(external_reference) > 200:
            raise ValueError("External reference must be at most 200 characters.")
        with self.transaction():
            with self._connect() as db:
                completed_at = utc_now()
                updated = db.execute(
                    """
                    UPDATE action_executions
                    SET status = ?, completed_at = ?, result_summary = ?,
                        external_reference = ?, error_text = ?
                    WHERE execution_id = ? AND tenant_id = ?
                      AND status = 'uncertain'
                    """,
                    (
                        outcome,
                        completed_at,
                        note,
                        external_reference,
                        "" if outcome == "succeeded" else note,
                        execution_id,
                        tenant_id,
                    ),
                )
                if updated.rowcount != 1:
                    raise ValueError(
                        "Only an uncertain execution can be reconciled."
                    )
                row = db.execute(
                    "SELECT * FROM action_executions WHERE execution_id = ?",
                    (execution_id,),
                ).fetchone()
                assert row is not None
                self._audit(
                    db,
                    "execution.reconciled",
                    tenant_id,
                    execution_id,
                    {
                        "outcome": outcome,
                        "note": note,
                        "reconciled_by": reconciled_by,
                        "external_reference": external_reference,
                    },
                )
                return self._execution_receipt(row)

    def overview(self, *, tenant_id: str) -> dict[str, int]:
        with self._connect() as db:
            counts = {
                "observations": db.execute(
                    "SELECT COUNT(*) FROM observations WHERE tenant_id = ?",
                    (tenant_id,),
                ).fetchone()[0],
                "changes": db.execute(
                    "SELECT COUNT(*) FROM changes WHERE tenant_id = ?",
                    (tenant_id,),
                ).fetchone()[0],
                "proposals": db.execute(
                    "SELECT COUNT(*) FROM proposals WHERE tenant_id = ?",
                    (tenant_id,),
                ).fetchone()[0],
                "pending": db.execute(
                    "SELECT COUNT(*) FROM proposals WHERE tenant_id = ? AND status = ?",
                    (tenant_id, ApprovalStatus.PENDING.value),
                ).fetchone()[0],
                "approved": db.execute(
                    "SELECT COUNT(*) FROM proposals WHERE tenant_id = ? AND status = ?",
                    (tenant_id, ApprovalStatus.APPROVED.value),
                ).fetchone()[0],
                "rejected": db.execute(
                    "SELECT COUNT(*) FROM proposals WHERE tenant_id = ? AND status = ?",
                    (tenant_id, ApprovalStatus.REJECTED.value),
                ).fetchone()[0],
                "cartography_cards": db.execute(
                    "SELECT COUNT(*) FROM cartography_cards WHERE tenant_id = ?",
                    (tenant_id,),
                ).fetchone()[0],
                "audit_events": db.execute(
                    "SELECT COUNT(*) FROM audit_events WHERE tenant_id = ?",
                    (tenant_id,),
                ).fetchone()[0],
                "import_batches": db.execute(
                    "SELECT COUNT(*) FROM import_batches WHERE tenant_id = ?",
                    (tenant_id,),
                ).fetchone()[0],
                "executions": db.execute(
                    "SELECT COUNT(*) FROM action_executions WHERE tenant_id = ?",
                    (tenant_id,),
                ).fetchone()[0],
                "execution_attention": db.execute(
                    """
                    SELECT COUNT(*) FROM action_executions
                    WHERE tenant_id = ? AND status IN (
                        'failed', 'uncertain', 'rollback_failed'
                    )
                    """,
                    (tenant_id,),
                ).fetchone()[0],
            }
        return counts

    def find_import_batch(
        self,
        *,
        tenant_id: str,
        connector_id: str,
        external_batch_id: str,
    ) -> ImportBatchReceipt | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT * FROM import_batches
                WHERE tenant_id = ? AND connector_id = ? AND external_batch_id = ?
                """,
                (tenant_id, connector_id, external_batch_id),
            ).fetchone()
            if row is None:
                return None
            records = db.execute(
                """
                SELECT * FROM import_record_receipts
                WHERE batch_id = ? ORDER BY rowid
                """,
                (row["batch_id"],),
            ).fetchall()
        return self._import_receipt(row, records)

    def start_import_batch(self, receipt: ImportBatchReceipt) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO import_batches
                (batch_id, external_batch_id, tenant_id, connector_id, domain,
                 source_system, status, record_count, imported_count,
                 duplicate_count, started_at, completed_at, payload_hash,
                 error_text)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.batch_id,
                    receipt.external_batch_id,
                    receipt.tenant_id,
                    receipt.connector_id,
                    receipt.domain,
                    receipt.source_system,
                    receipt.status,
                    receipt.record_count,
                    receipt.imported_count,
                    receipt.duplicate_count,
                    receipt.started_at,
                    receipt.completed_at,
                    receipt.payload_hash,
                    receipt.error,
                ),
            )
            self._audit(
                db,
                "import.started",
                receipt.tenant_id,
                receipt.batch_id,
                {
                    "connector_id": receipt.connector_id,
                    "external_batch_id": receipt.external_batch_id,
                    "record_count": receipt.record_count,
                },
            )

    def connector_record_observation(
        self,
        *,
        tenant_id: str,
        connector_id: str,
        record_id: str,
        version: str,
        fingerprint: str,
    ) -> str | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT observation_id FROM connector_record_ledger
                WHERE tenant_id = ? AND connector_id = ? AND record_id = ?
                  AND version = ? AND fingerprint = ?
                """,
                (tenant_id, connector_id, record_id, version, fingerprint),
            ).fetchone()
        return row["observation_id"] if row else None

    def record_import_item(
        self,
        *,
        batch_id: str,
        tenant_id: str,
        connector_id: str,
        receipt: ImportRecordReceipt,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO import_record_receipts
                (batch_id, record_id, version, status, observation_id, fingerprint)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    receipt.record_id,
                    receipt.version,
                    receipt.status,
                    receipt.observation_id,
                    receipt.fingerprint,
                ),
            )
            if receipt.status == "imported" and receipt.observation_id:
                db.execute(
                    """
                    INSERT INTO connector_record_ledger
                    (tenant_id, connector_id, record_id, version, fingerprint,
                     observation_id, first_batch_id, imported_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tenant_id,
                        connector_id,
                        receipt.record_id,
                        receipt.version,
                        receipt.fingerprint,
                        receipt.observation_id,
                        batch_id,
                        utc_now(),
                    ),
                )

    def complete_import_batch(self, receipt: ImportBatchReceipt) -> None:
        with self._connect() as db:
            db.execute(
                """
                UPDATE import_batches
                SET status = ?, imported_count = ?, duplicate_count = ?,
                    completed_at = ?, error_text = ?
                WHERE batch_id = ?
                """,
                (
                    receipt.status,
                    receipt.imported_count,
                    receipt.duplicate_count,
                    receipt.completed_at,
                    receipt.error,
                    receipt.batch_id,
                ),
            )
            self._audit(
                db,
                "import.completed",
                receipt.tenant_id,
                receipt.batch_id,
                {
                    "status": receipt.status,
                    "imported_count": receipt.imported_count,
                    "duplicate_count": receipt.duplicate_count,
                },
            )

    def record_import_replay(self, receipt: ImportBatchReceipt) -> None:
        with self._connect() as db:
            self._audit(
                db,
                "import.replayed",
                receipt.tenant_id,
                receipt.batch_id,
                {
                    "connector_id": receipt.connector_id,
                    "external_batch_id": receipt.external_batch_id,
                },
            )

    def list_import_batches(
        self,
        *,
        tenant_id: str,
        limit: int = 100,
    ) -> tuple[ImportBatchReceipt, ...]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500.")
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT * FROM import_batches WHERE tenant_id = ?
                ORDER BY started_at DESC, rowid DESC LIMIT ?
                """,
                (tenant_id, limit),
            ).fetchall()
            receipts = []
            for row in rows:
                records = db.execute(
                    "SELECT * FROM import_record_receipts WHERE batch_id = ? ORDER BY rowid",
                    (row["batch_id"],),
                ).fetchall()
                receipts.append(self._import_receipt(row, records))
        return tuple(receipts)

    @staticmethod
    def _import_receipt(
        row: sqlite3.Row,
        records: list[sqlite3.Row],
    ) -> ImportBatchReceipt:
        return ImportBatchReceipt(
            batch_id=row["batch_id"],
            external_batch_id=row["external_batch_id"],
            tenant_id=row["tenant_id"],
            connector_id=row["connector_id"],
            domain=row["domain"],
            source_system=row["source_system"],
            status=row["status"],
            record_count=row["record_count"],
            imported_count=row["imported_count"],
            duplicate_count=row["duplicate_count"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            payload_hash=row["payload_hash"],
            records=tuple(
                ImportRecordReceipt(
                    record_id=record["record_id"],
                    version=record["version"],
                    status=record["status"],
                    observation_id=record["observation_id"],
                    fingerprint=record["fingerprint"],
                )
                for record in records
            ),
            error=row["error_text"],
        )

    def audit_events(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM audit_events ORDER BY sequence").fetchall()
        return [
            {
                "sequence": row["sequence"],
                "event_type": row["event_type"],
                "tenant_id": row["tenant_id"],
                "subject_id": row["subject_id"],
                "detail": json.loads(row["detail_json"]),
                "occurred_at": row["occurred_at"],
            }
            for row in rows
        ]

    def intelligence_context(
        self,
        *,
        tenant_id: str,
        candidate_limit: int = 500,
    ) -> dict[str, tuple[dict[str, Any], ...]]:
        if not tenant_id.strip() or len(tenant_id) > 100:
            raise ValueError(
                "tenant_id is required and must be at most 100 characters."
            )
        if not 1 <= candidate_limit <= 1000:
            raise ValueError("candidate_limit must be between 1 and 1000.")
        with self._connect() as db:
            change_rows = db.execute(
                """
                SELECT c.id AS change_id, c.change_json, c.created_at,
                       o.id AS observation_id, o.domain, o.entity_type,
                       o.entity_id, o.source_system, o.source_record_id,
                       o.source_version, o.observed_at, o.attributes_json
                FROM changes c
                JOIN observations o ON o.id = c.current_observation_id
                WHERE c.tenant_id = ?
                ORDER BY c.created_at DESC, c.rowid DESC
                LIMIT ?
                """,
                (tenant_id, candidate_limit),
            ).fetchall()
            proposal_rows = db.execute(
                """
                SELECT * FROM proposals
                WHERE tenant_id = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                (tenant_id, candidate_limit),
            ).fetchall()
            card_rows = db.execute(
                """
                SELECT card_json FROM cartography_cards
                WHERE tenant_id = ?
                ORDER BY created_at_utc DESC, rowid DESC
                LIMIT ?
                """,
                (tenant_id, candidate_limit),
            ).fetchall()
        changes = tuple(
            {
                "change_id": row["change_id"],
                "change": json.loads(row["change_json"]),
                "created_at": row["created_at"],
                "observation_id": row["observation_id"],
                "domain": row["domain"],
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "source_system": row["source_system"],
                "source_record_id": row["source_record_id"],
                "source_version": row["source_version"],
                "observed_at": row["observed_at"],
                "attributes": json.loads(row["attributes_json"]),
            }
            for row in change_rows
        )
        proposals = tuple(
            {
                "id": row["id"],
                "domain": row["domain"],
                "action_type": row["action_type"],
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "reason": row["reason"],
                "payload": json.loads(row["payload_json"]),
                "risk": row["risk"],
                "status": row["status"],
                "policy_reason": row["policy_reason"],
                "created_at": row["created_at"],
            }
            for row in proposal_rows
        )
        cards = tuple(json.loads(row["card_json"]) for row in card_rows)
        return {"changes": changes, "proposals": proposals, "cards": cards}

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _database_integrity(path: Path) -> tuple[str, int]:
        if not path.is_file():
            raise ValueError("Backup database file is missing.")
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()
            integrity = str(result[0]) if result else "no result"
            if integrity != "ok":
                raise ValueError(f"SQLite integrity check failed: {integrity}")
            row = connection.execute("SELECT version FROM schema_meta").fetchone()
            if row is None:
                raise ValueError("Backup has no schema version.")
            schema_version = int(row[0])
            if schema_version != SCHEMA_VERSION:
                raise ValueError(
                    f"Backup schema version {schema_version} is not supported."
                )
        except sqlite3.DatabaseError as error:
            raise ValueError("Backup is not a valid Atlas Core database.") from error
        finally:
            connection.close()
        return integrity, schema_version

    def create_backup(self) -> BackupReceipt:
        if self._transaction_connection.get() is not None:
            raise RuntimeError("Cannot create a backup inside an active transaction.")
        with self._lifecycle_lock:
            self.backup_directory.mkdir(parents=True, exist_ok=True)
            created_at = utc_now()
            temp_id = self._stable_id("tmp", {"created_at": created_at})
            temp_database = self.backup_directory / f".{temp_id}.partial"
            if temp_database.exists():
                temp_database.unlink()
            try:
                source = sqlite3.connect(self.database)
                target = sqlite3.connect(temp_database)
                try:
                    source.backup(target)
                finally:
                    target.close()
                    source.close()
            except Exception:
                temp_database.unlink(missing_ok=True)
                raise
            integrity, schema_version = self._database_integrity(temp_database)
            sha256 = self._file_sha256(temp_database)
            backup_id = self._stable_id(
                "bkp",
                {"created_at": created_at, "sha256": sha256},
            )
            database_name = f"{backup_id}.db"
            database_path = self.backup_directory / database_name
            temp_database.replace(database_path)
            receipt = BackupReceipt(
                backup_id=backup_id,
                created_at=created_at,
                filename=database_name,
                size_bytes=database_path.stat().st_size,
                sha256=sha256,
                schema_version=schema_version,
                integrity_check=integrity,
            )
            manifest_path = self.backup_directory / f"{backup_id}.json"
            temp_manifest = self.backup_directory / f".{backup_id}.json.partial"
            temp_manifest.write_text(
                json.dumps(asdict(receipt), indent=2, sort_keys=True),
                encoding="utf-8",
            )
            temp_manifest.replace(manifest_path)
            with self._connect() as db:
                self._audit(
                    db,
                    "system.backup_created",
                    "system",
                    backup_id,
                    {
                        "sha256": sha256,
                        "size_bytes": receipt.size_bytes,
                        "schema_version": schema_version,
                    },
                )
            return receipt

    def _verified_backup(self, backup_id: str) -> BackupReceipt:
        if not BACKUP_ID_PATTERN.fullmatch(backup_id):
            raise ValueError("Invalid backup identifier.")
        manifest_path = self.backup_directory / f"{backup_id}.json"
        if not manifest_path.is_file():
            raise KeyError(f"Unknown backup: {backup_id}")
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            receipt = BackupReceipt(**payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("Backup manifest is invalid.") from error
        expected_filename = f"{backup_id}.db"
        if receipt.backup_id != backup_id or receipt.filename != expected_filename:
            raise ValueError("Backup manifest identity does not match its filename.")
        if (
            receipt.status != "verified"
            or receipt.error
            or receipt.integrity_check != "ok"
        ):
            raise ValueError("Backup manifest does not describe a verified backup.")
        database_path = self.backup_directory / expected_filename
        if not database_path.is_file():
            raise ValueError("Backup database file is missing.")
        if database_path.stat().st_size != receipt.size_bytes:
            raise ValueError("Backup size does not match its manifest.")
        if self._file_sha256(database_path) != receipt.sha256:
            raise ValueError("Backup checksum does not match its manifest.")
        integrity, schema_version = self._database_integrity(database_path)
        if integrity != receipt.integrity_check or schema_version != receipt.schema_version:
            raise ValueError("Backup integrity metadata does not match its manifest.")
        return receipt

    def list_backups(self) -> tuple[BackupReceipt, ...]:
        if not self.backup_directory.is_dir():
            return ()
        receipts: list[BackupReceipt] = []
        for manifest in self.backup_directory.glob("bkp_*.json"):
            backup_id = manifest.stem
            try:
                receipts.append(self._verified_backup(backup_id))
            except (KeyError, OSError, ValueError) as error:
                receipts.append(
                    BackupReceipt(
                        backup_id=backup_id,
                        created_at="",
                        filename=f"{backup_id}.db",
                        size_bytes=0,
                        sha256="",
                        schema_version=0,
                        integrity_check="failed",
                        status="invalid",
                        error=str(error),
                    )
                )
        return tuple(
            sorted(receipts, key=lambda receipt: receipt.created_at, reverse=True)
        )

    def export_encrypted_backup(
        self,
        backup_id: str,
        *,
        passphrase: str,
    ) -> EncryptedBackupArtifact:
        with self._lifecycle_lock:
            selected = self._verified_backup(backup_id)
            database_path = self.backup_directory / selected.filename
            artifact_data = encrypt_recovery_artifact(
                database_path.read_bytes(),
                passphrase=passphrase,
                metadata={
                    "backup_id": selected.backup_id,
                    "backup_created_at": selected.created_at,
                    "plaintext_sha256": selected.sha256,
                    "schema_version": selected.schema_version,
                    "integrity_check": selected.integrity_check,
                },
            )
            artifact_sha256 = hashlib.sha256(artifact_data).hexdigest()
            export_id = self._stable_id(
                "exp",
                {
                    "backup_id": backup_id,
                    "artifact_sha256": artifact_sha256,
                },
            )
            created_at = utc_now()
            receipt = EncryptedBackupArtifact(
                export_id=export_id,
                backup_id=backup_id,
                created_at=created_at,
                filename=f"{backup_id}.atlasbackup",
                size_bytes=len(artifact_data),
                sha256=artifact_sha256,
                cipher="AES-256-GCM",
                kdf="scrypt",
                data=artifact_data,
            )
            with self._connect() as db:
                self._audit(
                    db,
                    "system.backup_exported",
                    "system",
                    backup_id,
                    {
                        "export_id": export_id,
                        "sha256": artifact_sha256,
                        "size_bytes": len(artifact_data),
                        "cipher": receipt.cipher,
                        "kdf": receipt.kdf,
                    },
                )
            return receipt

    def import_encrypted_backup(
        self,
        artifact: bytes,
        *,
        passphrase: str,
    ) -> BackupReceipt:
        if self._transaction_connection.get() is not None:
            raise RuntimeError("Cannot import a backup inside an active transaction.")
        with self._lifecycle_lock:
            header, plaintext = decrypt_recovery_artifact(
                artifact,
                passphrase=passphrase,
            )
            try:
                backup_id = str(header["backup_id"])
                created_at = str(header["backup_created_at"])
                expected_sha256 = str(header["plaintext_sha256"])
                expected_schema = int(header["schema_version"])
                expected_integrity = str(header["integrity_check"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(
                    "Encrypted recovery artifact backup metadata is invalid."
                ) from error
            if not BACKUP_ID_PATTERN.fullmatch(backup_id):
                raise ValueError("Encrypted recovery artifact backup ID is invalid.")
            actual_sha256 = hashlib.sha256(plaintext).hexdigest()
            if actual_sha256 != expected_sha256:
                raise ValueError(
                    "Encrypted recovery artifact plaintext checksum is invalid."
                )
            self.backup_directory.mkdir(parents=True, exist_ok=True)
            database_name = f"{backup_id}.db"
            database_path = self.backup_directory / database_name
            manifest_path = self.backup_directory / f"{backup_id}.json"
            if database_path.exists() or manifest_path.exists():
                existing = self._verified_backup(backup_id)
                if existing.sha256 != expected_sha256:
                    raise ValueError(
                        "Imported backup conflicts with an existing backup ID."
                    )
                return existing
            temp_id = self._stable_id(
                "tmp",
                {"backup_id": backup_id, "artifact_sha256": actual_sha256},
            )
            temp_database = self.backup_directory / f".{temp_id}.import.partial"
            temp_manifest = self.backup_directory / f".{temp_id}.json.partial"
            try:
                temp_database.write_bytes(plaintext)
                integrity, schema_version = self._database_integrity(temp_database)
                if (
                    integrity != expected_integrity
                    or schema_version != expected_schema
                ):
                    raise ValueError(
                        "Encrypted recovery artifact integrity metadata does not match."
                    )
                receipt = BackupReceipt(
                    backup_id=backup_id,
                    created_at=created_at,
                    filename=database_name,
                    size_bytes=len(plaintext),
                    sha256=actual_sha256,
                    schema_version=schema_version,
                    integrity_check=integrity,
                )
                temp_manifest.write_text(
                    json.dumps(asdict(receipt), indent=2, sort_keys=True),
                    encoding="utf-8",
                )
                temp_database.replace(database_path)
                temp_manifest.replace(manifest_path)
            except Exception:
                temp_database.unlink(missing_ok=True)
                temp_manifest.unlink(missing_ok=True)
                raise
            with self._connect() as db:
                self._audit(
                    db,
                    "system.backup_imported",
                    "system",
                    backup_id,
                    {
                        "sha256": actual_sha256,
                        "size_bytes": len(plaintext),
                        "cipher": header["cipher"],
                        "kdf": header["kdf"]["name"],
                    },
                )
            return receipt

    def prune_backups(
        self,
        *,
        keep_last: int,
        confirmation: str = "",
        dry_run: bool = True,
    ) -> BackupRetentionReceipt:
        if not 1 <= keep_last <= 1000:
            raise ValueError("keep_last must be between 1 and 1000.")
        required = f"PRUNE BACKUPS KEEP {keep_last}"
        if not dry_run and confirmation != required:
            raise ValueError(f"Prune confirmation must be exactly: {required}")
        with self._lifecycle_lock:
            verified = tuple(
                receipt
                for receipt in self.list_backups()
                if receipt.status == "verified"
            )
            candidates = verified[keep_last:]
            deleted: list[str] = []
            reclaimed_bytes = sum(receipt.size_bytes for receipt in candidates)
            if not dry_run:
                for receipt in candidates:
                    database_path = self.backup_directory / receipt.filename
                    manifest_path = (
                        self.backup_directory / f"{receipt.backup_id}.json"
                    )
                    database_path.unlink()
                    manifest_path.unlink()
                    deleted.append(receipt.backup_id)
                with self._connect() as db:
                    self._audit(
                        db,
                        "system.backups_pruned",
                        "system",
                        "backup-retention",
                        {
                            "keep_last": keep_last,
                            "deleted_backup_ids": deleted,
                            "reclaimed_bytes": reclaimed_bytes,
                        },
                    )
            return BackupRetentionReceipt(
                keep_last=keep_last,
                candidate_backup_ids=tuple(
                    receipt.backup_id for receipt in candidates
                ),
                deleted_backup_ids=tuple(deleted),
                reclaimed_bytes=reclaimed_bytes,
                dry_run=dry_run,
                applied_at=utc_now(),
            )

    def restore_backup(self, backup_id: str) -> RestoreReceipt:
        if self._transaction_connection.get() is not None:
            raise RuntimeError("Cannot restore a backup inside an active transaction.")
        with self._lifecycle_lock:
            selected = self._verified_backup(backup_id)
            safety = self.create_backup()
            source_path = self.backup_directory / selected.filename
            restore_temp = self.database.with_suffix(
                self.database.suffix + f".{backup_id}.restore"
            )
            if restore_temp.exists():
                restore_temp.unlink()
            shutil.copyfile(source_path, restore_temp)
            integrity, _ = self._database_integrity(restore_temp)
            restored_sha256 = self._file_sha256(restore_temp)
            if restored_sha256 != selected.sha256:
                restore_temp.unlink(missing_ok=True)
                raise ValueError("Restored database checksum changed during staging.")
            restore_temp.replace(self.database)
            restored_at = utc_now()
            with self._connect() as db:
                self._audit(
                    db,
                    "system.backup_restored",
                    "system",
                    backup_id,
                    {
                        "safety_backup_id": safety.backup_id,
                        "sha256": restored_sha256,
                    },
                )
            return RestoreReceipt(
                restored_backup_id=backup_id,
                safety_backup_id=safety.backup_id,
                restored_at=restored_at,
                sha256=restored_sha256,
                integrity_check=integrity,
            )

    def record_cartography_card(self, card: CartographyCard) -> CartographyCard:
        card_id = self._stable_id(
            "map",
            {
                "tenant_id": card.tenant_id,
                "lane": card.lane,
                "title": card.title,
                "summary": card.summary,
                "source_pointers": card.source_pointers,
                "created_at_utc": card.created_at_utc,
            },
        )
        stored = replace(card, card_id=card_id)
        payload = asdict(stored)
        with self._connect() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO cartography_cards
                (card_id, tenant_id, project, lane, title, summary, card_json, created_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stored.card_id,
                    stored.tenant_id,
                    stored.project,
                    stored.lane,
                    stored.title,
                    stored.summary,
                    json.dumps(payload, sort_keys=True),
                    stored.created_at_utc,
                ),
            )
            self._audit(
                db,
                "cartography.recorded",
                stored.tenant_id,
                stored.card_id or "",
                {"lane": stored.lane, "title": stored.title},
            )
        return stored

    def search_cartography_cards(
        self,
        *,
        tenant_id: str,
        query: str = "",
        lane: str | None = None,
        limit: int = 20,
    ) -> tuple[CartographyCard, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100.")
        sql = "SELECT card_json FROM cartography_cards WHERE tenant_id = ?"
        params: list[Any] = [tenant_id]
        if lane:
            sql += " AND lane = ?"
            params.append(lane)
        if query:
            sql += " AND (title LIKE ? OR summary LIKE ? OR card_json LIKE ?)"
            pattern = f"%{query}%"
            params.extend((pattern, pattern, pattern))
        sql += " ORDER BY created_at_utc DESC, rowid DESC LIMIT ?"
        params.append(limit)
        with self._connect() as db:
            rows = db.execute(sql, params).fetchall()
        cards = []
        for row in rows:
            payload = json.loads(row["card_json"])
            for field_name in (
                "source_pointers",
                "tags",
                "must_retrieve",
                "decisions",
                "verification",
                "unresolved",
            ):
                payload[field_name] = tuple(payload[field_name])
            cards.append(CartographyCard(**payload))
        return tuple(cards)

    @staticmethod
    def _audit(
        db: sqlite3.Connection,
        event_type: str,
        tenant_id: str,
        subject_id: str,
        detail: dict[str, Any],
    ) -> None:
        db.execute(
            """
            INSERT INTO audit_events(event_type, tenant_id, subject_id, detail_json, occurred_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (event_type, tenant_id, subject_id, json.dumps(detail, sort_keys=True, default=str), utc_now()),
        )
