from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..models import ActionProposal, CanonicalObservation, ChangeSet, RiskLevel
from ..translator import IndustryAdapter


def _required(record: Mapping[str, Any], name: str) -> Any:
    value = record.get(name)
    if value is None or value == "":
        raise ValueError(f"Missing required manufacturing field: {name}")
    return value


def _non_negative_int(record: Mapping[str, Any], name: str) -> int:
    value = int(_required(record, name))
    if value < 0:
        raise ValueError(f"Manufacturing {name} must not be negative.")
    return value


class ManufacturingAdapter(IndustryAdapter):
    domain = "manufacturing"

    def mapping_manifest(self) -> Mapping[str, str]:
        return {
            "work_order_id": "entity_id",
            "updated_at": "observed_at",
            "line_id": "attributes.line_id",
            "sku": "attributes.sku",
            "status": "attributes.status",
            "quantity_planned": "attributes.quantity_planned",
            "quantity_completed": "attributes.quantity_completed",
            "defect_count": "attributes.defect_count",
            "downtime_minutes": "attributes.downtime_minutes",
            "quality_hold": "attributes.quality_hold",
        }

    def translate(
        self,
        record: Mapping[str, Any],
        *,
        tenant_id: str,
        source_system: str,
    ) -> CanonicalObservation:
        work_order_id = str(_required(record, "work_order_id"))
        quality_hold = _required(record, "quality_hold")
        if not isinstance(quality_hold, bool):
            raise ValueError("Manufacturing quality_hold must be a boolean.")
        return CanonicalObservation(
            tenant_id=tenant_id,
            domain=self.domain,
            entity_type="work_order",
            entity_id=work_order_id,
            source_system=source_system,
            source_record_id=work_order_id,
            source_version=str(record.get("version")) if record.get("version") else None,
            observed_at=str(_required(record, "updated_at")),
            attributes={
                "line_id": str(_required(record, "line_id")),
                "sku": str(_required(record, "sku")),
                "status": str(_required(record, "status")).lower(),
                "quantity_planned": _non_negative_int(record, "quantity_planned"),
                "quantity_completed": _non_negative_int(record, "quantity_completed"),
                "defect_count": _non_negative_int(record, "defect_count"),
                "downtime_minutes": _non_negative_int(record, "downtime_minutes"),
                "quality_hold": quality_hold,
            },
        )

    def propose_actions(self, changes: ChangeSet) -> tuple[ActionProposal, ...]:
        by_field = {change.field: change for change in changes.changes}
        hold = by_field.get("quality_hold")
        status = by_field.get("status")
        defects = by_field.get("defect_count")
        hold_started = hold is not None and hold.after is True
        line_stopped = status is not None and status.after in {"stopped", "quality_hold"}
        defects_increased = (
            defects is not None
            and defects.before is not None
            and int(defects.after) > int(defects.before)
        )
        if not hold_started and not line_stopped and not defects_increased:
            return ()
        return (
            ActionProposal(
                tenant_id=changes.tenant_id,
                domain=self.domain,
                action_type="review_manufacturing_quality_exception",
                entity_type=changes.entity_type,
                entity_id=changes.entity_id,
                reason="A quality or production-line exception requires human review.",
                payload={
                    "work_order_id": changes.entity_id,
                    "quality_hold": hold_started,
                    "line_stopped": line_stopped,
                    "defects_increased": defects_increased,
                    "review_tasks": [
                        "confirm inspection results",
                        "review machine and operator notes",
                        "verify disposition before releasing the work order",
                    ],
                },
                risk=RiskLevel.MEDIUM,
            ),
        )
