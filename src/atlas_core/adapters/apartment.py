from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from ..models import ActionProposal, CanonicalObservation, ChangeSet, RiskLevel
from ..translator import IndustryAdapter


def _required(record: Mapping[str, Any], name: str) -> Any:
    value = record.get(name)
    if value is None or value == "":
        raise ValueError(f"Missing required apartment field: {name}")
    return value


def _money(record: Mapping[str, Any], name: str) -> str:
    try:
        return str(Decimal(str(_required(record, name))).quantize(Decimal("0.01")))
    except InvalidOperation as error:
        raise ValueError(f"Apartment {name} must be numeric.") from error


class ApartmentAdapter(IndustryAdapter):
    domain = "apartment"

    def mapping_manifest(self) -> Mapping[str, str]:
        return {
            "lease_id": "entity_id",
            "updated_at": "observed_at",
            "unit_id": "attributes.unit_id",
            "resident_count": "attributes.resident_count",
            "start_date": "attributes.start_date",
            "end_date": "attributes.end_date",
            "rent": "attributes.rent",
            "balance_due": "attributes.balance_due",
            "status": "attributes.status",
            "currency": "attributes.currency",
        }

    def translate(
        self,
        record: Mapping[str, Any],
        *,
        tenant_id: str,
        source_system: str,
    ) -> CanonicalObservation:
        lease_id = str(_required(record, "lease_id"))
        return CanonicalObservation(
            tenant_id=tenant_id,
            domain=self.domain,
            entity_type="lease",
            entity_id=lease_id,
            source_system=source_system,
            source_record_id=lease_id,
            source_version=str(record.get("version")) if record.get("version") else None,
            observed_at=str(_required(record, "updated_at")),
            attributes={
                "unit_id": str(_required(record, "unit_id")),
                "resident_count": int(_required(record, "resident_count")),
                "start_date": str(_required(record, "start_date")),
                "end_date": str(_required(record, "end_date")),
                "rent": _money(record, "rent"),
                "balance_due": _money(record, "balance_due"),
                "status": str(_required(record, "status")).lower(),
                "currency": str(record.get("currency", "USD")).upper(),
            },
        )

    def propose_actions(self, changes: ChangeSet) -> tuple[ActionProposal, ...]:
        by_field = {change.field: change for change in changes.changes}
        balance = by_field.get("balance_due")
        status = by_field.get("status")
        balance_due = Decimal(str(balance.after)) if balance and balance.after else Decimal("0")
        became_delinquent = status is not None and status.after == "delinquent"
        if balance_due > 0 or became_delinquent:
            return (
                ActionProposal(
                    tenant_id=changes.tenant_id,
                    domain=self.domain,
                    action_type="review_delinquent_lease",
                    entity_type=changes.entity_type,
                    entity_id=changes.entity_id,
                    reason="Lease balance or status now requires human review.",
                    payload={
                        "lease_id": changes.entity_id,
                        "balance_due": str(balance_due.quantize(Decimal("0.01"))),
                        "review_tasks": [
                            "confirm ledger entries",
                            "review payment arrangement",
                            "check applicable notice policy",
                        ],
                    },
                    risk=RiskLevel.MEDIUM,
                ),
            )
        return ()
