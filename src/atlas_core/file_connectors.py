from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .models import ConnectorRecord

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_RECORDS = 1000
MAX_FIELDS = 200
MAX_TEXT_LENGTH = 100_000
MAX_NESTING_DEPTH = 20


@dataclass(frozen=True)
class FileConnectorPreview:
    format: str
    source_name: str
    sha256: str
    size_bytes: int
    record_count: int
    fields: tuple[str, ...]
    records: tuple[ConnectorRecord, ...]


def _json_depth(value: Any, depth: int = 0) -> int:
    if depth > MAX_NESTING_DEPTH:
        raise ValueError(
            f"Record payload nesting may not exceed {MAX_NESTING_DEPTH} levels."
        )
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 200:
                raise ValueError(
                    "Record payload keys must be non-empty text up to 200 characters."
                )
            _json_depth(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _json_depth(item, depth + 1)
    elif isinstance(value, str) and len(value) > MAX_TEXT_LENGTH:
        raise ValueError(
            f"Record text values may not exceed {MAX_TEXT_LENGTH} characters."
        )
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Record numeric values must be finite.")
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValueError("Record payload values must be JSON-compatible.")
    return depth


def _record(
    record_id: object,
    version: object,
    payload: object,
    *,
    location: str,
) -> ConnectorRecord:
    record_id = str(record_id).strip()
    version = str(version).strip()
    if not record_id or not version:
        raise ValueError(f"{location} requires non-empty record_id and version.")
    if len(record_id) > 200 or len(version) > 200:
        raise ValueError(f"{location} record_id or version is too long.")
    if not isinstance(payload, Mapping) or not payload:
        raise ValueError(f"{location} payload must be a non-empty object.")
    if len(payload) > MAX_FIELDS:
        raise ValueError(
            f"{location} payload may not contain more than {MAX_FIELDS} fields."
        )
    normalized = dict(payload)
    _json_depth(normalized)
    return ConnectorRecord(record_id, version, normalized)


def _decode(content: bytes) -> str:
    if not isinstance(content, bytes) or not content:
        raise ValueError("Import file must not be empty.")
    if len(content) > MAX_FILE_BYTES:
        raise ValueError(f"Import file may not exceed {MAX_FILE_BYTES} bytes.")
    if b"\x00" in content:
        raise ValueError("Import file must be UTF-8 text without NUL bytes.")
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("Import file must be valid UTF-8 text.") from error


def _coerce_csv_cell(value: str) -> Any:
    stripped = value.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, RecursionError):
        return value
    return parsed


def parse_jsonl(content: bytes) -> tuple[ConnectorRecord, ...]:
    text = _decode(content)
    records: list[ConnectorRecord] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if len(line) > MAX_TEXT_LENGTH * 2:
            raise ValueError(f"JSONL line {line_number} is too long.")
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, RecursionError) as error:
            raise ValueError(f"JSONL line {line_number} is invalid JSON.") from error
        if not isinstance(value, Mapping):
            raise ValueError(f"JSONL line {line_number} must be an object.")
        records.append(
            _record(
                value.get("record_id", ""),
                value.get("version", ""),
                value.get("payload"),
                location=f"JSONL line {line_number}",
            )
        )
        if len(records) > MAX_RECORDS:
            raise ValueError(
                f"Import file may not contain more than {MAX_RECORDS} records."
            )
    if not records:
        raise ValueError("Import file contains no records.")
    return tuple(records)


def parse_csv(content: bytes) -> tuple[ConnectorRecord, ...]:
    text = _decode(content)
    try:
        rows = csv.reader(io.StringIO(text, newline=""), strict=True)
        header = next(rows)
    except (StopIteration, csv.Error) as error:
        raise ValueError("CSV file has no valid header.") from error
    header = [name.strip() for name in header]
    if (
        not header
        or any(not name or len(name) > 200 for name in header)
        or len(header) != len(set(header))
    ):
        raise ValueError("CSV header names must be unique, non-empty text.")
    if "record_id" not in header or "version" not in header:
        raise ValueError("CSV requires record_id and version columns.")
    payload_fields = [
        name for name in header if name not in {"record_id", "version"}
    ]
    if not payload_fields:
        raise ValueError("CSV requires at least one payload column.")
    if len(payload_fields) > MAX_FIELDS:
        raise ValueError(
            f"CSV may not contain more than {MAX_FIELDS} payload columns."
        )
    records: list[ConnectorRecord] = []
    try:
        for row_number, row in enumerate(rows, 2):
            if not row or not any(cell.strip() for cell in row):
                continue
            if len(row) != len(header):
                raise ValueError(
                    f"CSV row {row_number} has {len(row)} cells; expected {len(header)}."
                )
            values = dict(zip(header, row, strict=True))
            payload = {
                name: converted
                for name in payload_fields
                if (converted := _coerce_csv_cell(values[name])) is not None
            }
            records.append(
                _record(
                    values["record_id"],
                    values["version"],
                    payload,
                    location=f"CSV row {row_number}",
                )
            )
            if len(records) > MAX_RECORDS:
                raise ValueError(
                    f"Import file may not contain more than {MAX_RECORDS} records."
                )
    except csv.Error as error:
        raise ValueError("CSV file is malformed.") from error
    if not records:
        raise ValueError("Import file contains no records.")
    return tuple(records)


def preview_file(
    content: bytes,
    *,
    format: str,
    source_name: str,
) -> FileConnectorPreview:
    normalized_format = format.strip().lower()
    source_name = source_name.strip()
    if normalized_format not in {"jsonl", "csv"}:
        raise ValueError("format must be jsonl or csv.")
    if not source_name or len(source_name) > 255:
        raise ValueError("source_name is required and must be at most 255 characters.")
    if "/" in source_name or "\\" in source_name or "\x00" in source_name:
        raise ValueError("source_name must be a plain filename.")
    records = (
        parse_jsonl(content)
        if normalized_format == "jsonl"
        else parse_csv(content)
    )
    fields = tuple(
        sorted({field for record in records for field in record.payload})
    )
    return FileConnectorPreview(
        format=normalized_format,
        source_name=source_name,
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        record_count=len(records),
        fields=fields,
        records=records,
    )


class FileSnapshotConnector:
    """Read-only connector created from validated JSONL or CSV bytes."""

    def __init__(
        self,
        *,
        connector_id: str,
        domain: str,
        source_system: str,
        preview: FileConnectorPreview,
    ) -> None:
        self.connector_id = connector_id.strip()
        self.domain = domain.strip()
        self.source_system = source_system.strip()
        if not self.connector_id or not self.domain or not self.source_system:
            raise ValueError("connector_id, domain, and source_system are required.")
        self.preview = preview

    def read(self) -> tuple[ConnectorRecord, ...]:
        return self.preview.records
