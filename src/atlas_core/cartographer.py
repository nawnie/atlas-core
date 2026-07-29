from __future__ import annotations

import json

from .memory import CompanyMemory
from .models import CanonicalObservation, CartographyCard, ChangeSet
from .translator import IndustryAdapter


class Cartographer:
    """Searchable source maps, lineage, decisions, and continuity."""

    def __init__(self, memory: CompanyMemory) -> None:
        self._memory = memory

    def record(self, card: CartographyCard) -> CartographyCard:
        return self._memory.record_cartography_card(card)

    def search(
        self,
        *,
        tenant_id: str,
        query: str = "",
        lane: str | None = None,
        limit: int = 20,
    ) -> tuple[CartographyCard, ...]:
        return self._memory.search_cartography_cards(
            tenant_id=tenant_id,
            query=query,
            lane=lane,
            limit=limit,
        )

    def record_ingestion(
        self,
        *,
        observation: CanonicalObservation,
        changes: ChangeSet,
        adapter: IndustryAdapter,
        proposal_ids: tuple[str, ...],
    ) -> CartographyCard:
        mapping = adapter.mapping_manifest()
        source_pointer = (
            f"{observation.source_system}:"
            f"{observation.source_record_id}:"
            f"{observation.source_version or 'unversioned'}"
        )
        return self.record(
            CartographyCard(
                tenant_id=observation.tenant_id,
                project=f"{observation.domain}:{observation.entity_type}",
                lane=f"{observation.domain}_operations",
                title=f"Mapped {observation.entity_type} {observation.entity_id}",
                summary=changes.summary,
                details=json.dumps(
                    {
                        "adapter": type(adapter).__name__,
                        "mapping": mapping,
                        "observation_id": observation.id,
                    },
                    sort_keys=True,
                ),
                source_pointers=(source_pointer, observation.id or ""),
                tags=(observation.domain, observation.entity_type, "ingestion"),
                must_retrieve=(observation.id or "",),
                decisions=(
                    f"Translated with {type(adapter).__name__}.",
                    f"Produced {len(proposal_ids)} action proposal(s).",
                ),
                verification=(
                    f"canonical_mapping_fields={len(mapping)}",
                    f"changed_fields={len(changes.changes)}",
                ),
                unresolved=tuple(
                    f"Human review required for proposal {proposal_id}"
                    for proposal_id in proposal_ids
                ),
                risk_level="medium" if proposal_ids else "low",
                current_verification_required=True,
            )
        )
