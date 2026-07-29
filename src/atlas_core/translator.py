from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any

from .models import ActionProposal, CanonicalObservation, ChangeSet


class IndustryAdapter(ABC):
    """Boundary between domain-specific records and Atlas Core contracts."""

    domain: str

    def mapping_manifest(self) -> Mapping[str, str]:
        """Describe source-field to canonical-field mappings without source data."""
        return {}

    @abstractmethod
    def translate(
        self,
        record: Mapping[str, Any],
        *,
        tenant_id: str,
        source_system: str,
    ) -> CanonicalObservation:
        raise NotImplementedError

    @abstractmethod
    def propose_actions(self, changes: ChangeSet) -> tuple[ActionProposal, ...]:
        raise NotImplementedError
