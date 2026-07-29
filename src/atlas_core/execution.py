from __future__ import annotations

import re
from dataclasses import replace
from typing import Protocol

from .memory import CompanyMemory
from .models import (
    ActionExecutionReceipt,
    ActionExecutionRequest,
    ActionExecutionResult,
)
from .provider_certification import (
    ProviderCertificationReceipt,
    ProviderCertificationRunner,
)

EXECUTOR_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
MAX_RESULT_TEXT = 500


class ActionExecutor(Protocol):
    """Server-registered provider adapter. Callers cannot supply its endpoint."""

    executor_id: str
    action_types: frozenset[str]
    supports_rollback: bool

    def execute(self, request: ActionExecutionRequest) -> ActionExecutionResult: ...

    def rollback(
        self,
        request: ActionExecutionRequest,
        receipt: ActionExecutionReceipt,
    ) -> ActionExecutionResult: ...


class ActionExecutionCoordinator:
    def __init__(
        self,
        memory: CompanyMemory,
        executors: tuple[ActionExecutor, ...] = (),
    ) -> None:
        self.memory = memory
        registry: dict[str, ActionExecutor] = {}
        for executor in executors:
            executor_id = str(executor.executor_id).strip()
            if not EXECUTOR_ID.fullmatch(executor_id):
                raise ValueError("Executor identifier is invalid.")
            if executor_id in registry:
                raise ValueError(f"Duplicate executor identifier: {executor_id}")
            if not executor.action_types or any(
                not action.strip() or len(action) > 100
                for action in executor.action_types
            ):
                raise ValueError(
                    f"Executor {executor_id} must declare valid action types."
                )
            registry[executor_id] = executor
        self._executors = registry

    def executor_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._executors))

    def provider_readiness(
        self,
        *,
        certify_local_sandboxes: bool = False,
    ) -> tuple[ProviderCertificationReceipt, ...]:
        runner = ProviderCertificationRunner()
        method = runner.certify if certify_local_sandboxes else runner.assess
        return tuple(
            method(self._executors[executor_id])
            for executor_id in sorted(self._executors)
        )

    def _executor(self, executor_id: str) -> ActionExecutor:
        executor = self._executors.get(executor_id)
        if executor is None:
            raise KeyError(f"Unknown or unavailable executor: {executor_id}")
        return executor

    @staticmethod
    def _result(result: ActionExecutionResult) -> ActionExecutionResult:
        if not isinstance(result, ActionExecutionResult):
            raise TypeError("Executor returned an invalid result.")
        summary = " ".join(result.summary.split())
        reference = " ".join(result.external_reference.split())
        error = " ".join(result.error.split())
        if not summary or len(summary) > MAX_RESULT_TEXT:
            raise ValueError("Executor result summary must be 1-500 characters.")
        if len(reference) > 200 or len(error) > MAX_RESULT_TEXT:
            raise ValueError("Executor result metadata exceeds safe bounds.")
        if result.succeeded and error:
            raise ValueError("Successful executor result must not include an error.")
        if not result.succeeded and not error:
            raise ValueError("Failed executor result must include an error.")
        return replace(
            result,
            summary=summary,
            external_reference=reference,
            error=error,
        )

    def queue(
        self,
        proposal_id: str,
        *,
        tenant_id: str,
        executor_id: str,
        requested_by: str,
        confirmation: str,
    ) -> ActionExecutionReceipt:
        executor = self._executor(executor_id)
        required = f"QUEUE EXECUTION {proposal_id} WITH {executor_id}"
        if confirmation != required:
            raise ValueError(f"Execution queue confirmation must be exactly: {required}")
        return self.memory.queue_execution(
            proposal_id=proposal_id,
            tenant_id=tenant_id,
            executor_id=executor_id,
            supported_action_types=executor.action_types,
            requested_by=requested_by,
        )

    def run(
        self,
        execution_id: str,
        *,
        tenant_id: str,
        confirmation: str,
    ) -> ActionExecutionReceipt:
        required = f"EXECUTE {execution_id}"
        if confirmation != required:
            raise ValueError(f"Execution confirmation must be exactly: {required}")
        request, receipt = self.memory.claim_execution(
            execution_id=execution_id,
            tenant_id=tenant_id,
        )
        if receipt.replayed:
            return receipt
        try:
            executor = self._executor(receipt.executor_id)
        except KeyError:
            return self.memory.finish_execution(
                execution_id=execution_id,
                status="failed",
                summary="Registered executor is unavailable.",
                error="Executor is not registered on this Atlas runtime.",
            )
        if request.action_type not in executor.action_types:
            return self.memory.finish_execution(
                execution_id=execution_id,
                status="failed",
                summary="Executor action allowlist no longer accepts this action.",
                error="Executor action allowlist mismatch.",
            )
        try:
            result = self._result(executor.execute(request))
        except Exception as error:
            return self.memory.finish_execution(
                execution_id=execution_id,
                status="uncertain",
                summary="Executor raised before Atlas received a definite outcome.",
                error=(
                    f"Executor raised {error.__class__.__name__}; "
                    "outcome requires reconciliation."
                ),
            )
        return self.memory.finish_execution(
            execution_id=execution_id,
            status="succeeded" if result.succeeded else "failed",
            summary=result.summary,
            external_reference=result.external_reference,
            error=result.error,
        )

    def rollback(
        self,
        execution_id: str,
        *,
        tenant_id: str,
        confirmation: str,
    ) -> ActionExecutionReceipt:
        required = f"ROLLBACK EXECUTION {execution_id}"
        if confirmation != required:
            raise ValueError(f"Rollback confirmation must be exactly: {required}")
        request, receipt = self.memory.claim_rollback(
            execution_id=execution_id,
            tenant_id=tenant_id,
        )
        if receipt.replayed:
            return receipt
        try:
            executor = self._executor(receipt.executor_id)
        except KeyError:
            return self.memory.finish_rollback(
                execution_id=execution_id,
                succeeded=False,
                summary="Registered rollback executor is unavailable.",
                error="Executor is not registered on this Atlas runtime.",
            )
        if not executor.supports_rollback:
            return self.memory.finish_rollback(
                execution_id=execution_id,
                succeeded=False,
                summary="Registered executor does not support rollback.",
                error="Rollback is unavailable for this executor.",
            )
        try:
            result = self._result(executor.rollback(request, receipt))
        except Exception as error:
            return self.memory.finish_rollback(
                execution_id=execution_id,
                succeeded=False,
                summary="Rollback raised before Atlas received a definite outcome.",
                error=(
                    f"Rollback raised {error.__class__.__name__}; "
                    "operator verification is required."
                ),
            )
        return self.memory.finish_rollback(
            execution_id=execution_id,
            succeeded=result.succeeded,
            summary=result.summary,
            external_reference=result.external_reference,
            error=result.error,
        )

    def reconcile(
        self,
        execution_id: str,
        *,
        tenant_id: str,
        outcome: str,
        note: str,
        reconciled_by: str,
        external_reference: str = "",
        confirmation: str,
    ) -> ActionExecutionReceipt:
        if outcome not in {"succeeded", "failed"}:
            raise ValueError("Reconciliation outcome must be succeeded or failed.")
        required = f"RECONCILE EXECUTION {execution_id} AS {outcome.upper()}"
        if confirmation != required:
            raise ValueError(
                f"Execution reconciliation confirmation must be exactly: {required}"
            )
        return self.memory.reconcile_execution(
            execution_id=execution_id,
            tenant_id=tenant_id,
            outcome=outcome,
            note=note,
            reconciled_by=reconciled_by,
            external_reference=external_reference,
        )

    def list(
        self,
        *,
        tenant_id: str,
        status: str | None = None,
        limit: int = 100,
    ) -> tuple[ActionExecutionReceipt, ...]:
        return self.memory.list_executions(
            tenant_id=tenant_id,
            status=status,
            limit=limit,
        )
