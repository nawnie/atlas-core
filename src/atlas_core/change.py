from __future__ import annotations

from typing import Any

from .models import CanonicalObservation, ChangeSet, FieldChange


def _kind(before: Any, after: Any) -> str:
    if before is None:
        return "added"
    if after is None:
        return "removed"
    return "changed"


def compare(
    previous: CanonicalObservation | None,
    current: CanonicalObservation,
) -> ChangeSet:
    before = previous.attributes if previous else {}
    fields = sorted(set(before) | set(current.attributes))
    changes = tuple(
        FieldChange(
            field=name,
            before=before.get(name),
            after=current.attributes.get(name),
            kind=_kind(before.get(name), current.attributes.get(name)),
        )
        for name in fields
        if before.get(name) != current.attributes.get(name)
    )
    if previous is None:
        summary = f"First observation recorded with {len(changes)} populated fields."
    elif not changes:
        summary = "No business fields changed."
    else:
        summary = f"{len(changes)} field(s) changed: {', '.join(c.field for c in changes)}."
    return ChangeSet(
        tenant_id=current.tenant_id,
        domain=current.domain,
        entity_type=current.entity_type,
        entity_id=current.entity_id,
        previous_observation_id=previous.id if previous else None,
        current_observation_id=current.id or "",
        changes=changes,
        summary=summary,
    )
