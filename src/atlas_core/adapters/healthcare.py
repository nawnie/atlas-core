from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..models import ActionProposal, CanonicalObservation, ChangeSet, RiskLevel
from ..translator import IndustryAdapter


def _required(record: Mapping[str, Any], name: str) -> Any:
    value = record.get(name)
    if value is None or value == "":
        raise ValueError(f"Missing required healthcare field: {name}")
    return value


class HealthcareAdapter(IndustryAdapter):
    """Administrative healthcare adapter.

    The canonical record intentionally uses an opaque patient reference and does
    not accept names, birth dates, diagnoses, clinical notes, or contact data.
    """

    domain = "healthcare"

    def mapping_manifest(self) -> Mapping[str, str]:
        return {
            "encounter_id": "entity_id",
            "updated_at": "observed_at",
            "patient_ref": "attributes.patient_ref",
            "care_setting": "attributes.care_setting",
            "status": "attributes.status",
            "authorization_status": "attributes.authorization_status",
            "documentation_complete": "attributes.documentation_complete",
            "service_code": "attributes.service_code",
        }

    def translate(
        self,
        record: Mapping[str, Any],
        *,
        tenant_id: str,
        source_system: str,
    ) -> CanonicalObservation:
        forbidden = {
            "patient_name",
            "date_of_birth",
            "diagnosis",
            "clinical_notes",
            "email",
            "phone",
            "address",
        }
        present = sorted(field for field in forbidden if record.get(field))
        if present:
            raise ValueError(
                "Healthcare administrative records must not include direct PHI fields: "
                + ", ".join(present)
            )
        encounter_id = str(_required(record, "encounter_id"))
        documentation_complete = _required(record, "documentation_complete")
        if not isinstance(documentation_complete, bool):
            raise ValueError("Healthcare documentation_complete must be a boolean.")
        return CanonicalObservation(
            tenant_id=tenant_id,
            domain=self.domain,
            entity_type="administrative_encounter",
            entity_id=encounter_id,
            source_system=source_system,
            source_record_id=encounter_id,
            source_version=str(record.get("version")) if record.get("version") else None,
            observed_at=str(_required(record, "updated_at")),
            attributes={
                "patient_ref": str(_required(record, "patient_ref")),
                "care_setting": str(_required(record, "care_setting")).lower(),
                "status": str(_required(record, "status")).lower(),
                "authorization_status": str(
                    _required(record, "authorization_status")
                ).lower(),
                "documentation_complete": documentation_complete,
                "service_code": str(_required(record, "service_code")),
            },
        )

    def propose_actions(self, changes: ChangeSet) -> tuple[ActionProposal, ...]:
        by_field = {change.field: change for change in changes.changes}
        authorization = by_field.get("authorization_status")
        documentation = by_field.get("documentation_complete")
        needs_auth_review = authorization is not None and authorization.after in {
            "denied",
            "expired",
            "more_information_required",
        }
        became_incomplete = documentation is not None and documentation.after is False
        if not needs_auth_review and not became_incomplete:
            return ()
        return (
            ActionProposal(
                tenant_id=changes.tenant_id,
                domain=self.domain,
                action_type="review_healthcare_authorization_exception",
                entity_type=changes.entity_type,
                entity_id=changes.entity_id,
                reason="An administrative authorization or documentation exception requires review.",
                payload={
                    "encounter_id": changes.entity_id,
                    "authorization_exception": needs_auth_review,
                    "documentation_incomplete": became_incomplete,
                    "review_tasks": [
                        "confirm payer response provenance",
                        "verify required administrative documentation",
                        "route to an authorized human reviewer",
                    ],
                    "execution_boundary": "No clinical or payer action is executed.",
                },
                risk=RiskLevel.MEDIUM,
            ),
        )
