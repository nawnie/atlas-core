"""Strict declarative source-to-canonical industry adapters."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any

from .models import (
    ActionProposal,
    CanonicalObservation,
    ChangeSet,
    RiskLevel,
)
from .translator import IndustryAdapter

MAX_SPEC_BYTES = 64 * 1024
IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,99}$")
FIELD_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
ACTION_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,99}$")
INTEGER = re.compile(r"^-?(?:0|[1-9][0-9]{0,17})$")
DIRECT_SENSITIVE_FIELDS = {
    "address",
    "birth_date",
    "clinical_note",
    "clinical_notes",
    "date_of_birth",
    "diagnosis",
    "dob",
    "email",
    "first_name",
    "full_name",
    "last_name",
    "medical_record_number",
    "mrn",
    "patient_name",
    "phone",
    "phone_number",
    "social_security_number",
    "ssn",
}
REVIEWED_CODED_DOMAINS = {"clinical", "healthcare", "medical"}


class DeclarativeValueType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"


class DeclarativeNormalization(StrEnum):
    NONE = "none"
    LOWER = "lower"
    UPPER = "upper"
    STRIP = "strip"


class DeclarativeRuleOperator(StrEnum):
    CHANGED_TO = "changed_to"
    CHANGED_FROM = "changed_from"
    EQUALS = "equals"
    TRUTHY = "truthy"
    NUMERIC_GREATER_THAN = "numeric_greater_than"
    NUMERIC_LESS_THAN = "numeric_less_than"


@dataclass(frozen=True)
class DeclarativeField:
    source: str
    target: str
    value_type: DeclarativeValueType
    required: bool = True
    normalization: DeclarativeNormalization = DeclarativeNormalization.NONE

    def __post_init__(self) -> None:
        if not FIELD_NAME.fullmatch(self.source):
            raise ValueError("Declarative source field is invalid.")
        if not FIELD_NAME.fullmatch(self.target):
            raise ValueError("Declarative target field is invalid.")
        if (
            self.source.lower() in DIRECT_SENSITIVE_FIELDS
            or self.target.lower() in DIRECT_SENSITIVE_FIELDS
        ):
            raise ValueError(
                "Direct sensitive fields require a reviewed coded adapter."
            )
        if (
            self.value_type is not DeclarativeValueType.STRING
            and self.normalization is not DeclarativeNormalization.NONE
        ):
            raise ValueError("Normalization is supported only for string fields.")


@dataclass(frozen=True)
class DeclarativeApprovalRule:
    rule_id: str
    field: str
    operator: DeclarativeRuleOperator
    value: str | int | float | bool | None
    action_type: str
    reason: str
    risk: RiskLevel
    review_tasks: tuple[str, ...]

    def __post_init__(self) -> None:
        if not IDENTIFIER.fullmatch(self.rule_id):
            raise ValueError("Declarative rule identifier is invalid.")
        if not FIELD_NAME.fullmatch(self.field):
            raise ValueError("Declarative rule field is invalid.")
        if not ACTION_TYPE.fullmatch(self.action_type):
            raise ValueError("Declarative rule action type is invalid.")
        if not self.reason.strip() or len(self.reason) > 500:
            raise ValueError("Declarative rule reason must be 1-500 characters.")
        if not self.review_tasks or len(self.review_tasks) > 20:
            raise ValueError("Declarative rule requires 1-20 review tasks.")
        if any(not task.strip() or len(task) > 200 for task in self.review_tasks):
            raise ValueError("Declarative review task is invalid.")
        if isinstance(self.value, (dict, list, tuple)):
            raise ValueError("Declarative rule values must be JSON scalars.")
        if self.operator is DeclarativeRuleOperator.TRUTHY and self.value is not None:
            raise ValueError("The truthy operator does not accept a value.")
        if (
            self.operator is not DeclarativeRuleOperator.TRUTHY
            and self.value is None
        ):
            raise ValueError("Declarative rule operator requires a value.")


@dataclass(frozen=True)
class DeclarativeAdapterSpec:
    adapter_id: str
    domain: str
    entity_type: str
    id_field: str
    observed_at_field: str
    fields: tuple[DeclarativeField, ...]
    rules: tuple[DeclarativeApprovalRule, ...] = ()
    source_record_id_field: str = ""
    version_field: str = ""
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("Unsupported declarative adapter schema version.")
        for value in (self.adapter_id, self.domain, self.entity_type):
            if not IDENTIFIER.fullmatch(value):
                raise ValueError("Declarative adapter identifier is invalid.")
        if self.domain.lower() in REVIEWED_CODED_DOMAINS:
            raise ValueError(
                "Healthcare and clinical domains require a reviewed coded adapter."
            )
        for value in (
            self.id_field,
            self.observed_at_field,
            self.source_record_id_field or self.id_field,
        ):
            if not FIELD_NAME.fullmatch(value):
                raise ValueError("Declarative adapter identity field is invalid.")
            if value.lower() in DIRECT_SENSITIVE_FIELDS:
                raise ValueError(
                    "Direct sensitive fields require a reviewed coded adapter."
                )
        if self.version_field:
            if not FIELD_NAME.fullmatch(self.version_field):
                raise ValueError("Declarative version field is invalid.")
            if self.version_field.lower() in DIRECT_SENSITIVE_FIELDS:
                raise ValueError(
                    "Direct sensitive fields require a reviewed coded adapter."
                )
        if not self.fields or len(self.fields) > 100:
            raise ValueError("Declarative adapter requires 1-100 mapped fields.")
        sources = tuple(field.source for field in self.fields)
        targets = tuple(field.target for field in self.fields)
        if len(sources) != len(set(sources)) or len(targets) != len(set(targets)):
            raise ValueError("Declarative field mappings must be unique.")
        reserved = {
            self.id_field,
            self.observed_at_field,
            self.source_record_id_field or self.id_field,
        }
        if self.version_field:
            reserved.add(self.version_field)
        if reserved & set(sources):
            raise ValueError(
                "Identity, timestamp, and version fields cannot also be attributes."
            )
        if len(self.rules) > 50:
            raise ValueError("Declarative adapter supports at most 50 rules.")
        rule_ids = tuple(rule.rule_id for rule in self.rules)
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("Declarative rule identifiers must be unique.")
        by_target = {field.target: field for field in self.fields}
        for rule in self.rules:
            mapped = by_target.get(rule.field)
            if mapped is None:
                raise ValueError("Declarative rule field is not mapped.")
            if (
                rule.operator
                in {
                    DeclarativeRuleOperator.NUMERIC_GREATER_THAN,
                    DeclarativeRuleOperator.NUMERIC_LESS_THAN,
                }
                and mapped.value_type
                not in {
                    DeclarativeValueType.INTEGER,
                    DeclarativeValueType.DECIMAL,
                }
            ):
                raise ValueError("Numeric rules require a numeric mapped field.")
            if (
                rule.operator is DeclarativeRuleOperator.TRUTHY
                and mapped.value_type is not DeclarativeValueType.BOOLEAN
            ):
                raise ValueError("Truthy rules require a boolean mapped field.")
            if rule.value is not None:
                _coerce_value(mapped, rule.value)

    def mapping_manifest(self) -> dict[str, str]:
        mapping = {
            self.id_field: "entity_id",
            self.observed_at_field: "observed_at",
        }
        source_record_field = self.source_record_id_field or self.id_field
        if source_record_field == self.id_field:
            mapping[self.id_field] = "entity_id|source_record_id"
        else:
            mapping[source_record_field] = "source_record_id"
        if self.version_field:
            mapping[self.version_field] = "source_version"
        mapping.update(
            {field.source: f"attributes.{field.target}" for field in self.fields}
        )
        return dict(sorted(mapping.items()))

    def safe_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for field in payload["fields"]:
            field["value_type"] = field["value_type"].value
            field["normalization"] = field["normalization"].value
        for rule in payload["rules"]:
            rule["operator"] = rule["operator"].value
            rule["risk"] = rule["risk"].value
        return payload

    def sha256(self) -> str:
        encoded = json.dumps(
            self.safe_dict(),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


def _required_identifier(record: Mapping[str, Any], name: str) -> str:
    value = record.get(name)
    if value is None or value == "":
        raise ValueError(f"Missing required declarative field: {name}")
    if isinstance(value, (dict, list, tuple, bool)):
        raise ValueError(f"Declarative field {name} must be a scalar identifier.")
    cleaned = str(value).strip()
    if not cleaned or len(cleaned) > 200 or any(ord(char) < 32 for char in cleaned):
        raise ValueError(f"Declarative field {name} is invalid.")
    return cleaned


def _timestamp(record: Mapping[str, Any], name: str) -> str:
    value = _required_identifier(record, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(
            f"Declarative field {name} must be an ISO 8601 timestamp."
        ) from error
    if parsed.tzinfo is None:
        raise ValueError(
            f"Declarative field {name} must include a timezone offset."
        )
    return value


def _coerce_value(field: DeclarativeField, value: object) -> Any:
    if value is None or value == "":
        if field.required:
            raise ValueError(
                f"Missing required declarative field: {field.source}"
            )
        return None
    if isinstance(value, (dict, list, tuple)):
        raise ValueError(f"Declarative field {field.source} must be a scalar.")
    if field.value_type is DeclarativeValueType.STRING:
        text = str(value)
        if len(text) > 4096 or any(ord(char) == 0 for char in text):
            raise ValueError(f"Declarative field {field.source} is invalid.")
        if field.normalization is DeclarativeNormalization.LOWER:
            return text.strip().lower()
        if field.normalization is DeclarativeNormalization.UPPER:
            return text.strip().upper()
        if field.normalization is DeclarativeNormalization.STRIP:
            return text.strip()
        return text
    if field.value_type is DeclarativeValueType.BOOLEAN:
        if not isinstance(value, bool):
            raise ValueError(
                f"Declarative field {field.source} must be a boolean."
            )
        return value
    if field.value_type is DeclarativeValueType.INTEGER:
        if isinstance(value, bool):
            raise ValueError(
                f"Declarative field {field.source} must be an integer."
            )
        if isinstance(value, int):
            return value
        if isinstance(value, str) and INTEGER.fullmatch(value.strip()):
            return int(value)
        raise ValueError(f"Declarative field {field.source} must be an integer.")
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(
            f"Declarative field {field.source} must be numeric."
        ) from error
    if not decimal.is_finite():
        raise ValueError(f"Declarative field {field.source} must be finite.")
    return str(decimal.quantize(Decimal("0.01")))


class DeclarativeIndustryAdapter(IndustryAdapter):
    """IndustryAdapter backed by a strict, bounded mapping specification."""

    def __init__(self, spec: DeclarativeAdapterSpec) -> None:
        self.spec = spec
        self.domain = spec.domain
        self.adapter_id = spec.adapter_id
        self._fields = {field.target: field for field in spec.fields}

    @classmethod
    def from_file(cls, path: str | Path) -> DeclarativeIndustryAdapter:
        return cls(load_declarative_adapter_spec(path))

    def mapping_manifest(self) -> Mapping[str, str]:
        return self.spec.mapping_manifest()

    def translate(
        self,
        record: Mapping[str, Any],
        *,
        tenant_id: str,
        source_system: str,
    ) -> CanonicalObservation:
        if not tenant_id.strip() or len(tenant_id) > 100:
            raise ValueError("Declarative tenant_id must be 1-100 characters.")
        if not source_system.strip() or len(source_system) > 100:
            raise ValueError("Declarative source_system must be 1-100 characters.")
        allowed = set(self.spec.mapping_manifest())
        unknown = set(record) - allowed
        if unknown:
            raise ValueError("Declarative record contains unmapped source fields.")
        entity_id = _required_identifier(record, self.spec.id_field)
        source_record_id = _required_identifier(
            record,
            self.spec.source_record_id_field or self.spec.id_field,
        )
        attributes = {
            field.target: _coerce_value(field, record.get(field.source))
            for field in self.spec.fields
            if field.required or field.source in record
        }
        version_value = (
            record.get(self.spec.version_field) if self.spec.version_field else None
        )
        version = (
            _required_identifier(record, self.spec.version_field)
            if self.spec.version_field
            and version_value is not None
            and version_value != ""
            else None
        )
        return CanonicalObservation(
            tenant_id=tenant_id.strip(),
            domain=self.spec.domain,
            entity_type=self.spec.entity_type,
            entity_id=entity_id,
            source_system=source_system.strip(),
            source_record_id=source_record_id,
            source_version=version,
            observed_at=_timestamp(record, self.spec.observed_at_field),
            attributes=attributes,
        )

    def propose_actions(self, changes: ChangeSet) -> tuple[ActionProposal, ...]:
        by_field = {change.field: change for change in changes.changes}
        proposals: list[ActionProposal] = []
        for rule in self.spec.rules:
            change = by_field.get(rule.field)
            if change is None or not self._matches(rule, change.before, change.after):
                continue
            proposals.append(
                ActionProposal(
                    tenant_id=changes.tenant_id,
                    domain=self.spec.domain,
                    action_type=rule.action_type,
                    entity_type=changes.entity_type,
                    entity_id=changes.entity_id,
                    reason=rule.reason,
                    payload={
                        "entity_id": changes.entity_id,
                        "rule_id": rule.rule_id,
                        "matched_field": rule.field,
                        "before": change.before,
                        "after": change.after,
                        "review_tasks": list(rule.review_tasks),
                    },
                    risk=rule.risk,
                    requested_by=f"declarative:{self.spec.adapter_id}",
                )
            )
        return tuple(proposals)

    def _matches(
        self,
        rule: DeclarativeApprovalRule,
        before: object,
        after: object,
    ) -> bool:
        field = self._fields[rule.field]
        expected = (
            _coerce_value(field, rule.value) if rule.value is not None else None
        )
        if rule.operator is DeclarativeRuleOperator.CHANGED_TO:
            return before != after and after == expected
        if rule.operator is DeclarativeRuleOperator.CHANGED_FROM:
            return before != after and before == expected
        if rule.operator is DeclarativeRuleOperator.EQUALS:
            return after == expected
        if rule.operator is DeclarativeRuleOperator.TRUTHY:
            return after is True
        if rule.operator is DeclarativeRuleOperator.NUMERIC_GREATER_THAN:
            return Decimal(str(after)) > Decimal(str(expected))
        return Decimal(str(after)) < Decimal(str(expected))


def load_declarative_adapter_spec(path: str | Path) -> DeclarativeAdapterSpec:
    source = Path(path).expanduser().resolve()
    try:
        if source.stat().st_size > MAX_SPEC_BYTES:
            raise ValueError("Declarative adapter spec exceeds the 64 KiB limit.")
        text = source.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError("Declarative adapter spec cannot be read.") from error
    except UnicodeError as error:
        raise ValueError("Declarative adapter spec must be valid UTF-8 JSON.") from error
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as error:
        raise ValueError("Declarative adapter spec must be valid UTF-8 JSON.") from error
    return declarative_adapter_spec_from_dict(payload)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Declarative adapter spec contains duplicate JSON keys.")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Declarative adapter spec rejects non-finite JSON value {value}.")


def _string_value(
    payload: Mapping[str, Any],
    name: str,
    *,
    default: str | None = None,
) -> str:
    value = payload.get(name, default)
    if not isinstance(value, str):
        raise ValueError(f"Declarative adapter field {name} must be a string.")
    return value


def declarative_adapter_spec_from_dict(payload: object) -> DeclarativeAdapterSpec:
    if not isinstance(payload, dict):
        raise ValueError("Declarative adapter spec must be a JSON object.")
    allowed = {
        "adapter_id",
        "domain",
        "entity_type",
        "id_field",
        "observed_at_field",
        "source_record_id_field",
        "version_field",
        "fields",
        "rules",
        "schema_version",
    }
    if set(payload) - allowed:
        raise ValueError("Declarative adapter spec contains unknown fields.")
    required = {
        "adapter_id",
        "domain",
        "entity_type",
        "id_field",
        "observed_at_field",
        "fields",
    }
    if required - set(payload):
        raise ValueError("Declarative adapter spec is missing required fields.")
    fields_payload = payload["fields"]
    rules_payload = payload.get("rules", [])
    if not isinstance(fields_payload, list) or not isinstance(rules_payload, list):
        raise ValueError("Declarative fields and rules must be arrays.")
    fields = tuple(_field_from_dict(item) for item in fields_payload)
    rules = tuple(_rule_from_dict(item) for item in rules_payload)
    try:
        schema_version = payload.get("schema_version", 1)
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise TypeError
        return DeclarativeAdapterSpec(
            adapter_id=_string_value(payload, "adapter_id"),
            domain=_string_value(payload, "domain"),
            entity_type=_string_value(payload, "entity_type"),
            id_field=_string_value(payload, "id_field"),
            observed_at_field=_string_value(payload, "observed_at_field"),
            source_record_id_field=_string_value(
                payload, "source_record_id_field", default=""
            ),
            version_field=_string_value(payload, "version_field", default=""),
            fields=fields,
            rules=rules,
            schema_version=schema_version,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("Declarative adapter spec values are invalid.") from error


def _field_from_dict(payload: object) -> DeclarativeField:
    if not isinstance(payload, dict):
        raise ValueError("Declarative field mapping must be an object.")
    allowed = {"source", "target", "value_type", "required", "normalization"}
    if set(payload) - allowed or {"source", "target", "value_type"} - set(payload):
        raise ValueError("Declarative field mapping shape is invalid.")
    required = payload.get("required", True)
    if not isinstance(required, bool):
        raise ValueError("Declarative field required must be a boolean.")
    try:
        return DeclarativeField(
            source=_string_value(payload, "source"),
            target=_string_value(payload, "target"),
            value_type=DeclarativeValueType(str(payload["value_type"])),
            required=required,
            normalization=DeclarativeNormalization(
                str(payload.get("normalization", "none"))
            ),
        )
    except ValueError as error:
        raise ValueError("Declarative field mapping values are invalid.") from error


def _rule_from_dict(payload: object) -> DeclarativeApprovalRule:
    if not isinstance(payload, dict):
        raise ValueError("Declarative approval rule must be an object.")
    allowed = {
        "rule_id",
        "field",
        "operator",
        "value",
        "action_type",
        "reason",
        "risk",
        "review_tasks",
    }
    required = allowed - {"value"}
    if set(payload) - allowed or required - set(payload):
        raise ValueError("Declarative approval rule shape is invalid.")
    tasks = payload["review_tasks"]
    if not isinstance(tasks, list) or not all(isinstance(task, str) for task in tasks):
        raise ValueError("Declarative review tasks must be a string array.")
    try:
        return DeclarativeApprovalRule(
            rule_id=_string_value(payload, "rule_id"),
            field=_string_value(payload, "field"),
            operator=DeclarativeRuleOperator(str(payload["operator"])),
            value=payload.get("value"),
            action_type=_string_value(payload, "action_type"),
            reason=_string_value(payload, "reason"),
            risk=RiskLevel(str(payload["risk"])),
            review_tasks=tuple(tasks),
        )
    except ValueError as error:
        raise ValueError("Declarative approval rule values are invalid.") from error
