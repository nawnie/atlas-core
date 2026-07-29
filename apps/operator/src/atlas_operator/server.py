from __future__ import annotations

import json
import secrets
import struct
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from atlas_core.adapters import (
    ApartmentAdapter,
    ConstructionAdapter,
    HealthcareAdapter,
    HotelAdapter,
    ManufacturingAdapter,
)
from atlas_core.file_connectors import MAX_FILE_BYTES
from atlas_core.providers import HotelPmsSandbox
from atlas_core_sdk import AtlasClient, AtlasError

MAX_BODY_BYTES = 256 * 1024
MAX_RECOVERY_BYTES = 512 * 1024 * 1024
MAX_IMPORT_HEADER_BYTES = 16 * 1024
MAX_FILE_UPLOAD_BYTES = MAX_FILE_BYTES + MAX_IMPORT_HEADER_BYTES + 4
WEB_ROOT = Path(__file__).with_name("web")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


class AtlasOperatorServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        client: AtlasClient,
        hotel_sandbox: HotelPmsSandbox,
    ) -> None:
        super().__init__(server_address, AtlasOperatorHandler)
        self.client = client
        self.hotel_sandbox = hotel_sandbox
        self.provider_readiness = client.provider_readiness(
            certify_local_sandboxes=True
        )
        self.request_token = secrets.token_urlsafe(32)


class AtlasOperatorHandler(BaseHTTPRequestHandler):
    server: AtlasOperatorServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        print(f"[atlas-operator] {self.address_string()} {format % args}")

    def _headers(self, content_type: str, length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'",
        )

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, sort_keys=True, default=str).encode()
        self.send_response(status)
        self._headers("application/json; charset=utf-8", len(body))
        self.end_headers()
        self.wfile.write(body)

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

    def _error(self, status: int, code: str, message: str) -> None:
        self._json(status, {"error": {"code": code, "message": message}})

    def _serve_static(self, path: str) -> bool:
        entry = STATIC_FILES.get(path)
        if entry is None:
            return False
        name, content_type = entry
        body = (WEB_ROOT / name).read_bytes()
        self.send_response(HTTPStatus.OK)
        self._headers(content_type, len(body))
        self.end_headers()
        self.wfile.write(body)
        return True

    def _query(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urlsplit(self.path)
        return parsed.path, parse_qs(parsed.query, keep_blank_values=True)

    @staticmethod
    def _one(query: dict[str, list[str]], name: str, default: str = "") -> str:
        values = query.get(name)
        return values[0] if values else default

    def _tenant(self, query: dict[str, list[str]]) -> str:
        tenant_id = self._one(query, "tenant_id").strip()
        if not tenant_id or len(tenant_id) > 100:
            raise ValueError("tenant_id is required and must be at most 100 characters.")
        return tenant_id

    def _read_json(self) -> dict[str, Any]:
        if self.headers.get_content_type() != "application/json":
            raise TypeError("Content-Type must be application/json.")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("Content-Length is required.")
        length = int(raw_length)
        if not 0 < length <= MAX_BODY_BYTES:
            raise ValueError(f"JSON body must be between 1 and {MAX_BODY_BYTES} bytes.")
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object.")
        return payload

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        port = self.server.server_address[1]
        return origin in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    def _authorize_mutation(self, payload: dict[str, Any]) -> str:
        if not self._same_origin():
            raise PermissionError("Cross-origin mutations are not allowed.")
        supplied = self.headers.get("X-Atlas-Request-Token", "")
        if not secrets.compare_digest(supplied, self.server.request_token):
            raise PermissionError("Invalid request token.")
        tenant_id = str(payload.get("tenant_id", "")).strip()
        header_tenant = self.headers.get("X-Atlas-Tenant", "").strip()
        if not tenant_id or tenant_id != header_tenant:
            raise PermissionError("Tenant header must match the request body.")
        if len(tenant_id) > 100:
            raise ValueError("tenant_id must be at most 100 characters.")
        return tenant_id

    def _authorize_binary_mutation(self) -> str:
        if not self._same_origin():
            raise PermissionError("Cross-origin mutations are not allowed.")
        supplied = self.headers.get("X-Atlas-Request-Token", "")
        if not secrets.compare_digest(supplied, self.server.request_token):
            raise PermissionError("Invalid request token.")
        tenant_id = self.headers.get("X-Atlas-Tenant", "").strip()
        if not tenant_id or len(tenant_id) > 100:
            raise PermissionError("A valid tenant header is required.")
        return tenant_id

    def _read_recovery_upload(self) -> tuple[str, bytes]:
        if (
            self.headers.get_content_type()
            != "application/vnd.atlas.encrypted-backup"
        ):
            raise TypeError(
                "Content-Type must be application/vnd.atlas.encrypted-backup."
            )
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("Content-Length is required.")
        length = int(raw_length)
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

    def _read_file_upload(self) -> tuple[dict[str, Any], bytes]:
        if self.headers.get_content_type() != "application/vnd.atlas.import-file":
            raise TypeError(
                "Content-Type must be application/vnd.atlas.import-file."
            )
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("Content-Length is required.")
        length = int(raw_length)
        if not 5 <= length <= MAX_FILE_UPLOAD_BYTES:
            raise ValueError(
                f"File import upload may not exceed {MAX_FILE_UPLOAD_BYTES} bytes."
            )
        envelope = self.rfile.read(length)
        header_length = struct.unpack(">I", envelope[:4])[0]
        if not 2 <= header_length <= MAX_IMPORT_HEADER_BYTES:
            raise ValueError("File import envelope header is invalid.")
        content_start = 4 + header_length
        if content_start >= len(envelope):
            raise ValueError("File import envelope is truncated.")
        try:
            metadata = json.loads(envelope[4:content_start])
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("File import metadata is invalid JSON.") from error
        if not isinstance(metadata, dict):
            raise ValueError("File import metadata must be an object.")
        return metadata, envelope[content_start:]

    def do_GET(self) -> None:
        path, query = self._query()
        if self._serve_static(path):
            return
        try:
            if path == "/api/health":
                self._json(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "service": "atlas-core-operator",
                        "version": "0.1.0",
                        "binding": "localhost-only",
                    },
                )
            elif path == "/api/bootstrap":
                self._json(
                    HTTPStatus.OK,
                    {
                        "request_token": self.server.request_token,
                        "default_tenant": "demo-hotel",
                        "capabilities": [
                            "company-memory",
                            "universal-translator",
                            "cartographer",
                            "change-intelligence",
                            "cited-intelligence",
                            "approval-engine",
                            "controlled-sandbox-execution",
                            "provider-certification",
                            "replayable-imports",
                            "verified-backups",
                            "local-automation-receipts",
                        ],
                        "domains": [
                            "hotel",
                            "apartment",
                            "construction",
                            "manufacturing",
                            "healthcare",
                        ],
                    },
                )
            elif path == "/api/hotel-sandbox/reservations":
                tenant_id = self._tenant(query)
                reservations = self.server.hotel_sandbox.reservations(
                    tenant_id=tenant_id
                )
                self._json(
                    HTTPStatus.OK,
                    {
                        "sandbox": True,
                        "executor_id": self.server.hotel_sandbox.executor_id,
                        "items": [asdict(item) for item in reservations],
                    },
                )
            elif path == "/api/providers":
                self._json(
                    HTTPStatus.OK,
                    {
                        "items": [
                            asdict(receipt)
                            for receipt in self.server.provider_readiness
                        ]
                    },
                )
            elif path == "/api/overview":
                tenant_id = self._tenant(query)
                self._json(HTTPStatus.OK, asdict(self.server.client.overview(tenant_id=tenant_id)))
            elif path == "/api/answer":
                tenant_id = self._tenant(query)
                answer = self.server.client.answer(
                    tenant_id=tenant_id,
                    query=self._one(query, "query"),
                    limit=int(self._one(query, "limit", "5")),
                )
                self._json(HTTPStatus.OK, asdict(answer))
            elif path == "/api/proposals":
                tenant_id = self._tenant(query)
                status = self._one(query, "status") or None
                limit = int(self._one(query, "limit", "100"))
                items = self.server.client.proposals(
                    tenant_id=tenant_id,
                    status=status,
                    limit=limit,
                )
                self._json(HTTPStatus.OK, {"items": [asdict(item) for item in items]})
            elif path == "/api/executions":
                tenant_id = self._tenant(query)
                items = self.server.client.executions(
                    tenant_id=tenant_id,
                    status=self._one(query, "status") or None,
                    limit=int(self._one(query, "limit", "100")),
                )
                self._json(
                    HTTPStatus.OK,
                    {"items": [asdict(item) for item in items]},
                )
            elif path == "/api/cartography":
                tenant_id = self._tenant(query)
                cards = self.server.client.search_cartography(
                    tenant_id=tenant_id,
                    query=self._one(query, "query"),
                    lane=self._one(query, "lane") or None,
                    limit=int(self._one(query, "limit", "50")),
                )
                self._json(HTTPStatus.OK, {"items": [asdict(card) for card in cards]})
            elif path == "/api/audit":
                tenant_id = self._tenant(query)
                limit = min(max(int(self._one(query, "limit", "100")), 1), 500)
                events = self.server.client.audit_events(tenant_id=tenant_id)
                self._json(
                    HTTPStatus.OK,
                    {"items": [asdict(event) for event in events[-limit:]][::-1]},
                )
            elif path == "/api/imports":
                tenant_id = self._tenant(query)
                receipts = self.server.client.imports(
                    tenant_id=tenant_id,
                    limit=int(self._one(query, "limit", "100")),
                )
                self._json(
                    HTTPStatus.OK,
                    {"items": [asdict(receipt) for receipt in receipts]},
                )
            elif path == "/api/backups":
                receipts = self.server.client.backups()
                self._json(
                    HTTPStatus.OK,
                    {"items": [asdict(receipt) for receipt in receipts]},
                )
            elif path == "/api/automation":
                status = self.server.client.automation_status()
                self._json(HTTPStatus.OK, asdict(status))
            else:
                self._error(HTTPStatus.NOT_FOUND, "not_found", "Route not found.")
        except (ValueError, AtlasError) as error:
            self._error(HTTPStatus.BAD_REQUEST, "invalid_request", str(error))
        except Exception:
            self._error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "internal_error",
                "The local operator service could not complete the request.",
            )

    def do_POST(self) -> None:
        path, _ = self._query()
        try:
            if path in {"/api/file-imports/preview", "/api/file-imports/apply"}:
                tenant_id = self._authorize_binary_mutation()
                metadata, content = self._read_file_upload()
                preview = self.server.client.preview_file_import(
                    content,
                    format=str(metadata.get("format", "")),
                    source_name=str(metadata.get("source_name", "")),
                )
                if path.endswith("/preview"):
                    self._json(
                        HTTPStatus.OK,
                        {
                            "format": preview.format,
                            "source_name": preview.source_name,
                            "sha256": preview.sha256,
                            "size_bytes": preview.size_bytes,
                            "record_count": preview.record_count,
                            "fields": preview.fields,
                            "records": [
                                {
                                    "record_id": record.record_id,
                                    "version": record.version,
                                }
                                for record in preview.records[:20]
                            ],
                            "records_truncated": preview.record_count > 20,
                            "external_batch_id": preview.external_batch_id,
                        },
                    )
                else:
                    receipt = self.server.client.import_file(
                        content,
                        format=preview.format,
                        source_name=preview.source_name,
                        tenant_id=tenant_id,
                        connector_id=str(metadata.get("connector_id", "")),
                        domain=str(metadata.get("domain", "")),
                        source_system=str(metadata.get("source_system", "")),
                        external_batch_id=(
                            str(metadata["external_batch_id"])
                            if metadata.get("external_batch_id")
                            else None
                        ),
                    )
                    self._json(HTTPStatus.CREATED, asdict(receipt))
                return
            if path == "/api/backups/import":
                self._authorize_binary_mutation()
                passphrase, artifact = self._read_recovery_upload()
                receipt = self.server.client.import_encrypted_backup(
                    artifact,
                    passphrase=passphrase,
                )
                self._json(HTTPStatus.CREATED, asdict(receipt))
                return
            payload = self._read_json()
            tenant_id = self._authorize_mutation(payload)
            if path == "/api/ingest":
                record = payload.get("record")
                if not isinstance(record, dict):
                    raise ValueError("record must be an object.")
                result = self.server.client.ingest(
                    domain=str(payload.get("domain", "")),
                    tenant_id=tenant_id,
                    source_system=str(payload.get("source_system", "")),
                    record=record,
                )
                self._json(HTTPStatus.CREATED, asdict(result))
            elif path == "/api/demo/seed":
                self._json(HTTPStatus.CREATED, self._seed_demo(tenant_id))
            elif path == "/api/demo/apartment":
                self._json(HTTPStatus.CREATED, self._seed_apartment(tenant_id))
            elif path == "/api/demo/construction":
                self._json(HTTPStatus.CREATED, self._seed_construction(tenant_id))
            elif path == "/api/demo/manufacturing":
                self._json(HTTPStatus.CREATED, self._seed_manufacturing(tenant_id))
            elif path == "/api/demo/healthcare":
                self._json(HTTPStatus.CREATED, self._seed_healthcare(tenant_id))
            elif path == "/api/imports":
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
                self._json(HTTPStatus.CREATED, asdict(receipt))
            elif path == "/api/backups":
                receipt = self.server.client.create_backup()
                self._json(HTTPStatus.CREATED, asdict(receipt))
            elif path == "/api/backups/retention":
                dry_run = payload.get("dry_run", True)
                if not isinstance(dry_run, bool):
                    raise ValueError("dry_run must be a boolean.")
                receipt = self.server.client.prune_backups(
                    keep_last=int(payload.get("keep_last", 0)),
                    confirmation=str(payload.get("confirmation", "")),
                    dry_run=dry_run,
                )
                self._json(HTTPStatus.OK, asdict(receipt))
            elif path.startswith("/api/backups/") and path.endswith("/export"):
                backup_id = path.removeprefix("/api/backups/").removesuffix(
                    "/export"
                )
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
                        "X-Atlas-SHA256": artifact.sha256,
                    },
                )
            elif path.startswith("/api/backups/") and path.endswith("/restore"):
                backup_id = path.removeprefix("/api/backups/").removesuffix(
                    "/restore"
                )
                receipt = self.server.client.restore_backup(
                    backup_id,
                    confirmation=str(payload.get("confirmation", "")),
                )
                self._json(HTTPStatus.OK, asdict(receipt))
            elif path.startswith("/api/proposals/") and path.endswith("/decision"):
                proposal_id = path.removeprefix("/api/proposals/").removesuffix(
                    "/decision"
                )
                approved = payload.get("approved")
                if not isinstance(approved, bool):
                    raise ValueError("approved must be a boolean.")
                method = self.server.client.approve if approved else self.server.client.reject
                receipt = method(
                    proposal_id,
                    reason=str(payload.get("reason", "")),
                    decided_by=str(payload.get("decided_by", "")),
                    tenant_id=tenant_id,
                )
                self._json(HTTPStatus.OK, asdict(receipt))
            elif (
                path.startswith("/api/proposals/")
                and path.endswith("/sandbox-execution")
            ):
                proposal_id = path.removeprefix("/api/proposals/").removesuffix(
                    "/sandbox-execution"
                )
                receipt = self.server.client.queue_execution(
                    proposal_id,
                    tenant_id=tenant_id,
                    executor_id=self.server.hotel_sandbox.executor_id,
                    requested_by="local-sandbox-operator",
                    confirmation=str(payload.get("confirmation", "")),
                )
                self._json(HTTPStatus.CREATED, asdict(receipt))
            elif path.startswith("/api/executions/") and path.endswith("/run"):
                execution_id = path.removeprefix("/api/executions/").removesuffix(
                    "/run"
                )
                self._require_sandbox_execution(execution_id, tenant_id)
                receipt = self.server.client.run_execution(
                    execution_id,
                    tenant_id=tenant_id,
                    confirmation=str(payload.get("confirmation", "")),
                )
                self._json(HTTPStatus.OK, asdict(receipt))
            elif path.startswith("/api/executions/") and path.endswith("/rollback"):
                execution_id = path.removeprefix("/api/executions/").removesuffix(
                    "/rollback"
                )
                self._require_sandbox_execution(execution_id, tenant_id)
                receipt = self.server.client.rollback_execution(
                    execution_id,
                    tenant_id=tenant_id,
                    confirmation=str(payload.get("confirmation", "")),
                )
                self._json(HTTPStatus.OK, asdict(receipt))
            else:
                self._error(HTTPStatus.NOT_FOUND, "not_found", "Route not found.")
        except PermissionError as error:
            self._error(HTTPStatus.FORBIDDEN, "forbidden", str(error))
        except KeyError as error:
            self._error(HTTPStatus.NOT_FOUND, "not_found", str(error))
        except (TypeError, ValueError, json.JSONDecodeError, AtlasError) as error:
            self._error(HTTPStatus.BAD_REQUEST, "invalid_request", str(error))
        except Exception:
            self._error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "internal_error",
                "The local operator service could not complete the request.",
            )

    def _require_sandbox_execution(
        self, execution_id: str, tenant_id: str
    ) -> None:
        receipt = next(
            (
                item
                for item in self.server.client.executions(
                    tenant_id=tenant_id, limit=500
                )
                if item.execution_id == execution_id
            ),
            None,
        )
        if receipt is None:
            raise KeyError(f"Unknown execution: {execution_id}")
        if receipt.executor_id != self.server.hotel_sandbox.executor_id:
            raise PermissionError(
                "The local operator may run only its built-in Hotel/PMS sandbox."
            )

    def _seed_demo(self, tenant_id: str) -> dict[str, Any]:
        base, cancelled = self.server.hotel_sandbox.seed_cancelled_reservation(
            tenant_id=tenant_id,
        )
        self.server.client.ingest(
            domain="hotel",
            tenant_id=tenant_id,
            source_system="hotel-pms-sandbox",
            record=base,
        )
        result = self.server.client.ingest(
            domain="hotel",
            tenant_id=tenant_id,
            source_system="hotel-pms-sandbox",
            record=cancelled,
        )
        return {
            "message": "Hotel/PMS sandbox cancellation loaded.",
            "result": asdict(result),
        }

    def _seed_apartment(self, tenant_id: str) -> dict[str, Any]:
        base = {
            "lease_id": "LEASE-310",
            "updated_at": "2026-07-24T13:00:00Z",
            "unit_id": "B-204",
            "resident_count": 2,
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
            "rent": "1850.00",
            "balance_due": "0.00",
            "status": "active",
            "currency": "USD",
        }
        receipt = self.server.client.import_batch(
            tenant_id=tenant_id,
            connector_id="synthetic-property-feed",
            domain="apartment",
            source_system="synthetic-property-manager",
            external_batch_id="apartment-demo-20260724",
            records=(
                {"record_id": "LEASE-310", "version": "1", "payload": base},
                {
                    "record_id": "LEASE-310",
                    "version": "2",
                    "payload": {
                        **base,
                        "updated_at": "2026-07-24T13:05:00Z",
                        "balance_due": "925.00",
                        "status": "delinquent",
                    },
                },
            ),
        )
        return {
            "message": (
                "Synthetic apartment import replayed."
                if receipt.replayed
                else "Synthetic apartment import completed."
            ),
            "receipt": asdict(receipt),
        }

    def _seed_construction(self, tenant_id: str) -> dict[str, Any]:
        base = {
            "work_item_id": "SITE-42-ELECTRICAL",
            "updated_at": "2026-07-24T14:00:00Z",
            "project_id": "SITE-42",
            "trade": "electrical",
            "status": "in_progress",
            "percent_complete": 55,
            "budget": "42000.00",
            "actual_cost": "23800.00",
            "safety_hold": False,
            "currency": "USD",
        }
        return self._import_demo(
            tenant_id=tenant_id,
            connector_id="synthetic-construction-feed",
            domain="construction",
            source_system="synthetic-project-system",
            external_batch_id="construction-demo-20260724",
            record_id="SITE-42-ELECTRICAL",
            base=base,
            changed={
                "updated_at": "2026-07-24T14:05:00Z",
                "status": "on_hold",
                "budget": "42500.00",
                "actual_cost": "44150.00",
                "safety_hold": True,
            },
            label="construction",
        )

    def _seed_manufacturing(self, tenant_id: str) -> dict[str, Any]:
        base = {
            "work_order_id": "WO-8802",
            "updated_at": "2026-07-24T15:00:00Z",
            "line_id": "LINE-3",
            "sku": "AES-BRACKET-7",
            "status": "running",
            "quantity_planned": 500,
            "quantity_completed": 240,
            "defect_count": 2,
            "downtime_minutes": 0,
            "quality_hold": False,
        }
        return self._import_demo(
            tenant_id=tenant_id,
            connector_id="synthetic-manufacturing-feed",
            domain="manufacturing",
            source_system="synthetic-mes",
            external_batch_id="manufacturing-demo-20260724",
            record_id="WO-8802",
            base=base,
            changed={
                "updated_at": "2026-07-24T15:05:00Z",
                "status": "quality_hold",
                "quantity_completed": 252,
                "defect_count": 11,
                "downtime_minutes": 18,
                "quality_hold": True,
            },
            label="manufacturing",
        )

    def _seed_healthcare(self, tenant_id: str) -> dict[str, Any]:
        base = {
            "encounter_id": "ENC-OPAQUE-901",
            "updated_at": "2026-07-24T16:00:00Z",
            "patient_ref": "PAT-OPAQUE-77",
            "care_setting": "outpatient",
            "status": "scheduled",
            "authorization_status": "approved",
            "documentation_complete": True,
            "service_code": "SYNTH-100",
        }
        return self._import_demo(
            tenant_id=tenant_id,
            connector_id="synthetic-healthcare-feed",
            domain="healthcare",
            source_system="synthetic-practice-system",
            external_batch_id="healthcare-demo-20260724",
            record_id="ENC-OPAQUE-901",
            base=base,
            changed={
                "updated_at": "2026-07-24T16:05:00Z",
                "authorization_status": "more_information_required",
                "documentation_complete": False,
            },
            label="healthcare administrative",
        )

    def _import_demo(
        self,
        *,
        tenant_id: str,
        connector_id: str,
        domain: str,
        source_system: str,
        external_batch_id: str,
        record_id: str,
        base: dict[str, Any],
        changed: dict[str, Any],
        label: str,
    ) -> dict[str, Any]:
        receipt = self.server.client.import_batch(
            tenant_id=tenant_id,
            connector_id=connector_id,
            domain=domain,
            source_system=source_system,
            external_batch_id=external_batch_id,
            records=(
                {"record_id": record_id, "version": "1", "payload": base},
                {
                    "record_id": record_id,
                    "version": "2",
                    "payload": {**base, **changed},
                },
            ),
        )
        return {
            "message": (
                f"Synthetic {label} import replayed."
                if receipt.replayed
                else f"Synthetic {label} import completed."
            ),
            "receipt": asdict(receipt),
        }


def create_server(
    *,
    database: str | Path,
    host: str = "127.0.0.1",
    port: int = 8791,
    automation_config: str | Path | None = None,
) -> AtlasOperatorServer:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("Atlas Operator may bind only to localhost.")
    database = Path(database).expanduser().resolve()
    hotel_sandbox = HotelPmsSandbox(
        database.with_name(f"{database.stem}.hotel-sandbox.db")
    )
    client = AtlasClient.local(
        database=database,
        automation_config=automation_config,
        executors=(hotel_sandbox,),
        adapters=(
            HotelAdapter(),
            ApartmentAdapter(),
            ConstructionAdapter(),
            ManufacturingAdapter(),
            HealthcareAdapter(),
        ),
    )
    return AtlasOperatorServer(
        (host, port),
        client,
        hotel_sandbox,
    )
