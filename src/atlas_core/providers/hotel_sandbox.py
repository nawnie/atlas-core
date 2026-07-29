from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..models import (
    ActionExecutionReceipt,
    ActionExecutionRequest,
    ActionExecutionResult,
    utc_now,
)
from ..provider_certification import (
    CredentialStrategy,
    ProviderEnvironment,
    ProviderManifest,
)


@dataclass(frozen=True)
class HotelSandboxReservation:
    tenant_id: str
    reservation_id: str
    version: str
    status: str
    cancellation_review_status: str
    inventory_status: str
    updated_at: str


class HotelPmsSandbox:
    """Persistent local PMS simulator for synthetic Hotel workflows only.

    This adapter never performs network I/O and never accepts provider
    credentials. It exists to validate the complete Atlas execution contract
    before a separately reviewed live-provider adapter is authorized.
    """

    executor_id = "hotel-pms-sandbox"
    action_types = frozenset({"review_cancelled_reservation"})
    supports_rollback = True
    is_sandbox = True
    manifest = ProviderManifest(
        provider_id=executor_id,
        display_name="Atlas Hotel/PMS Sandbox",
        domain="hotel",
        environment=ProviderEnvironment.LOCAL_SANDBOX,
        action_types=tuple(sorted(action_types)),
        supports_rollback=supports_rollback,
        network_access=False,
        credential_strategy=CredentialStrategy.NONE,
        synthetic_only=True,
    )

    def __init__(self, database: str | Path) -> None:
        self.database = Path(database).expanduser().resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=30)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA busy_timeout = 30000")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS hotel_sandbox_reservations (
                    tenant_id TEXT NOT NULL,
                    reservation_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    cancellation_review_status TEXT NOT NULL,
                    inventory_status TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, reservation_id)
                );
                CREATE TABLE IF NOT EXISTS hotel_sandbox_actions (
                    idempotency_key TEXT PRIMARY KEY,
                    execution_id TEXT NOT NULL UNIQUE,
                    tenant_id TEXT NOT NULL,
                    reservation_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    external_reference TEXT NOT NULL,
                    status TEXT NOT NULL,
                    previous_review_status TEXT NOT NULL,
                    previous_inventory_status TEXT NOT NULL,
                    executed_at TEXT NOT NULL,
                    rolled_back_at TEXT NOT NULL DEFAULT ''
                );
                """
            )

    @staticmethod
    def _clean_identifier(value: str, name: str) -> str:
        cleaned = value.strip()
        if not cleaned or len(cleaned) > 100:
            raise ValueError(f"{name} must be 1-100 characters.")
        return cleaned

    def seed_cancelled_reservation(
        self,
        *,
        tenant_id: str,
        reservation_id: str = "DEMO-2048",
    ) -> tuple[dict[str, object], dict[str, object]]:
        """Create a deterministic two-version synthetic reservation."""

        tenant_id = self._clean_identifier(tenant_id, "tenant_id")
        reservation_id = self._clean_identifier(
            reservation_id, "reservation_id"
        )
        base: dict[str, object] = {
            "reservation_id": reservation_id,
            "version": "1",
            "updated_at": "2026-07-24T12:00:00Z",
            "arrival_date": "2026-08-10",
            "departure_date": "2026-08-13",
            "guest_count": 2,
            "room_type": "king",
            "status": "confirmed",
            "total": "729.00",
            "currency": "USD",
        }
        cancelled = {
            **base,
            "version": "2",
            "updated_at": "2026-07-24T12:05:00Z",
            "status": "cancelled",
            "total": "0.00",
        }
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                INSERT INTO hotel_sandbox_reservations (
                    tenant_id, reservation_id, version, status,
                    cancellation_review_status, inventory_status, updated_at
                ) VALUES (?, ?, '2', 'cancelled', 'pending', 'held', ?)
                ON CONFLICT(tenant_id, reservation_id) DO UPDATE SET
                    version = excluded.version,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (tenant_id, reservation_id, str(cancelled["updated_at"])),
            )
        return base, cancelled

    def reservations(
        self, *, tenant_id: str
    ) -> tuple[HotelSandboxReservation, ...]:
        tenant_id = self._clean_identifier(tenant_id, "tenant_id")
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT * FROM hotel_sandbox_reservations
                WHERE tenant_id = ?
                ORDER BY reservation_id
                """,
                (tenant_id,),
            ).fetchall()
        return tuple(
            HotelSandboxReservation(
                tenant_id=row["tenant_id"],
                reservation_id=row["reservation_id"],
                version=row["version"],
                status=row["status"],
                cancellation_review_status=row["cancellation_review_status"],
                inventory_status=row["inventory_status"],
                updated_at=row["updated_at"],
            )
            for row in rows
        )

    @staticmethod
    def _reference(idempotency_key: str) -> str:
        digest = hashlib.sha256(idempotency_key.encode()).hexdigest()[:20]
        return f"hotel-sandbox:{digest}"

    @staticmethod
    def _validate_request(request: ActionExecutionRequest) -> str:
        if request.domain != "hotel":
            raise ValueError("Hotel sandbox accepts only the hotel domain.")
        if request.action_type != "review_cancelled_reservation":
            raise ValueError("Hotel sandbox action type is not allowed.")
        reservation_id = str(request.payload.get("reservation_id", "")).strip()
        if not reservation_id or reservation_id != request.entity_id:
            raise ValueError(
                "Hotel sandbox reservation identity does not match the proposal."
            )
        return reservation_id

    def execute(self, request: ActionExecutionRequest) -> ActionExecutionResult:
        reservation_id = self._validate_request(request)
        reference = self._reference(request.idempotency_key)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                """
                SELECT * FROM hotel_sandbox_actions
                WHERE idempotency_key = ?
                """,
                (request.idempotency_key,),
            ).fetchone()
            if prior is not None:
                if (
                    prior["execution_id"] != request.execution_id
                    or prior["tenant_id"] != request.tenant_id
                    or prior["reservation_id"] != reservation_id
                ):
                    raise ValueError("Hotel sandbox idempotency collision.")
                if prior["status"] == "rolled_back":
                    return ActionExecutionResult(
                        succeeded=False,
                        summary="Sandbox action was already rolled back.",
                        external_reference=prior["external_reference"],
                        error="A rolled-back action cannot be executed again.",
                    )
                return ActionExecutionResult(
                    succeeded=True,
                    summary="Sandbox cancellation review was already completed.",
                    external_reference=prior["external_reference"],
                )
            reservation = db.execute(
                """
                SELECT * FROM hotel_sandbox_reservations
                WHERE tenant_id = ? AND reservation_id = ?
                """,
                (request.tenant_id, reservation_id),
            ).fetchone()
            if reservation is None:
                return ActionExecutionResult(
                    succeeded=False,
                    summary="Sandbox reservation was not found.",
                    error="The reservation is not present in this tenant sandbox.",
                )
            if reservation["status"] != "cancelled":
                return ActionExecutionResult(
                    succeeded=False,
                    summary="Sandbox reservation is not cancelled.",
                    error="Only a cancelled reservation can complete this review.",
                )
            db.execute(
                """
                UPDATE hotel_sandbox_reservations
                SET cancellation_review_status = 'completed',
                    inventory_status = 'released',
                    updated_at = ?
                WHERE tenant_id = ? AND reservation_id = ?
                """,
                (utc_now(), request.tenant_id, reservation_id),
            )
            db.execute(
                """
                INSERT INTO hotel_sandbox_actions (
                    idempotency_key, execution_id, tenant_id, reservation_id,
                    action_type, external_reference, status,
                    previous_review_status, previous_inventory_status,
                    executed_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?)
                """,
                (
                    request.idempotency_key,
                    request.execution_id,
                    request.tenant_id,
                    reservation_id,
                    request.action_type,
                    reference,
                    reservation["cancellation_review_status"],
                    reservation["inventory_status"],
                    utc_now(),
                ),
            )
        return ActionExecutionResult(
            succeeded=True,
            summary=(
                "Sandbox cancellation review completed and room inventory released."
            ),
            external_reference=reference,
        )

    def rollback(
        self,
        request: ActionExecutionRequest,
        receipt: ActionExecutionReceipt,
    ) -> ActionExecutionResult:
        reservation_id = self._validate_request(request)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            action = db.execute(
                """
                SELECT * FROM hotel_sandbox_actions
                WHERE idempotency_key = ? AND execution_id = ?
                """,
                (request.idempotency_key, request.execution_id),
            ).fetchone()
            if action is None:
                return ActionExecutionResult(
                    succeeded=False,
                    summary="Sandbox action receipt was not found.",
                    error="Rollback requires the original sandbox action receipt.",
                )
            if action["tenant_id"] != request.tenant_id:
                raise ValueError("Hotel sandbox rollback tenant mismatch.")
            if action["status"] == "rolled_back":
                return ActionExecutionResult(
                    succeeded=True,
                    summary="Sandbox cancellation review was already rolled back.",
                    external_reference=action["external_reference"],
                )
            db.execute(
                """
                UPDATE hotel_sandbox_reservations
                SET cancellation_review_status = ?,
                    inventory_status = ?,
                    updated_at = ?
                WHERE tenant_id = ? AND reservation_id = ?
                """,
                (
                    action["previous_review_status"],
                    action["previous_inventory_status"],
                    utc_now(),
                    request.tenant_id,
                    reservation_id,
                ),
            )
            db.execute(
                """
                UPDATE hotel_sandbox_actions
                SET status = 'rolled_back', rolled_back_at = ?
                WHERE idempotency_key = ?
                """,
                (utc_now(), request.idempotency_key),
            )
        return ActionExecutionResult(
            succeeded=True,
            summary=(
                "Sandbox cancellation review was reversed and inventory restored."
            ),
            external_reference=receipt.external_reference,
        )

    def create_conformance_request(
        self, certification_id: str
    ) -> ActionExecutionRequest:
        suffix = hashlib.sha256(certification_id.encode()).hexdigest()[:12]
        tenant_id = f"atlas-cert-{suffix}"
        reservation_id = f"CERT-{suffix}"
        self.seed_cancelled_reservation(
            tenant_id=tenant_id,
            reservation_id=reservation_id,
        )
        return ActionExecutionRequest(
            execution_id=f"cert-exe-{suffix}",
            proposal_id=f"cert-proposal-{suffix}",
            tenant_id=tenant_id,
            domain="hotel",
            action_type="review_cancelled_reservation",
            entity_type="reservation",
            entity_id=reservation_id,
            payload={
                "reservation_id": reservation_id,
                "review_tasks": [
                    "confirm synthetic cancellation source",
                    "release synthetic room inventory",
                ],
            },
            idempotency_key=f"cert-idem-{suffix}",
            attempt=1,
        )

    def inspect_conformance_state(
        self, request: ActionExecutionRequest
    ) -> dict[str, str]:
        reservations = self.reservations(tenant_id=request.tenant_id)
        matching = next(
            (
                reservation
                for reservation in reservations
                if reservation.reservation_id == request.entity_id
            ),
            None,
        )
        if matching is None:
            return {}
        return {
            "status": matching.status,
            "cancellation_review_status": matching.cancellation_review_status,
            "inventory_status": matching.inventory_status,
        }

    def cleanup_conformance(self, request: ActionExecutionRequest) -> None:
        if not request.tenant_id.startswith("atlas-cert-"):
            raise ValueError("Refusing to clean a non-certification tenant.")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                DELETE FROM hotel_sandbox_actions
                WHERE tenant_id = ? AND execution_id = ?
                """,
                (request.tenant_id, request.execution_id),
            )
            db.execute(
                """
                DELETE FROM hotel_sandbox_reservations
                WHERE tenant_id = ? AND reservation_id = ?
                """,
                (request.tenant_id, request.entity_id),
            )
