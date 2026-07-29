from __future__ import annotations

import hashlib
import json
import secrets
import ssl
import struct
import time
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from atlas_core import ActionExecutor, IndustryAdapter
from atlas_core_sdk import (
    AtlasClient,
    AtlasError,
    ExecutionConflictError,
    InvalidRequestError,
    ProposalConflictError,
)

MAX_BODY_BYTES = 6 * 1024 * 1024
MAX_RECOVERY_BYTES = 512 * 1024 * 1024
MAX_TOKEN_BYTES = 4096
PERMISSIONS = frozenset(
    {
        "read",
        "ingest",
        "approve",
        "execute",
        "reconcile",
        "backup",
        "providers",
    }
)
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


@dataclass(frozen=True)
class AccessGrant:
    name: str
    token_sha256: str
    tenants: frozenset[str]
    permissions: frozenset[str]

    @classmethod
    def from_token(
        cls,
        *,
        name: str,
        token: str,
        tenants: frozenset[str],
        permissions: frozenset[str],
    ) -> AccessGrant:
        if not name.strip() or len(name) > 100:
            raise ValueError("Grant name is required and must be at most 100 characters.")
        if not 32 <= len(token) <= MAX_TOKEN_BYTES:
            raise ValueError("Gateway tokens must be between 32 and 4096 characters.")
        if token.strip() != token or any(
            character.isspace() or ord(character) < 32 for character in token
        ):
            raise ValueError("Gateway tokens must not contain whitespace or controls.")
        if not tenants or any(
            not tenant.strip() or len(tenant) > 100 for tenant in tenants
        ):
            raise ValueError("At least one valid tenant grant is required.")
        unknown = permissions - PERMISSIONS
        if unknown or not permissions:
            raise ValueError(f"Unknown or empty gateway permissions: {sorted(unknown)}")
        if permissions & {"backup", "providers"} and "*" not in tenants:
            raise ValueError(
                "Backup and provider permissions require the wildcard tenant grant."
            )
        return cls(
            name=name,
            token_sha256=hashlib.sha256(token.encode()).hexdigest(),
            tenants=tenants,
            permissions=permissions,
        )

    def allows(self, permission: str, tenant_id: str | None = None) -> bool:
        if permission not in self.permissions:
            return False
        if tenant_id is None:
            return permission in {"backup", "providers"} and "*" in self.tenants
        return "*" in self.tenants or tenant_id in self.tenants


class RateLimiter:
    def __init__(self, requests_per_minute: int) -> None:
        if not 1 <= requests_per_minute <= 10000:
            raise ValueError("requests_per_minute must be between 1 and 10000.")
        self._limit = requests_per_minute
        self._lock = Lock()
        self._windows: dict[str, tuple[int, int]] = {}

    def allow(self, key: str) -> tuple[bool, int]:
        current_window = int(time.monotonic() // 60)
        with self._lock:
            window, count = self._windows.get(key, (current_window, 0))
            if window != current_window:
                window, count = current_window, 0
            count += 1
            self._windows[key] = (window, count)
        return count <= self._limit, max(0, self._limit - count)


class AtlasGatewayServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        client: AtlasClient,
        grants: tuple[AccessGrant, ...],
        requests_per_minute: int,
    ) -> None:
        if not grants:
            raise ValueError("At least one access grant is required.")
        digests = [grant.token_sha256 for grant in grants]
        if len(digests) != len(set(digests)):
            raise ValueError("Gateway token hashes must be unique.")
        super().__init__(server_address, AtlasGatewayHandler)
        self.client = client
        self.grants = grants
        self.rate_limiter = RateLimiter(requests_per_minute)
        self.tls_enabled = False


class AtlasGatewayHandler(BaseHTTPRequestHandler):
    server: AtlasGatewayServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        print(f"[atlas-gateway] {self.address_string()} {format % args}")

    def _headers(self, content_type: str, length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")

    def _json(
        self,
        status: int,
        payload: Any,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, sort_keys=True, default=str).encode()
        self.send_response(status)
        self._headers("application/json; charset=utf-8", len(body))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, code: str, message: str) -> None:
        self._json(status, {"error": {"code": code, "message": message}})

    def _binary(
        self,
        status: int,
        body: bytes,
        *,
        extra_headers: dict[str, str],
    ) -> None:
        self.send_response(status)
        self._headers("application/vnd.atlas.encrypted-backup", len(body))
        for name, value in extra_headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _authenticate(self) -> AccessGrant:
        values = self.headers.get_all("Authorization", [])
        if len(values) != 1 or not values[0].startswith("Bearer "):
            raise PermissionError("A bearer service token is required.")
        token = values[0].removeprefix("Bearer ")
        if not token or len(token) > MAX_TOKEN_BYTES:
            raise PermissionError("The bearer service token is invalid.")
        digest = hashlib.sha256(token.encode()).hexdigest()
        matched = None
        for grant in self.server.grants:
            if secrets.compare_digest(digest, grant.token_sha256):
                matched = grant
        if matched is None:
            raise PermissionError("The bearer service token is invalid.")
        allowed, remaining = self.server.rate_limiter.allow(digest)
        if not allowed:
            raise RuntimeError("rate_limit")
        self._rate_remaining = remaining
        return matched

    @staticmethod
    def _tenant(value: str) -> str:
        tenant_id = unquote(value)
        if (
            not tenant_id.strip()
            or len(tenant_id) > 100
            or "/" in tenant_id
            or "\\" in tenant_id
        ):
            raise ValueError("Invalid tenant identifier.")
        return tenant_id

    @staticmethod
    def _identifier(value: str, name: str) -> str:
        decoded = unquote(value)
        if (
            not decoded.strip()
            or len(decoded) > 200
            or "/" in decoded
            or "\\" in decoded
        ):
            raise ValueError(f"Invalid {name}.")
        return decoded

    def _read_json(self) -> dict[str, Any]:
        if self.headers.get_content_type() != "application/json":
            raise TypeError("Content-Type must be application/json.")
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            raise ValueError("Content-Length is required.")
        length = int(length_header)
        if not 0 <= length <= MAX_BODY_BYTES:
            raise ValueError(f"JSON body may not exceed {MAX_BODY_BYTES} bytes.")
        payload = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object.")
        return payload

    def _read_recovery_upload(self) -> tuple[str, bytes]:
        if (
            self.headers.get_content_type()
            != "application/vnd.atlas.encrypted-backup"
        ):
            raise TypeError(
                "Content-Type must be application/vnd.atlas.encrypted-backup."
            )
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            raise ValueError("Content-Length is required.")
        length = int(length_header)
        if not 5 <= length <= MAX_RECOVERY_BYTES:
            raise ValueError(
                f"Recovery upload may not exceed {MAX_RECOVERY_BYTES} bytes."
            )
        envelope = self.rfile.read(length)
        passphrase_length = struct.unpack(">I", envelope[:4])[0]
        if not 1 <= passphrase_length <= 1024 or 4 + passphrase_length >= len(
            envelope
        ):
            raise ValueError("Recovery upload envelope is invalid.")
        try:
            passphrase = envelope[4 : 4 + passphrase_length].decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("Recovery passphrase must be UTF-8 text.") from error
        return passphrase, envelope[4 + passphrase_length :]

    def _authorize(
        self,
        grant: AccessGrant,
        permission: str,
        tenant_id: str | None = None,
    ) -> None:
        if not grant.allows(permission, tenant_id):
            raise LookupError("The service identity is not authorized for this operation.")

    def _route(self) -> tuple[list[str], dict[str, list[str]]]:
        parsed = urlsplit(self.path)
        return [segment for segment in parsed.path.split("/") if segment], parse_qs(
            parsed.query,
            keep_blank_values=True,
        )

    @staticmethod
    def _one(query: dict[str, list[str]], name: str, default: str = "") -> str:
        values = query.get(name)
        return values[0] if values else default

    def _handle_error(self, error: Exception) -> None:
        if isinstance(error, PermissionError):
            self._json(
                HTTPStatus.UNAUTHORIZED,
                {"error": {"code": "unauthenticated", "message": str(error)}},
                extra_headers={"WWW-Authenticate": 'Bearer realm="atlas-gateway"'},
            )
        elif isinstance(error, LookupError):
            self._error(HTTPStatus.FORBIDDEN, "forbidden", str(error))
        elif isinstance(error, RuntimeError) and str(error) == "rate_limit":
            self._json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {"error": {"code": "rate_limited", "message": "Rate limit exceeded."}},
                extra_headers={"Retry-After": "60"},
            )
        elif isinstance(error, (ProposalConflictError, ExecutionConflictError)):
            self._error(HTTPStatus.CONFLICT, "conflict", str(error))
        elif isinstance(error, (AtlasError, TypeError, ValueError, json.JSONDecodeError)):
            self._error(HTTPStatus.BAD_REQUEST, "invalid_request", str(error))
        else:
            self._error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "internal_error",
                "Atlas Gateway could not complete the request.",
            )

    def do_GET(self) -> None:
        segments, query = self._route()
        if segments == ["v1", "health"]:
            self._json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "service": "atlas-core-gateway",
                    "version": "0.1.0",
                    "tls": self.server.tls_enabled,
                },
            )
            return
        try:
            grant = self._authenticate()
            if segments == ["v1", "admin", "backups"]:
                self._authorize(grant, "backup")
                items = self.server.client.backups()
                self._json(
                    HTTPStatus.OK,
                    {"items": [asdict(item) for item in items]},
                    extra_headers={"X-RateLimit-Remaining": str(self._rate_remaining)},
                )
                return
            if segments == ["v1", "admin", "automation"]:
                self._authorize(grant, "backup")
                automation = asdict(self.server.client.automation_status())
                automation["config_path"] = ""
                automation["state_directory"] = ""
                self._json(
                    HTTPStatus.OK,
                    automation,
                    extra_headers={"X-RateLimit-Remaining": str(self._rate_remaining)},
                )
                return
            if segments == ["v1", "admin", "providers"]:
                self._authorize(grant, "providers")
                items = self.server.client.provider_readiness()
                self._json(
                    HTTPStatus.OK,
                    {"items": [asdict(item) for item in items]},
                    extra_headers={
                        "X-RateLimit-Remaining": str(self._rate_remaining)
                    },
                )
                return
            if len(segments) != 4 or segments[:2] != ["v1", "tenants"]:
                self._error(HTTPStatus.NOT_FOUND, "not_found", "Route not found.")
                return
            tenant_id = self._tenant(segments[2])
            resource = segments[3]
            self._authorize(grant, "read", tenant_id)
            if resource == "overview":
                payload: Any = asdict(
                    self.server.client.overview(tenant_id=tenant_id)
                )
            elif resource == "answer":
                payload = asdict(
                    self.server.client.answer(
                        tenant_id=tenant_id,
                        query=self._one(query, "query"),
                        limit=int(self._one(query, "limit", "5")),
                    )
                )
            elif resource == "proposals":
                items = self.server.client.proposals(
                    tenant_id=tenant_id,
                    status=self._one(query, "status") or None,
                    limit=int(self._one(query, "limit", "100")),
                )
                payload = {"items": [asdict(item) for item in items]}
            elif resource == "executions":
                items = self.server.client.executions(
                    tenant_id=tenant_id,
                    status=self._one(query, "status") or None,
                    limit=int(self._one(query, "limit", "100")),
                )
                payload = {"items": [asdict(item) for item in items]}
            elif resource == "cartography":
                items = self.server.client.search_cartography(
                    tenant_id=tenant_id,
                    query=self._one(query, "query"),
                    lane=self._one(query, "lane") or None,
                    limit=int(self._one(query, "limit", "20")),
                )
                payload = {"items": [asdict(item) for item in items]}
            elif resource == "audit":
                items = self.server.client.audit_events(tenant_id=tenant_id)
                payload = {"items": [asdict(item) for item in items]}
            elif resource == "imports":
                items = self.server.client.imports(
                    tenant_id=tenant_id,
                    limit=int(self._one(query, "limit", "100")),
                )
                payload = {"items": [asdict(item) for item in items]}
            else:
                self._error(HTTPStatus.NOT_FOUND, "not_found", "Route not found.")
                return
            self._json(
                HTTPStatus.OK,
                payload,
                extra_headers={"X-RateLimit-Remaining": str(self._rate_remaining)},
            )
        except Exception as error:
            self._handle_error(error)

    def do_POST(self) -> None:
        try:
            grant = self._authenticate()
            segments, _ = self._route()
            if segments == ["v1", "admin", "backups", "import"]:
                self._authorize(grant, "backup")
                passphrase, artifact = self._read_recovery_upload()
                receipt = self.server.client.import_encrypted_backup(
                    artifact,
                    passphrase=passphrase,
                )
                self._json(HTTPStatus.CREATED, asdict(receipt))
                return
            payload = self._read_json()
            if segments == ["v1", "admin", "backups"]:
                self._authorize(grant, "backup")
                receipt = self.server.client.create_backup()
                self._json(HTTPStatus.CREATED, asdict(receipt))
                return
            if segments == ["v1", "admin", "backups", "retention"]:
                self._authorize(grant, "backup")
                dry_run = payload.get("dry_run", True)
                if not isinstance(dry_run, bool):
                    raise ValueError("dry_run must be a boolean.")
                receipt = self.server.client.prune_backups(
                    keep_last=int(payload.get("keep_last", 0)),
                    confirmation=str(payload.get("confirmation", "")),
                    dry_run=dry_run,
                )
                self._json(HTTPStatus.OK, asdict(receipt))
                return
            if (
                len(segments) == 5
                and segments[:3] == ["v1", "admin", "backups"]
                and segments[4] == "export"
            ):
                self._authorize(grant, "backup")
                backup_id = self._identifier(segments[3], "backup_id")
                artifact = self.server.client.export_encrypted_backup(
                    backup_id,
                    passphrase=str(payload.get("passphrase", "")),
                )
                self._binary(
                    HTTPStatus.OK,
                    artifact.data,
                    extra_headers={
                        "Content-Disposition": (
                            f'attachment; filename="{artifact.filename}"'
                        ),
                        "X-Atlas-Export-Id": artifact.export_id,
                        "X-Atlas-Backup-Id": artifact.backup_id,
                        "X-Atlas-Created-At": artifact.created_at,
                        "X-Atlas-Filename": artifact.filename,
                        "X-Atlas-SHA256": artifact.sha256,
                        "X-Atlas-Cipher": artifact.cipher,
                        "X-Atlas-KDF": artifact.kdf,
                    },
                )
                return
            if (
                len(segments) == 5
                and segments[:3] == ["v1", "admin", "backups"]
                and segments[4] == "restore"
            ):
                self._authorize(grant, "backup")
                backup_id = self._identifier(segments[3], "backup_id")
                receipt = self.server.client.restore_backup(
                    backup_id,
                    confirmation=str(payload.get("confirmation", "")),
                )
                self._json(HTTPStatus.OK, asdict(receipt))
                return
            if len(segments) < 4 or segments[:2] != ["v1", "tenants"]:
                self._error(HTTPStatus.NOT_FOUND, "not_found", "Route not found.")
                return
            tenant_id = self._tenant(segments[2])
            if len(segments) == 4 and segments[3] == "ingest":
                self._authorize(grant, "ingest", tenant_id)
                record = payload.get("record")
                if not isinstance(record, dict):
                    raise ValueError("record must be an object.")
                receipt: Any = self.server.client.ingest(
                    domain=str(payload.get("domain", "")),
                    tenant_id=tenant_id,
                    source_system=str(payload.get("source_system", "")),
                    record=record,
                )
                status = HTTPStatus.CREATED
            elif len(segments) == 4 and segments[3] == "imports":
                self._authorize(grant, "ingest", tenant_id)
                records = payload.get("records")
                if not isinstance(records, list) or not all(
                    isinstance(record, dict) for record in records
                ):
                    raise ValueError("records must be an array of objects.")
                receipt = self.server.client.import_batch(
                    tenant_id=tenant_id,
                    connector_id=str(payload.get("connector_id", "")),
                    domain=str(payload.get("domain", "")),
                    source_system=str(payload.get("source_system", "")),
                    external_batch_id=str(payload.get("external_batch_id", "")),
                    records=tuple(records),
                )
                status = HTTPStatus.CREATED
            elif (
                len(segments) == 6
                and segments[3] == "proposals"
                and segments[5] == "decision"
            ):
                self._authorize(grant, "approve", tenant_id)
                proposal_id = self._identifier(segments[4], "proposal_id")
                approved = payload.get("approved")
                if not isinstance(approved, bool):
                    raise ValueError("approved must be a boolean.")
                method = (
                    self.server.client.approve
                    if approved
                    else self.server.client.reject
                )
                receipt = method(
                    proposal_id,
                    reason=str(payload.get("reason", "")),
                    decided_by=str(payload.get("decided_by", "")),
                    tenant_id=tenant_id,
                )
                status = HTTPStatus.OK
            elif (
                len(segments) == 6
                and segments[3] == "proposals"
                and segments[5] == "execution"
            ):
                self._authorize(grant, "execute", tenant_id)
                proposal_id = self._identifier(segments[4], "proposal_id")
                receipt = self.server.client.queue_execution(
                    proposal_id,
                    tenant_id=tenant_id,
                    executor_id=str(payload.get("executor_id", "")),
                    requested_by=f"gateway:{grant.name}",
                    confirmation=str(payload.get("confirmation", "")),
                )
                status = HTTPStatus.CREATED
            elif (
                len(segments) == 6
                and segments[3] == "executions"
                and segments[5] in {"run", "rollback", "reconcile"}
            ):
                execution_id = self._identifier(segments[4], "execution_id")
                operation = segments[5]
                self._authorize(
                    grant,
                    "reconcile" if operation == "reconcile" else "execute",
                    tenant_id,
                )
                if operation == "run":
                    receipt = self.server.client.run_execution(
                        execution_id,
                        tenant_id=tenant_id,
                        confirmation=str(payload.get("confirmation", "")),
                    )
                elif operation == "rollback":
                    receipt = self.server.client.rollback_execution(
                        execution_id,
                        tenant_id=tenant_id,
                        confirmation=str(payload.get("confirmation", "")),
                    )
                else:
                    receipt = self.server.client.reconcile_execution(
                        execution_id,
                        tenant_id=tenant_id,
                        outcome=str(payload.get("outcome", "")),
                        note=str(payload.get("note", "")),
                        reconciled_by=f"gateway:{grant.name}",
                        external_reference=str(
                            payload.get("external_reference", "")
                        ),
                        confirmation=str(payload.get("confirmation", "")),
                    )
                status = HTTPStatus.OK
            else:
                self._error(HTTPStatus.NOT_FOUND, "not_found", "Route not found.")
                return
            self._json(
                status,
                asdict(receipt),
                extra_headers={"X-RateLimit-Remaining": str(self._rate_remaining)},
            )
        except Exception as error:
            self._handle_error(error)


def create_gateway(
    *,
    database: str | Path,
    adapters: tuple[IndustryAdapter, ...],
    grants: tuple[AccessGrant, ...],
    host: str = "127.0.0.1",
    port: int = 8792,
    allow_remote: bool = False,
    tls_certificate: str | Path | None = None,
    tls_private_key: str | Path | None = None,
    requests_per_minute: int = 120,
    automation_config: str | Path | None = None,
    executors: tuple[ActionExecutor, ...] = (),
) -> AtlasGatewayServer:
    loopback = host in LOOPBACK_HOSTS
    if not loopback and not allow_remote:
        raise ValueError("Non-loopback binding requires allow_remote=True.")
    if bool(tls_certificate) != bool(tls_private_key):
        raise ValueError("TLS certificate and private key must be provided together.")
    if not loopback and not tls_certificate:
        raise ValueError("Non-loopback Atlas Gateway binding requires TLS.")
    server = AtlasGatewayServer(
        (host, port),
        client=AtlasClient.local(
            database=database,
            adapters=adapters,
            executors=executors,
            automation_config=automation_config,
        ),
        grants=grants,
        requests_per_minute=requests_per_minute,
    )
    if tls_certificate and tls_private_key:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(
            certfile=str(tls_certificate),
            keyfile=str(tls_private_key),
        )
        server.socket = context.wrap_socket(server.socket, server_side=True)
        server.tls_enabled = True
    return server
