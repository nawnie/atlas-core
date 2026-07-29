from __future__ import annotations

from pathlib import Path

from atlas_core import (
    ActionExecutionCoordinator,
    ActionExecutor,
    ApprovalDecision,
    AtlasCore,
    AutomationStore,
    IndustryAdapter,
    SnapshotConnector,
)
from atlas_core.models import ApprovalStatus, utc_now

from .contracts import (
    ApprovalReceipt,
    AutomationEvent,
    AutomationRunReceipt,
    AutomationStatus,
    AnswerCitation,
    AnswerClaim,
    AnswerSection,
    AuditEvent,
    BackupReceipt,
    BackupRetentionReceipt,
    CartographyCard,
    Change,
    IngestRequest,
    EncryptedBackupArtifact,
    ExecutionReceipt,
    IngestResponse,
    ImportReceipt,
    ImportRecord,
    IntelligenceAnswer,
    Proposal,
    ProviderCertificationCheck,
    ProviderCertificationReceipt,
    RestoreReceipt,
    TenantOverview,
)
from .errors import (
    AtlasTransportError,
    ExecutionConflictError,
    InvalidRequestError,
    ProposalConflictError,
)


class LocalAtlasTransport:
    """In-process transport for local Atlas Core deployments."""

    def __init__(
        self,
        *,
        database: str | Path,
        adapters: tuple[IndustryAdapter, ...],
        executors: tuple[ActionExecutor, ...] = (),
        automation_config: str | Path | None = None,
    ) -> None:
        self._engine = AtlasCore(database, adapters)
        self._executions = ActionExecutionCoordinator(
            self._engine.memory,
            executors,
        )
        self._automation = AutomationStore(database)
        self._automation_config = automation_config

    @staticmethod
    def _automation_event(event: object) -> AutomationEvent:
        return AutomationEvent(
            event_id=event.event_id,
            run_id=event.run_id,
            event_type=event.event_type,
            status=event.status,
            occurred_at=event.occurred_at,
            job_id=event.job_id,
            source_name=event.source_name,
            sha256=event.sha256,
            detail=event.detail,
            error=event.error,
        )

    @classmethod
    def _automation_run(cls, receipt: object) -> AutomationRunReceipt:
        return AutomationRunReceipt(
            run_id=receipt.run_id,
            started_at=receipt.started_at,
            completed_at=receipt.completed_at,
            status=receipt.status,
            imported_files=receipt.imported_files,
            rejected_files=receipt.rejected_files,
            replayed_files=receipt.replayed_files,
            skipped_files=receipt.skipped_files,
            backup_created=receipt.backup_created,
            portable_exported=receipt.portable_exported,
            events=tuple(cls._automation_event(event) for event in receipt.events),
        )

    def automation_status(self) -> AutomationStatus:
        try:
            status = self._automation.status(
                config_path=self._automation_config or "",
            )
        except Exception as error:
            raise AtlasTransportError(
                "Local Atlas automation status failed."
            ) from error
        return AutomationStatus(
            configured=status.configured,
            config_path=status.config_path,
            state_directory=status.state_directory,
            locked=status.locked,
            last_run=(
                self._automation_run(status.last_run) if status.last_run else None
            ),
            recent_events=tuple(
                self._automation_event(event) for event in status.recent_events
            ),
        )

    @staticmethod
    def _provider(receipt: object) -> ProviderCertificationReceipt:
        return ProviderCertificationReceipt(
            certification_id=receipt.certification_id,
            provider_id=receipt.provider_id,
            display_name=receipt.display_name,
            domain=receipt.domain,
            environment=receipt.environment,
            status=receipt.status,
            manifest_sha256=receipt.manifest_sha256,
            action_types=tuple(receipt.action_types),
            supports_rollback=receipt.supports_rollback,
            network_access=receipt.network_access,
            credential_strategy=receipt.credential_strategy,
            synthetic_only=receipt.synthetic_only,
            live_action_authorized=receipt.live_action_authorized,
            checks=tuple(
                ProviderCertificationCheck(
                    name=check.name,
                    status=check.status,
                    detail=check.detail,
                )
                for check in receipt.checks
            ),
            generated_at=receipt.generated_at,
        )

    def provider_readiness(
        self,
        *,
        certify_local_sandboxes: bool = False,
    ) -> tuple[ProviderCertificationReceipt, ...]:
        try:
            return tuple(
                self._provider(receipt)
                for receipt in self._executions.provider_readiness(
                    certify_local_sandboxes=certify_local_sandboxes,
                )
            )
        except Exception as error:
            raise AtlasTransportError(
                "Local provider readiness check failed."
            ) from error

    @staticmethod
    def _execution(receipt: object) -> ExecutionReceipt:
        return ExecutionReceipt(
            execution_id=receipt.execution_id,
            proposal_id=receipt.proposal_id,
            tenant_id=receipt.tenant_id,
            executor_id=receipt.executor_id,
            action_type=receipt.action_type,
            idempotency_key=receipt.idempotency_key,
            status=receipt.status,
            attempt_count=receipt.attempt_count,
            requested_by=receipt.requested_by,
            queued_at=receipt.queued_at,
            started_at=receipt.started_at,
            completed_at=receipt.completed_at,
            result_summary=receipt.result_summary,
            external_reference=receipt.external_reference,
            error=receipt.error,
            rollback_status=receipt.rollback_status,
            rollback_at=receipt.rollback_at,
            replayed=receipt.replayed,
        )

    def executions(
        self,
        *,
        tenant_id: str,
        status: str | None = None,
        limit: int = 100,
    ) -> tuple[ExecutionReceipt, ...]:
        try:
            return tuple(
                self._execution(receipt)
                for receipt in self._executions.list(
                    tenant_id=tenant_id,
                    status=status,
                    limit=limit,
                )
            )
        except ValueError as error:
            raise InvalidRequestError(str(error)) from error
        except Exception as error:
            raise AtlasTransportError("Local execution query failed.") from error

    def queue_execution(
        self,
        proposal_id: str,
        *,
        tenant_id: str,
        executor_id: str,
        requested_by: str,
        confirmation: str,
    ) -> ExecutionReceipt:
        try:
            return self._execution(
                self._executions.queue(
                    proposal_id,
                    tenant_id=tenant_id,
                    executor_id=executor_id,
                    requested_by=requested_by,
                    confirmation=confirmation,
                )
            )
        except (KeyError, ValueError) as error:
            raise ExecutionConflictError(str(error)) from error
        except Exception as error:
            raise AtlasTransportError("Local execution queue failed.") from error

    def run_execution(
        self,
        execution_id: str,
        *,
        tenant_id: str,
        confirmation: str,
    ) -> ExecutionReceipt:
        try:
            return self._execution(
                self._executions.run(
                    execution_id,
                    tenant_id=tenant_id,
                    confirmation=confirmation,
                )
            )
        except (KeyError, ValueError) as error:
            raise ExecutionConflictError(str(error)) from error
        except Exception as error:
            raise AtlasTransportError("Local execution failed.") from error

    def rollback_execution(
        self,
        execution_id: str,
        *,
        tenant_id: str,
        confirmation: str,
    ) -> ExecutionReceipt:
        try:
            return self._execution(
                self._executions.rollback(
                    execution_id,
                    tenant_id=tenant_id,
                    confirmation=confirmation,
                )
            )
        except (KeyError, ValueError) as error:
            raise ExecutionConflictError(str(error)) from error
        except Exception as error:
            raise AtlasTransportError("Local execution rollback failed.") from error

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
        try:
            return self._execution(
                self._executions.reconcile(
                    execution_id,
                    tenant_id=tenant_id,
                    outcome=outcome,
                    note=note,
                    reconciled_by=reconciled_by,
                    external_reference=external_reference,
                    confirmation=confirmation,
                )
            )
        except (KeyError, ValueError) as error:
            raise ExecutionConflictError(str(error)) from error
        except Exception as error:
            raise AtlasTransportError(
                "Local execution reconciliation failed."
            ) from error

    def ingest(self, request: IngestRequest) -> IngestResponse:
        try:
            result = self._engine.ingest(
                request.domain,
                request.record,
                tenant_id=request.tenant_id,
                source_system=request.source_system,
            )
        except ValueError as error:
            raise InvalidRequestError(str(error)) from error
        except Exception as error:
            raise AtlasTransportError("Local Atlas ingestion failed.") from error
        return IngestResponse(
            observation_id=result.observation.id or "",
            tenant_id=result.observation.tenant_id,
            domain=result.observation.domain,
            entity_type=result.observation.entity_type,
            entity_id=result.observation.entity_id,
            summary=result.changes.summary,
            changes=tuple(
                Change(
                    field=change.field,
                    before=change.before,
                    after=change.after,
                    kind=change.kind,
                )
                for change in result.changes.changes
            ),
            proposals=tuple(
                Proposal(
                    id=proposal.id or "",
                    tenant_id=proposal.tenant_id,
                    domain=proposal.domain,
                    action_type=proposal.action_type,
                    entity_type=proposal.entity_type,
                    entity_id=proposal.entity_id,
                    reason=proposal.reason,
                    payload=proposal.payload,
                    risk=proposal.risk.value,
                    requested_by=proposal.requested_by,
                    approval_status="pending",
                )
                for proposal in result.proposals
            ),
        )

    def answer(
        self,
        *,
        tenant_id: str,
        query: str,
        limit: int = 5,
    ) -> IntelligenceAnswer:
        try:
            answer = self._engine.answer(
                tenant_id=tenant_id,
                query=query,
                limit=limit,
            )
        except ValueError as error:
            raise InvalidRequestError(str(error)) from error
        except Exception as error:
            raise AtlasTransportError("Local Atlas answer retrieval failed.") from error
        return IntelligenceAnswer(
            tenant_id=answer.tenant_id,
            query=answer.query,
            headline=answer.headline,
            sections=tuple(
                AnswerSection(
                    title=section.title,
                    claims=tuple(
                        AnswerClaim(
                            text=claim.text,
                            citation_ids=claim.citation_ids,
                        )
                        for claim in section.claims
                    ),
                )
                for section in answer.sections
            ),
            citations=tuple(
                AnswerCitation(
                    citation_id=citation.citation_id,
                    evidence_type=citation.evidence_type,
                    subject_id=citation.subject_id,
                    domain=citation.domain,
                    entity_type=citation.entity_type,
                    entity_id=citation.entity_id,
                    source_pointer=citation.source_pointer,
                    occurred_at=citation.occurred_at,
                    excerpt=citation.excerpt,
                )
                for citation in answer.citations
            ),
            confidence=answer.confidence,
            generated_at=answer.generated_at,
            limitations=answer.limitations,
        )

    def decide(
        self,
        proposal_id: str,
        *,
        approved: bool,
        reason: str,
        decided_by: str,
        tenant_id: str | None = None,
    ) -> ApprovalReceipt:
        if not proposal_id.strip() or not reason.strip() or not decided_by.strip():
            raise InvalidRequestError(
                "proposal_id, reason, and decided_by must not be blank."
            )
        status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
        decided_at = utc_now()
        decision = ApprovalDecision(
            proposal_id=proposal_id,
            status=status,
            reason=reason,
            decided_by=decided_by,
            decided_at=decided_at,
        )
        try:
            self._engine.memory.decide(
                decision,
                expected_tenant_id=tenant_id,
            )
        except (KeyError, ValueError) as error:
            raise ProposalConflictError(str(error)) from error
        except Exception as error:
            raise AtlasTransportError("Local Atlas decision failed.") from error
        return ApprovalReceipt(
            proposal_id=proposal_id,
            status=status.value,
            reason=reason,
            decided_by=decided_by,
            decided_at=decided_at,
        )

    def audit_events(self, *, tenant_id: str | None = None) -> tuple[AuditEvent, ...]:
        events = self._engine.memory.audit_events()
        if tenant_id is not None:
            events = [event for event in events if event["tenant_id"] == tenant_id]
        return tuple(AuditEvent(**event) for event in events)

    def search_cartography(
        self,
        *,
        tenant_id: str,
        query: str = "",
        lane: str | None = None,
        limit: int = 20,
    ) -> tuple[CartographyCard, ...]:
        try:
            cards = self._engine.cartographer.search(
                tenant_id=tenant_id,
                query=query,
                lane=lane,
                limit=limit,
            )
        except ValueError as error:
            raise InvalidRequestError(str(error)) from error
        except Exception as error:
            raise AtlasTransportError("Local Cartographer search failed.") from error
        return tuple(
            CartographyCard(
                card_id=card.card_id or "",
                tenant_id=card.tenant_id,
                project=card.project,
                lane=card.lane,
                title=card.title,
                summary=card.summary,
                source_pointers=card.source_pointers,
                tags=card.tags,
                must_retrieve=card.must_retrieve,
                decisions=card.decisions,
                verification=card.verification,
                unresolved=card.unresolved,
                details=card.details,
                confidence=card.confidence,
                risk_level=card.risk_level,
                current_verification_required=card.current_verification_required,
                created_at_utc=card.created_at_utc,
            )
            for card in cards
        )

    def proposals(
        self,
        *,
        tenant_id: str,
        status: str | None = None,
        limit: int = 100,
    ) -> tuple[Proposal, ...]:
        try:
            proposals = self._engine.memory.list_proposals(
                tenant_id=tenant_id,
                status=status,
                limit=limit,
            )
        except ValueError as error:
            raise InvalidRequestError(str(error)) from error
        except Exception as error:
            raise AtlasTransportError("Local proposal query failed.") from error
        return tuple(
            Proposal(
                id=proposal.id,
                tenant_id=proposal.tenant_id,
                domain=proposal.domain,
                action_type=proposal.action_type,
                entity_type=proposal.entity_type,
                entity_id=proposal.entity_id,
                reason=proposal.reason,
                payload=proposal.payload,
                risk=proposal.risk,
                requested_by=proposal.requested_by,
                approval_status=proposal.status,
                policy_reason=proposal.policy_reason,
                created_at=proposal.created_at,
            )
            for proposal in proposals
        )

    def overview(self, *, tenant_id: str) -> TenantOverview:
        try:
            counts = self._engine.memory.overview(tenant_id=tenant_id)
        except Exception as error:
            raise AtlasTransportError("Local overview query failed.") from error
        return TenantOverview(tenant_id=tenant_id, **counts)

    @staticmethod
    def _import_receipt(receipt: object) -> ImportReceipt:
        return ImportReceipt(
            batch_id=receipt.batch_id,
            external_batch_id=receipt.external_batch_id,
            tenant_id=receipt.tenant_id,
            connector_id=receipt.connector_id,
            domain=receipt.domain,
            source_system=receipt.source_system,
            status=receipt.status,
            record_count=receipt.record_count,
            imported_count=receipt.imported_count,
            duplicate_count=receipt.duplicate_count,
            started_at=receipt.started_at,
            completed_at=receipt.completed_at,
            payload_hash=receipt.payload_hash,
            records=tuple(
                ImportRecord(
                    record_id=item.record_id,
                    version=item.version,
                    status=item.status,
                    observation_id=item.observation_id,
                    fingerprint=item.fingerprint,
                )
                for item in receipt.records
            ),
            replayed=receipt.replayed,
            error=receipt.error,
        )

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
        try:
            connector = SnapshotConnector(
                connector_id=connector_id,
                domain=domain,
                source_system=source_system,
                records=records,
            )
            receipt = self._engine.import_batch(
                connector,
                tenant_id=tenant_id,
                external_batch_id=external_batch_id,
            )
        except ValueError as error:
            raise InvalidRequestError(str(error)) from error
        except Exception as error:
            raise AtlasTransportError("Local Atlas import failed.") from error
        return self._import_receipt(receipt)

    def imports(
        self,
        *,
        tenant_id: str,
        limit: int = 100,
    ) -> tuple[ImportReceipt, ...]:
        try:
            receipts = self._engine.memory.list_import_batches(
                tenant_id=tenant_id,
                limit=limit,
            )
        except ValueError as error:
            raise InvalidRequestError(str(error)) from error
        except Exception as error:
            raise AtlasTransportError("Local import receipt query failed.") from error
        return tuple(self._import_receipt(receipt) for receipt in receipts)

    @staticmethod
    def _backup_receipt(receipt: object) -> BackupReceipt:
        return BackupReceipt(
            backup_id=receipt.backup_id,
            created_at=receipt.created_at,
            filename=receipt.filename,
            size_bytes=receipt.size_bytes,
            sha256=receipt.sha256,
            schema_version=receipt.schema_version,
            integrity_check=receipt.integrity_check,
            status=receipt.status,
            error=receipt.error,
        )

    def create_backup(self) -> BackupReceipt:
        try:
            return self._backup_receipt(self._engine.memory.create_backup())
        except (KeyError, RuntimeError, ValueError) as error:
            raise InvalidRequestError(str(error)) from error
        except Exception as error:
            raise AtlasTransportError("Local Atlas backup failed.") from error

    def backups(self) -> tuple[BackupReceipt, ...]:
        try:
            return tuple(
                self._backup_receipt(receipt)
                for receipt in self._engine.memory.list_backups()
            )
        except Exception as error:
            raise AtlasTransportError("Local Atlas backup listing failed.") from error

    def restore_backup(
        self,
        backup_id: str,
        *,
        confirmation: str,
    ) -> RestoreReceipt:
        required = f"RESTORE {backup_id}"
        if confirmation != required:
            raise InvalidRequestError(f"Restore confirmation must be exactly: {required}")
        try:
            receipt = self._engine.memory.restore_backup(backup_id)
        except (KeyError, RuntimeError, ValueError) as error:
            raise InvalidRequestError(str(error)) from error
        except Exception as error:
            raise AtlasTransportError("Local Atlas restore failed.") from error
        return RestoreReceipt(
            restored_backup_id=receipt.restored_backup_id,
            safety_backup_id=receipt.safety_backup_id,
            restored_at=receipt.restored_at,
            sha256=receipt.sha256,
            integrity_check=receipt.integrity_check,
        )

    def export_encrypted_backup(
        self,
        backup_id: str,
        *,
        passphrase: str,
    ) -> EncryptedBackupArtifact:
        try:
            artifact = self._engine.memory.export_encrypted_backup(
                backup_id,
                passphrase=passphrase,
            )
        except (KeyError, RuntimeError, ValueError) as error:
            raise InvalidRequestError(str(error)) from error
        except Exception as error:
            raise AtlasTransportError(
                "Local encrypted backup export failed."
            ) from error
        return EncryptedBackupArtifact(
            export_id=artifact.export_id,
            backup_id=artifact.backup_id,
            created_at=artifact.created_at,
            filename=artifact.filename,
            size_bytes=artifact.size_bytes,
            sha256=artifact.sha256,
            cipher=artifact.cipher,
            kdf=artifact.kdf,
            data=artifact.data,
        )

    def import_encrypted_backup(
        self,
        artifact: bytes,
        *,
        passphrase: str,
    ) -> BackupReceipt:
        try:
            return self._backup_receipt(
                self._engine.memory.import_encrypted_backup(
                    artifact,
                    passphrase=passphrase,
                )
            )
        except (KeyError, RuntimeError, ValueError) as error:
            raise InvalidRequestError(str(error)) from error
        except Exception as error:
            raise AtlasTransportError(
                "Local encrypted backup import failed."
            ) from error

    def prune_backups(
        self,
        *,
        keep_last: int,
        confirmation: str = "",
        dry_run: bool = True,
    ) -> BackupRetentionReceipt:
        try:
            receipt = self._engine.memory.prune_backups(
                keep_last=keep_last,
                confirmation=confirmation,
                dry_run=dry_run,
            )
        except (RuntimeError, ValueError) as error:
            raise InvalidRequestError(str(error)) from error
        except Exception as error:
            raise AtlasTransportError("Local backup retention failed.") from error
        return BackupRetentionReceipt(
            keep_last=receipt.keep_last,
            candidate_backup_ids=receipt.candidate_backup_ids,
            deleted_backup_ids=receipt.deleted_backup_ids,
            reclaimed_bytes=receipt.reclaimed_bytes,
            dry_run=receipt.dry_run,
            applied_at=receipt.applied_at,
        )
