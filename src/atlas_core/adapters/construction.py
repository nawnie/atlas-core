from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from ..models import ActionProposal, CanonicalObservation, ChangeSet, RiskLevel
from ..translator import IndustryAdapter


def _required(record: Mapping[str, Any], name: str) -> Any:
    value = record.get(name)
    if value is None or value == "":
        raise ValueError(f"Missing required construction field: {name}")
    return value


def _decimal(record: Mapping[str, Any], name: str) -> str:
    try:
        return str(Decimal(str(_required(record, name))).quantize(Decimal("0.01")))
    except InvalidOperation as error:
        raise ValueError(f"Construction {name} must be numeric.") from error


class ConstructionAdapter(IndustryAdapter):
    domain = "construction"

    def mapping_manifest(self) -> Mapping[str, str]:
        return {
            "work_item_id": "entity_id",
            "updated_at": "observed_at",
            "project_id": "attributes.project_id",
            "trade": "attributes.trade",
            "status": "attributes.status",
            "percent_complete": "attributes.percent_complete",
            "budget": "attributes.budget",
            "actual_cost": "attributes.actual_cost",
            "safety_hold": "attributes.safety_hold",
            "currency": "attributes.currency",
        }

    def translate(
        self,
        record: Mapping[str, Any],
        *,
        tenant_id: str,
        source_system: str,
    ) -> CanonicalObservation:
        work_item_id = str(_required(record, "work_item_id"))
        percent_complete = int(_required(record, "percent_complete"))
        if not 0 <= percent_complete <= 100:
            raise ValueError("Construction percent_complete must be between 0 and 100.")
        safety_hold = _required(record, "safety_hold")
        if not isinstance(safety_hold, bool):
            raise ValueError("Construction safety_hold must be a boolean.")
        return CanonicalObservation(
            tenant_id=tenant_id,
            domain=self.domain,
            entity_type="work_item",
            entity_id=work_item_id,
            source_system=source_system,
            source_record_id=work_item_id,
            source_version=str(record.get("version")) if record.get("version") else None,
            observed_at=str(_required(record, "updated_at")),
            attributes={
                "project_id": str(_required(record, "project_id")),
                "trade": str(_required(record, "trade")).lower(),
                "status": str(_required(record, "status")).lower(),
                "percent_complete": percent_complete,
                "budget": _decimal(record, "budget"),
                "actual_cost": _decimal(record, "actual_cost"),
                "safety_hold": safety_hold,
                "currency": str(record.get("currency", "USD")).upper(),
            },
        )

    def propose_actions(self, changes: ChangeSet) -> tuple[ActionProposal, ...]:
        by_field = {change.field: change for change in changes.changes}
        safety_hold = by_field.get("safety_hold")
        actual_cost = by_field.get("actual_cost")
        budget = by_field.get("budget")
        current_budget = Decimal(str(budget.after)) if budget else None
        current_cost = Decimal(str(actual_cost.after)) if actual_cost else None
        safety_started = safety_hold is not None and safety_hold.after is True
        over_budget = (
            current_budget is not None
            and current_cost is not None
            and current_cost > current_budget
        )
        if not safety_started and not over_budget:
            return ()
        return (
            ActionProposal(
                tenant_id=changes.tenant_id,
                domain=self.domain,
                action_type="review_construction_exception",
                entity_type=changes.entity_type,
                entity_id=changes.entity_id,
                reason="A construction safety hold or budget exception requires review.",
                payload={
                    "work_item_id": changes.entity_id,
                    "safety_hold": safety_started,
                    "over_budget": over_budget,
                    "review_tasks": [
                        "confirm the source update",
                        "review superintendent notes",
                        "verify the approved budget and change orders",
                    ],
                },
                risk=RiskLevel.MEDIUM,
            ),
        )
