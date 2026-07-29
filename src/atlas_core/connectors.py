from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Protocol

from .models import ConnectorRecord


class ReadOnlyConnector(Protocol):
    connector_id: str
    domain: str
    source_system: str

    def read(self) -> Iterable[ConnectorRecord]: ...


class SnapshotConnector:
    """Bounded in-memory connector for files, APIs, and replayable fixtures."""

    def __init__(
        self,
        *,
        connector_id: str,
        domain: str,
        source_system: str,
        records: Sequence[Mapping[str, Any]],
    ) -> None:
        self.connector_id = connector_id.strip()
        self.domain = domain.strip()
        self.source_system = source_system.strip()
        if not self.connector_id or not self.domain or not self.source_system:
            raise ValueError("connector_id, domain, and source_system are required.")
        self._records = tuple(self._coerce(record) for record in records)

    @staticmethod
    def _coerce(record: Mapping[str, Any]) -> ConnectorRecord:
        record_id = str(record.get("record_id", "")).strip()
        version = str(record.get("version", "")).strip()
        payload = record.get("payload")
        if not record_id or not version or not isinstance(payload, Mapping):
            raise ValueError(
                "Each connector record requires record_id, version, and payload."
            )
        if not payload:
            raise ValueError("Connector record payload must not be empty.")
        return ConnectorRecord(record_id, version, dict(payload))

    def read(self) -> Iterable[ConnectorRecord]:
        return iter(self._records)
