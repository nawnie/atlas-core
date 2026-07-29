from __future__ import annotations

import json
import ssl
import struct
from dataclasses import fields
from typing import Any, TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    Request,
    build_opener,
)

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
    AuthenticationError,
    AuthorizationError,
    ExecutionConflictError,
    InvalidRequestError,
    ProposalConflictError,
)

MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_RECOVERY_BYTES = 512 * 1024 * 1024
_T = TypeVar("_T")


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        raise AtlasTransportError("Atlas Gateway redirects are not allowed.")


def _tuple_fields(payload: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    copy = dict(payload)
    for name in names:
        copy[name] = tuple(copy.get(name, ()))
    return copy


def _dataclass_payload(model: type[_T], payload: dict[str, Any]) -> _T:
    allowed = {field.name for field in fields(model)}
    return model(**{key: value for key, value in payload.items() if key in allowed})


class HttpAtlasTransport:
    """Authenticated Atlas Gateway transport.

    HTTPS is mandatory except for an explicitly enabled loopback-only test
    connection. Redirects are rejected so credentials cannot be forwarded to a
    different origin or downgraded endpoint.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_token: str,
        timeout: float = 15.0,
        ca_file: str | None = None,
        allow_insecure_loopback: bool = False,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("base_url must be an absolute gateway origin or path.")
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and loopback and allow_insecure_loopback
        ):
            raise ValueError("Remote Atlas connections require HTTPS.")
        if not 32 <= len(api_token) <= 4096 or api_token.strip() != api_token:
            raise ValueError("api_token must be between 32 and 4096 characters.")
        if any(character.isspace() or ord(character) < 32 for character in api_token):
            raise ValueError("api_token must not contain whitespace or control characters.")
        if not 0 < timeout <= 120:
            raise ValueError("timeout must be between 0 and 120 seconds.")
        context = ssl.create_default_context(cafile=ca_file)
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token
        self._timeout = timeout
        self._opener = build_opener(_RejectRedirects(), HTTPSHandler(context=context))

    @staticmethod
    def _tenant_path(tenant_id: str) -> str:
        if not tenant_id.strip() or len(tenant_id) > 100:
            raise InvalidRequestError(
                "tenant_id is required and must be at most 100 characters."
            )
        return quote(tenant_id, safe="")

    @staticmethod
    def _identifier(value: str, name: str) -> str:
        if not value.strip() or len(value) > 200:
            raise InvalidRequestError(f"{name} is required and must be at most 200 characters.")
        return quote(value, safe="")

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_token}",
            "User-Agent": "atlas-core-sdk/0.1.0",
        }
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":"), default=str).encode()
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self._base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                content_type = response.headers.get_content_type()
                if content_type != "application/json":
                    raise AtlasTransportError(
                        "Atlas Gateway returned an unexpected content type."
                    )
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise AtlasTransportError("Atlas Gateway response is too large.")
        except HTTPError as error:
            raw = error.read(MAX_RESPONSE_BYTES + 1)
            try:
                response_payload = json.loads(raw)
                message = str(
                    response_payload.get("error", {}).get(
                        "message",
                        f"Atlas Gateway request failed ({error.code}).",
                    )
                )
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                message = f"Atlas Gateway request failed ({error.code})."
            if error.code == 400:
                raise InvalidRequestError(message) from error
            if error.code == 401:
                raise AuthenticationError(message) from error
            if error.code == 403:
                raise AuthorizationError(message) from error
            if error.code == 409:
                if "/executions/" in path or path.endswith("/execution"):
                    raise ExecutionConflictError(message) from error
                raise ProposalConflictError(message) from error
            raise AtlasTransportError(message) from error
        except (OSError, TimeoutError, URLError) as error:
            raise AtlasTransportError("Atlas Gateway could not be reached.") from error
        try:
            decoded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AtlasTransportError("Atlas Gateway returned invalid JSON.") from error
        if not isinstance(decoded, dict):
            raise AtlasTransportError("Atlas Gateway response must be a JSON object.")
        return decoded

    def _binary_request(
        self,
        method: str,
        path: str,
        *,
        body: bytes,
        content_type: str,
        max_bytes: int = MAX_RECOVERY_BYTES,
    ) -> tuple[bytes, dict[str, str]]:
        request = Request(
            f"{self._base_url}{path}",
            data=body,
            headers={
                "Accept": "application/vnd.atlas.encrypted-backup",
                "Authorization": f"Bearer {self._api_token}",
                "Content-Type": content_type,
                "User-Agent": "atlas-core-sdk/0.1.0",
            },
            method=method,
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    raise AtlasTransportError(
                        "Atlas Gateway recovery response is too large."
                    )
                headers = {
                    name.lower(): value for name, value in response.headers.items()
                }
                return raw, headers
        except HTTPError as error:
            raw = error.read(MAX_RESPONSE_BYTES + 1)
            try:
                payload = json.loads(raw)
                message = str(
                    payload.get("error", {}).get(
                        "message",
                        f"Atlas Gateway request failed ({error.code}).",
                    )
                )
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                message = f"Atlas Gateway request failed ({error.code})."
            if error.code == 400:
                raise InvalidRequestError(message) from error
            if error.code == 401:
                raise AuthenticationError(message) from error
            if error.code == 403:
                raise AuthorizationError(message) from error
            raise AtlasTransportError(message) from error
        except (OSError, TimeoutError, URLError) as error:
            raise AtlasTransportError("Atlas Gateway could not be reached.") from error

    def ingest(self, request: IngestRequest) -> IngestResponse:
        payload = self._request(
            "POST",
            f"/v1/tenants/{self._tenant_path(request.tenant_id)}/ingest",
            {
                "domain": request.domain,
                "source_system": request.source_system,
                "record": request.record,
            },
        )
        payload["changes"] = tuple(
            _dataclass_payload(Change, item) for item in payload.get("changes", ())
        )
        payload["proposals"] = tuple(
            _dataclass_payload(Proposal, item) for item in payload.get("proposals", ())
        )
        return _dataclass_payload(IngestResponse, payload)

    def answer(
        self,
        *,
        tenant_id: str,
        query: str,
        limit: int = 5,
    ) -> IntelligenceAnswer:
        parameters = urlencode({"query": query, "limit": str(limit)})
        payload = self._request(
            "GET",
            f"/v1/tenants/{self._tenant_path(tenant_id)}/answer?{parameters}",
        )
        payload["sections"] = tuple(
            _dataclass_payload(
                AnswerSection,
                {
                    **section,
                    "claims": tuple(
                        _dataclass_payload(
                            AnswerClaim,
                            _tuple_fields(claim, ("citation_ids",)),
                        )
                        for claim in section.get("claims", ())
                    ),
                },
            )
            for section in payload.get("sections", ())
        )
        payload["citations"] = tuple(
            _dataclass_payload(AnswerCitation, citation)
            for citation in payload.get("citations", ())
        )
        payload["limitations"] = tuple(payload.get("limitations", ()))
        return _dataclass_payload(IntelligenceAnswer, payload)

    def decide(
        self,
        proposal_id: str,
        *,
        approved: bool,
        reason: str,
        decided_by: str,
        tenant_id: str | None = None,
    ) -> ApprovalReceipt:
        if tenant_id is None:
            raise InvalidRequestError("tenant_id is required for remote decisions.")
        payload = self._request(
            "POST",
            (
                f"/v1/tenants/{self._tenant_path(tenant_id)}/proposals/"
                f"{self._identifier(proposal_id, 'proposal_id')}/decision"
            ),
            {
                "approved": approved,
                "reason": reason,
                "decided_by": decided_by,
            },
        )
        return _dataclass_payload(ApprovalReceipt, payload)

    def audit_events(self, *, tenant_id: str | None = None) -> tuple[AuditEvent, ...]:
        if tenant_id is None:
            raise InvalidRequestError("tenant_id is required for remote audit reads.")
        payload = self._request(
            "GET",
            f"/v1/tenants/{self._tenant_path(tenant_id)}/audit",
        )
        return tuple(
            _dataclass_payload(AuditEvent, item) for item in payload.get("items", ())
        )

    def search_cartography(
        self,
        *,
        tenant_id: str,
        query: str = "",
        lane: str | None = None,
        limit: int = 20,
    ) -> tuple[CartographyCard, ...]:
        parameters = urlencode(
            {"query": query, "lane": lane or "", "limit": str(limit)}
        )
        payload = self._request(
            "GET",
            f"/v1/tenants/{self._tenant_path(tenant_id)}/cartography?{parameters}",
        )
        return tuple(
            _dataclass_payload(
                CartographyCard,
                _tuple_fields(
                    item,
                    (
                        "source_pointers",
                        "tags",
                        "must_retrieve",
                        "decisions",
                        "verification",
                        "unresolved",
                    ),
                ),
            )
            for item in payload.get("items", ())
        )

    def proposals(
        self,
        *,
        tenant_id: str,
        status: str | None = None,
        limit: int = 100,
    ) -> tuple[Proposal, ...]:
        parameters = urlencode({"status": status or "", "limit": str(limit)})
        payload = self._request(
            "GET",
            f"/v1/tenants/{self._tenant_path(tenant_id)}/proposals?{parameters}",
        )
        return tuple(
            _dataclass_payload(Proposal, item) for item in payload.get("items", ())
        )

    def provider_readiness(
        self,
        *,
        certify_local_sandboxes: bool = False,
    ) -> tuple[ProviderCertificationReceipt, ...]:
        if certify_local_sandboxes:
            raise InvalidRequestError(
                "Dynamic provider certification is local-runtime only."
            )
        payload = self._request("GET", "/v1/admin/providers")
        return tuple(
            _dataclass_payload(
                ProviderCertificationReceipt,
                {
                    **item,
                    "action_types": tuple(item.get("action_types", ())),
                    "checks": tuple(
                        _dataclass_payload(ProviderCertificationCheck, check)
                        for check in item.get("checks", ())
                    ),
                },
            )
            for item in payload.get("items", ())
        )

    def executions(
        self,
        *,
        tenant_id: str,
        status: str | None = None,
        limit: int = 100,
    ) -> tuple[ExecutionReceipt, ...]:
        parameters = urlencode({"status": status or "", "limit": str(limit)})
        payload = self._request(
            "GET",
            f"/v1/tenants/{self._tenant_path(tenant_id)}/executions?{parameters}",
        )
        return tuple(
            _dataclass_payload(ExecutionReceipt, item)
            for item in payload.get("items", ())
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
        payload = self._request(
            "POST",
            (
                f"/v1/tenants/{self._tenant_path(tenant_id)}/proposals/"
                f"{self._identifier(proposal_id, 'proposal_id')}/execution"
            ),
            {
                "executor_id": executor_id,
                "requested_by": requested_by,
                "confirmation": confirmation,
            },
        )
        return _dataclass_payload(ExecutionReceipt, payload)

    def run_execution(
        self,
        execution_id: str,
        *,
        tenant_id: str,
        confirmation: str,
    ) -> ExecutionReceipt:
        payload = self._request(
            "POST",
            (
                f"/v1/tenants/{self._tenant_path(tenant_id)}/executions/"
                f"{self._identifier(execution_id, 'execution_id')}/run"
            ),
            {"confirmation": confirmation},
        )
        return _dataclass_payload(ExecutionReceipt, payload)

    def rollback_execution(
        self,
        execution_id: str,
        *,
        tenant_id: str,
        confirmation: str,
    ) -> ExecutionReceipt:
        payload = self._request(
            "POST",
            (
                f"/v1/tenants/{self._tenant_path(tenant_id)}/executions/"
                f"{self._identifier(execution_id, 'execution_id')}/rollback"
            ),
            {"confirmation": confirmation},
        )
        return _dataclass_payload(ExecutionReceipt, payload)

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
        payload = self._request(
            "POST",
            (
                f"/v1/tenants/{self._tenant_path(tenant_id)}/executions/"
                f"{self._identifier(execution_id, 'execution_id')}/reconcile"
            ),
            {
                "outcome": outcome,
                "note": note,
                "reconciled_by": reconciled_by,
                "external_reference": external_reference,
                "confirmation": confirmation,
            },
        )
        return _dataclass_payload(ExecutionReceipt, payload)

    def overview(self, *, tenant_id: str) -> TenantOverview:
        payload = self._request(
            "GET",
            f"/v1/tenants/{self._tenant_path(tenant_id)}/overview",
        )
        return _dataclass_payload(TenantOverview, payload)

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
        payload = self._request(
            "POST",
            f"/v1/tenants/{self._tenant_path(tenant_id)}/imports",
            {
                "connector_id": connector_id,
                "domain": domain,
                "source_system": source_system,
                "external_batch_id": external_batch_id,
                "records": records,
            },
        )
        payload["records"] = tuple(
            _dataclass_payload(ImportRecord, item)
            for item in payload.get("records", ())
        )
        return _dataclass_payload(ImportReceipt, payload)

    def imports(
        self,
        *,
        tenant_id: str,
        limit: int = 100,
    ) -> tuple[ImportReceipt, ...]:
        payload = self._request(
            "GET",
            (
                f"/v1/tenants/{self._tenant_path(tenant_id)}/imports?"
                f"{urlencode({'limit': str(limit)})}"
            ),
        )
        receipts = []
        for item in payload.get("items", ()):
            item["records"] = tuple(
                _dataclass_payload(ImportRecord, record)
                for record in item.get("records", ())
            )
            receipts.append(_dataclass_payload(ImportReceipt, item))
        return tuple(receipts)

    def create_backup(self) -> BackupReceipt:
        return _dataclass_payload(
            BackupReceipt,
            self._request("POST", "/v1/admin/backups", {}),
        )

    @staticmethod
    def _automation_event(payload: dict[str, Any]) -> AutomationEvent:
        return _dataclass_payload(AutomationEvent, payload)

    @classmethod
    def _automation_run(
        cls,
        payload: dict[str, Any],
    ) -> AutomationRunReceipt:
        payload["events"] = tuple(
            cls._automation_event(event)
            for event in payload.get("events", ())
        )
        return _dataclass_payload(AutomationRunReceipt, payload)

    def automation_status(self) -> AutomationStatus:
        payload = self._request("GET", "/v1/admin/automation")
        last_run = payload.get("last_run")
        payload["last_run"] = (
            self._automation_run(last_run) if isinstance(last_run, dict) else None
        )
        payload["recent_events"] = tuple(
            self._automation_event(event)
            for event in payload.get("recent_events", ())
        )
        return _dataclass_payload(AutomationStatus, payload)

    def backups(self) -> tuple[BackupReceipt, ...]:
        payload = self._request("GET", "/v1/admin/backups")
        return tuple(
            _dataclass_payload(BackupReceipt, item)
            for item in payload.get("items", ())
        )

    def restore_backup(
        self,
        backup_id: str,
        *,
        confirmation: str,
    ) -> RestoreReceipt:
        payload = self._request(
            "POST",
            f"/v1/admin/backups/{self._identifier(backup_id, 'backup_id')}/restore",
            {"confirmation": confirmation},
        )
        return _dataclass_payload(RestoreReceipt, payload)

    def export_encrypted_backup(
        self,
        backup_id: str,
        *,
        passphrase: str,
    ) -> EncryptedBackupArtifact:
        body = json.dumps(
            {"passphrase": passphrase},
            separators=(",", ":"),
        ).encode()
        data, headers = self._binary_request(
            "POST",
            f"/v1/admin/backups/{self._identifier(backup_id, 'backup_id')}/export",
            body=body,
            content_type="application/json",
        )
        try:
            return EncryptedBackupArtifact(
                export_id=headers["x-atlas-export-id"],
                backup_id=headers["x-atlas-backup-id"],
                created_at=headers["x-atlas-created-at"],
                filename=headers["x-atlas-filename"],
                size_bytes=len(data),
                sha256=headers["x-atlas-sha256"],
                cipher=headers["x-atlas-cipher"],
                kdf=headers["x-atlas-kdf"],
                data=data,
            )
        except KeyError as error:
            raise AtlasTransportError(
                "Atlas Gateway recovery response metadata is incomplete."
            ) from error

    def import_encrypted_backup(
        self,
        artifact: bytes,
        *,
        passphrase: str,
    ) -> BackupReceipt:
        passphrase_bytes = passphrase.encode("utf-8")
        if len(passphrase_bytes) > 1024:
            raise InvalidRequestError("passphrase is too large.")
        envelope = struct.pack(">I", len(passphrase_bytes)) + passphrase_bytes + artifact
        raw, headers = self._binary_request(
            "POST",
            "/v1/admin/backups/import",
            body=envelope,
            content_type="application/vnd.atlas.encrypted-backup",
            max_bytes=MAX_RESPONSE_BYTES,
        )
        if headers.get("content-type", "").split(";", 1)[0] != "application/json":
            raise AtlasTransportError(
                "Atlas Gateway returned an unexpected import response."
            )
        try:
            return _dataclass_payload(BackupReceipt, json.loads(raw))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise AtlasTransportError(
                "Atlas Gateway returned invalid import metadata."
            ) from error

    def prune_backups(
        self,
        *,
        keep_last: int,
        confirmation: str = "",
        dry_run: bool = True,
    ) -> BackupRetentionReceipt:
        payload = self._request(
            "POST",
            "/v1/admin/backups/retention",
            {
                "keep_last": keep_last,
                "confirmation": confirmation,
                "dry_run": dry_run,
            },
        )
        return _dataclass_payload(
            BackupRetentionReceipt,
            _tuple_fields(
                payload,
                ("candidate_backup_ids", "deleted_backup_ids"),
            ),
        )
