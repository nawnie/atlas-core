from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from .models import (
    ActionExecutionReceipt,
    ActionExecutionRequest,
    ActionExecutionResult,
    utc_now,
)

if TYPE_CHECKING:
    from .execution import ActionExecutor

PROVIDER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
HOSTNAME = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)


class ProviderEnvironment(StrEnum):
    LOCAL_SANDBOX = "local_sandbox"
    VENDOR_SANDBOX = "vendor_sandbox"
    PRODUCTION = "production"


class CredentialStrategy(StrEnum):
    NONE = "none"
    ENVIRONMENT_REFERENCE = "environment_reference"
    SECRET_MANAGER_REFERENCE = "secret_manager_reference"


@dataclass(frozen=True)
class ProviderManifest:
    provider_id: str
    display_name: str
    domain: str
    environment: ProviderEnvironment
    action_types: tuple[str, ...]
    supports_rollback: bool
    network_access: bool
    credential_strategy: CredentialStrategy
    synthetic_only: bool
    live_action_authorized: bool = False
    endpoint_hosts: tuple[str, ...] = ()
    contract_version: int = 1

    def __post_init__(self) -> None:
        if not PROVIDER_ID.fullmatch(self.provider_id):
            raise ValueError("Provider identifier is invalid.")
        if not self.display_name.strip() or len(self.display_name) > 100:
            raise ValueError("Provider display name must be 1-100 characters.")
        if not self.domain.strip() or len(self.domain) > 100:
            raise ValueError("Provider domain must be 1-100 characters.")
        if not self.action_types or len(set(self.action_types)) != len(
            self.action_types
        ):
            raise ValueError("Provider action types must be non-empty and unique.")
        if any(
            not action.strip() or len(action) > 100
            for action in self.action_types
        ):
            raise ValueError("Provider action type is invalid.")
        if self.contract_version != 1:
            raise ValueError("Unsupported provider contract version.")
        if self.network_access and not self.endpoint_hosts:
            raise ValueError("Networked providers must declare endpoint hosts.")
        if not self.network_access and self.endpoint_hosts:
            raise ValueError("Offline providers cannot declare endpoint hosts.")
        if any(
            "://" in host or "/" in host or not HOSTNAME.fullmatch(host)
            for host in self.endpoint_hosts
        ):
            raise ValueError("Provider endpoint hosts must be hostnames only.")
        if (
            not self.network_access
            and self.credential_strategy is not CredentialStrategy.NONE
        ):
            raise ValueError("Offline providers cannot require credentials.")
        if (
            self.environment is ProviderEnvironment.LOCAL_SANDBOX
            and (
                self.network_access
                or not self.synthetic_only
                or self.live_action_authorized
                or self.credential_strategy is not CredentialStrategy.NONE
            )
        ):
            raise ValueError(
                "Local sandbox providers must be offline, credential-free, "
                "synthetic-only, and not live-authorized."
            )
        if (
            self.environment is ProviderEnvironment.PRODUCTION
            and self.synthetic_only
        ):
            raise ValueError("Production providers cannot be synthetic-only.")

    def safe_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["environment"] = self.environment.value
        payload["credential_strategy"] = self.credential_strategy.value
        return payload


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


class ConformanceActionExecutor(Protocol):
    executor_id: str
    action_types: frozenset[str]
    supports_rollback: bool
    manifest: ProviderManifest

    def execute(self, request: ActionExecutionRequest) -> ActionExecutionResult: ...

    def rollback(
        self,
        request: ActionExecutionRequest,
        receipt: ActionExecutionReceipt,
    ) -> ActionExecutionResult: ...

    def create_conformance_request(
        self, certification_id: str
    ) -> ActionExecutionRequest: ...

    def inspect_conformance_state(
        self, request: ActionExecutionRequest
    ) -> dict[str, str]: ...

    def cleanup_conformance(self, request: ActionExecutionRequest) -> None: ...


class ProviderCertificationRunner:
    """Static and synthetic-only dynamic validation for provider adapters."""

    @staticmethod
    def _manifest(executor: ActionExecutor) -> ProviderManifest:
        manifest = getattr(executor, "manifest", None)
        if not isinstance(manifest, ProviderManifest):
            raise ValueError("Executor does not declare a ProviderManifest.")
        return manifest

    @staticmethod
    def _manifest_hash(manifest: ProviderManifest) -> str:
        encoded = json.dumps(
            manifest.safe_dict(),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def manifest_sha256(manifest: ProviderManifest) -> str:
        """Return the canonical safe-manifest digest used by receipts."""

        return ProviderCertificationRunner._manifest_hash(manifest)

    @staticmethod
    def _certification_id(manifest: ProviderManifest) -> str:
        digest = hashlib.sha256(
            (
                f"{manifest.provider_id}:{ProviderCertificationRunner._manifest_hash(manifest)}"
            ).encode()
        ).hexdigest()[:20]
        return f"cert_{digest}"

    @staticmethod
    def _check(name: str, passed: bool, detail: str) -> ProviderCertificationCheck:
        return ProviderCertificationCheck(
            name=name,
            status="passed" if passed else "failed",
            detail=detail,
        )

    def assess(self, executor: ActionExecutor) -> ProviderCertificationReceipt:
        try:
            manifest = self._manifest(executor)
        except ValueError:
            return ProviderCertificationReceipt(
                certification_id="",
                provider_id=str(getattr(executor, "executor_id", "")),
                display_name="Unmanifested executor",
                domain="",
                environment="unknown",
                status="failed",
                manifest_sha256="",
                action_types=tuple(sorted(getattr(executor, "action_types", ()))),
                supports_rollback=bool(
                    getattr(executor, "supports_rollback", False)
                ),
                network_access=False,
                credential_strategy="unknown",
                synthetic_only=False,
                live_action_authorized=False,
                checks=(
                    self._check(
                        "manifest",
                        False,
                        "A valid ProviderManifest is required for certification.",
                    ),
                ),
                generated_at=utc_now(),
            )
        checks = (
            self._check(
                "provider_identity",
                manifest.provider_id == executor.executor_id,
                "Manifest provider ID matches the registered executor ID.",
            ),
            self._check(
                "action_allowlist",
                frozenset(manifest.action_types) == executor.action_types,
                "Manifest actions match the runtime executor allowlist.",
            ),
            self._check(
                "rollback_contract",
                manifest.supports_rollback == executor.supports_rollback,
                "Manifest rollback capability matches the runtime executor.",
            ),
            self._check(
                "credential_reference",
                manifest.credential_strategy
                in {
                    CredentialStrategy.NONE,
                    CredentialStrategy.ENVIRONMENT_REFERENCE,
                    CredentialStrategy.SECRET_MANAGER_REFERENCE,
                },
                "Manifest declares a reference strategy and never a secret value.",
            ),
            self._check(
                "live_authorization",
                (
                    manifest.environment is not ProviderEnvironment.PRODUCTION
                    or manifest.live_action_authorized
                ),
                (
                    "Production-class providers require explicit live-action "
                    "authorization in their reviewed runtime manifest."
                ),
            ),
        )
        failed = any(check.status == "failed" for check in checks)
        if failed:
            status = "failed"
        elif manifest.environment is ProviderEnvironment.LOCAL_SANDBOX:
            status = "ready_for_conformance"
        else:
            status = "production_review_required"
        return self._receipt(manifest, status=status, checks=checks)

    def certify(self, executor: ActionExecutor) -> ProviderCertificationReceipt:
        assessed = self.assess(executor)
        if assessed.status != "ready_for_conformance":
            return assessed
        manifest = self._manifest(executor)
        required_methods = (
            "create_conformance_request",
            "inspect_conformance_state",
            "cleanup_conformance",
        )
        if any(not callable(getattr(executor, name, None)) for name in required_methods):
            return self._receipt(
                manifest,
                status="failed",
                checks=assessed.checks
                + (
                    self._check(
                        "conformance_fixture",
                        False,
                        "Local sandbox is missing its synthetic conformance fixture.",
                    ),
                ),
            )

        certification_id = assessed.certification_id
        request: ActionExecutionRequest | None = None
        checks = list(assessed.checks)
        try:
            request = executor.create_conformance_request(certification_id)  # type: ignore[attr-defined]
            request_valid = (
                request.tenant_id.startswith("atlas-cert-")
                and request.domain == manifest.domain
                and request.action_type in manifest.action_types
                and request.idempotency_key.startswith("cert-idem-")
            )
            checks.append(
                self._check(
                    "synthetic_fixture",
                    request_valid,
                    "Fixture is isolated under an Atlas certification tenant.",
                )
            )
            if not request_valid:
                return self._receipt(manifest, status="failed", checks=tuple(checks))
            before = executor.inspect_conformance_state(request)  # type: ignore[attr-defined]
            first = executor.execute(request)
            after = executor.inspect_conformance_state(request)  # type: ignore[attr-defined]
            replay = executor.execute(request)
            checks.extend(
                (
                    self._check(
                        "execute",
                        isinstance(first, ActionExecutionResult) and first.succeeded,
                        "Synthetic action returned a definite successful result.",
                    ),
                    self._check(
                        "provider_state_change",
                        before != after,
                        "Synthetic provider state changed after execution.",
                    ),
                    self._check(
                        "idempotent_replay",
                        (
                            replay.succeeded
                            and replay.external_reference
                            == first.external_reference
                            and executor.inspect_conformance_state(request) == after  # type: ignore[attr-defined]
                        ),
                        "Replay returned the same reference without another state change.",
                    ),
                )
            )
            if manifest.supports_rollback and first.succeeded:
                receipt = ActionExecutionReceipt(
                    execution_id=request.execution_id,
                    proposal_id=request.proposal_id,
                    tenant_id=request.tenant_id,
                    executor_id=manifest.provider_id,
                    action_type=request.action_type,
                    idempotency_key=request.idempotency_key,
                    status="succeeded",
                    attempt_count=1,
                    requested_by="atlas-certification",
                    queued_at=utc_now(),
                    result_summary=first.summary,
                    external_reference=first.external_reference,
                )
                rollback = executor.rollback(request, receipt)
                restored = executor.inspect_conformance_state(request)  # type: ignore[attr-defined]
                rollback_replay = executor.rollback(request, receipt)
                checks.extend(
                    (
                        self._check(
                            "rollback",
                            rollback.succeeded,
                            "Synthetic rollback returned a definite successful result.",
                        ),
                        self._check(
                            "state_restoration",
                            restored == before,
                            "Rollback restored the recorded pre-execution state.",
                        ),
                        self._check(
                            "rollback_replay",
                            (
                                rollback_replay.succeeded
                                and executor.inspect_conformance_state(request)  # type: ignore[attr-defined]
                                == before
                            ),
                            "Rollback replay left provider state unchanged.",
                        ),
                    )
                )
        except Exception as error:
            checks.append(
                self._check(
                    "dynamic_conformance",
                    False,
                    (
                        f"Provider raised {error.__class__.__name__}; "
                        "no exception text was retained."
                    ),
                )
            )
        finally:
            if request is not None:
                try:
                    executor.cleanup_conformance(request)  # type: ignore[attr-defined]
                    checks.append(
                        self._check(
                            "fixture_cleanup",
                            True,
                            "Synthetic certification state was removed.",
                        )
                    )
                except Exception as error:
                    checks.append(
                        self._check(
                            "fixture_cleanup",
                            False,
                            (
                                f"Cleanup raised {error.__class__.__name__}; "
                                "manual inspection is required."
                            ),
                        )
                    )
        status = (
            "sandbox_certified"
            if checks and all(check.status == "passed" for check in checks)
            else "failed"
        )
        return self._receipt(manifest, status=status, checks=tuple(checks))

    def _receipt(
        self,
        manifest: ProviderManifest,
        *,
        status: str,
        checks: tuple[ProviderCertificationCheck, ...],
    ) -> ProviderCertificationReceipt:
        return ProviderCertificationReceipt(
            certification_id=self._certification_id(manifest),
            provider_id=manifest.provider_id,
            display_name=manifest.display_name,
            domain=manifest.domain,
            environment=manifest.environment.value,
            status=status,
            manifest_sha256=self._manifest_hash(manifest),
            action_types=tuple(sorted(manifest.action_types)),
            supports_rollback=manifest.supports_rollback,
            network_access=manifest.network_access,
            credential_strategy=manifest.credential_strategy.value,
            synthetic_only=manifest.synthetic_only,
            live_action_authorized=manifest.live_action_authorized,
            checks=checks,
            generated_at=utc_now(),
        )
