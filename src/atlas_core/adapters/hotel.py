from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from ..models import ActionProposal, CanonicalObservation, ChangeSet, RiskLevel
from ..translator import IndustryAdapter


def _required(record: Mapping[str, Any], name: str) -> Any:
    value = record.get(name)
    if value is None or value == "":
        raise ValueError(f"Missing required hotel field: {name}")
    return value


class HotelAdapter(IndustryAdapter):
    domain = "hotel"

    def mapping_manifest(self) -> Mapping[str, str]:
        return {
            "reservation_id": "entity_id",
            "updated_at": "observed_at",
            "arrival_date": "attributes.arrival_date",
            "departure_date": "attributes.departure_date",
            "guest_count": "attributes.guest_count",
            "room_type": "attributes.room_type",
            "status": "attributes.status",
            "total": "attributes.total",
            "currency": "attributes.currency",
        }

    def translate(
        self,
        record: Mapping[str, Any],
        *,
        tenant_id: str,
        source_system: str,
    ) -> CanonicalObservation:
        reservation_id = str(_required(record, "reservation_id"))
        try:
            total = str(Decimal(str(_required(record, "total"))).quantize(Decimal("0.01")))
        except InvalidOperation as error:
            raise ValueError("Hotel total must be numeric.") from error
        return CanonicalObservation(
            tenant_id=tenant_id,
            domain=self.domain,
            entity_type="reservation",
            entity_id=reservation_id,
            source_system=source_system,
            source_record_id=reservation_id,
            source_version=str(record.get("version")) if record.get("version") else None,
            observed_at=str(_required(record, "updated_at")),
            attributes={
                "arrival_date": str(_required(record, "arrival_date")),
                "departure_date": str(_required(record, "departure_date")),
                "guest_count": int(_required(record, "guest_count")),
                "room_type": str(_required(record, "room_type")),
                "status": str(_required(record, "status")).lower(),
                "total": total,
                "currency": str(record.get("currency", "USD")).upper(),
            },
        )

    def propose_actions(self, changes: ChangeSet) -> tuple[ActionProposal, ...]:
        by_field = {change.field: change for change in changes.changes}
        status = by_field.get("status")
        if status and status.after == "cancelled":
            return (
                ActionProposal(
                    tenant_id=changes.tenant_id,
                    domain=self.domain,
                    action_type="review_cancelled_reservation",
                    entity_type=changes.entity_type,
                    entity_id=changes.entity_id,
                    reason="Reservation status changed to cancelled.",
                    payload={
                        "reservation_id": changes.entity_id,
                        "review_tasks": [
                            "confirm cancellation source",
                            "review refund terms",
                            "release room inventory",
                        ],
                    },
                    risk=RiskLevel.MEDIUM,
                ),
            )
        return ()
