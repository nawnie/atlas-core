from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .adapters import (
    ApartmentAdapter,
    ConstructionAdapter,
    HealthcareAdapter,
    HotelAdapter,
    ManufacturingAdapter,
)
from .engine import AtlasCore
from .file_connectors import MAX_FILE_BYTES, FileSnapshotConnector, preview_file
from .models import BackupReceipt, utc_now
from .recovery_destinations import (
    LocalFolderRecoveryDestination,
    PortableRecoveryReceipt,
)

AUTOMATION_SCHEMA_VERSION = 1
MAX_CONFIG_BYTES = 64 * 1024
MAX_INBOXES = 20
MAX_RECEIPTS = 100
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
DOMAINS = frozenset(
    {"hotel", "apartment", "construction", "manufacturing", "healthcare"}
)
ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


@dataclass(frozen=True)
class InboxJob:
    job_id: str
    tenant_id: str
    domain: str
    connector_id: str
    source_system: str
    path: Path


@dataclass(frozen=True)
class BackupJob:
    enabled: bool = True
    interval_minutes: int = 1440
    keep_last: int = 7
    apply_retention: bool = False


@dataclass(frozen=True)
class PortableDestinationJob:
    enabled: bool = False
    destination_id: str = "portable-recovery"
    kind: str = "local_folder"
    path: Path | None = None
    passphrase_env: str = "ATLAS_RECOVERY_PASSPHRASE"
    keep_last: int = 7
    apply_retention: bool = False


@dataclass(frozen=True)
class AutomationConfig:
    schema_version: int
    inboxes: tuple[InboxJob, ...]
    backup: BackupJob
    portable_destination: PortableDestinationJob


@dataclass(frozen=True)
class AutomationEvent:
    event_id: str
    run_id: str
    event_type: str
    status: str
    occurred_at: str
    job_id: str = ""
    source_name: str = ""
    sha256: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass(frozen=True)
class AutomationRunReceipt:
    run_id: str
    started_at: str
    completed_at: str
    status: str
    imported_files: int
    rejected_files: int
    replayed_files: int
    skipped_files: int
    backup_created: bool
    events: tuple[AutomationEvent, ...]
    portable_exported: bool = False


@dataclass(frozen=True)
class AutomationStatus:
    configured: bool
    config_path: str
    state_directory: str
    locked: bool
    last_run: AutomationRunReceipt | None
    recent_events: tuple[AutomationEvent, ...]


def _identifier(value: object, label: str) -> str:
    normalized = str(value).strip()
    if not IDENTIFIER.fullmatch(normalized):
        raise ValueError(
            f"{label} must be 1-100 characters using letters, numbers, dot, "
            "underscore, or hyphen."
        )
    return normalized


def _plain_error(error: Exception) -> str:
    message = " ".join(str(error).split())
    return (message or error.__class__.__name__)[:500]


def _reject_unknown(
    payload: dict[str, Any],
    allowed: frozenset[str],
    *,
    label: str,
) -> None:
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError(f"{label} contains unknown fields: {sorted(unknown)}")


def _resolve_directory(value: object, *, base: Path, label: str) -> Path:
    raw = str(value).strip()
    if not raw or "\x00" in raw:
        raise ValueError(f"{label} is required.")
    path = Path(raw)
    if not path.is_absolute():
        path = base / path
    return Path(os.path.abspath(path))


def load_automation_config(path: str | Path) -> AutomationConfig:
    config_path = Path(path).resolve()
    if not config_path.is_file():
        raise ValueError(f"Automation config does not exist: {config_path}")
    if config_path.stat().st_size > MAX_CONFIG_BYTES:
        raise ValueError(
            f"Automation config may not exceed {MAX_CONFIG_BYTES} bytes."
        )
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Automation config must be valid UTF-8 JSON.") from error
    if not isinstance(payload, dict):
        raise ValueError("Automation config must be a JSON object.")
    _reject_unknown(
        payload,
        frozenset(
            {"schema_version", "inboxes", "backup", "portable_destination"}
        ),
        label="Automation config",
    )
    if payload.get("schema_version") != AUTOMATION_SCHEMA_VERSION:
        raise ValueError(
            f"Automation config schema_version must be {AUTOMATION_SCHEMA_VERSION}."
        )
    raw_inboxes = payload.get("inboxes", [])
    if not isinstance(raw_inboxes, list) or len(raw_inboxes) > MAX_INBOXES:
        raise ValueError(
            f"Automation config may contain at most {MAX_INBOXES} inboxes."
        )
    inboxes: list[InboxJob] = []
    seen_jobs: set[str] = set()
    seen_paths: set[Path] = set()
    for index, raw in enumerate(raw_inboxes):
        if not isinstance(raw, dict):
            raise ValueError(f"inboxes[{index}] must be an object.")
        _reject_unknown(
            raw,
            frozenset(
                {
                    "job_id",
                    "tenant_id",
                    "domain",
                    "connector_id",
                    "source_system",
                    "path",
                }
            ),
            label=f"inboxes[{index}]",
        )
        job_id = _identifier(raw.get("job_id", ""), f"inboxes[{index}].job_id")
        if job_id in seen_jobs:
            raise ValueError(f"Duplicate inbox job_id: {job_id}")
        domain = str(raw.get("domain", "")).strip()
        if domain not in DOMAINS:
            raise ValueError(f"inboxes[{index}].domain is not supported.")
        inbox_path = _resolve_directory(
            raw.get("path", ""),
            base=config_path.parent,
            label=f"inboxes[{index}].path",
        )
        if inbox_path in seen_paths:
            raise ValueError(f"Duplicate inbox path: {inbox_path}")
        seen_jobs.add(job_id)
        seen_paths.add(inbox_path)
        inboxes.append(
            InboxJob(
                job_id=job_id,
                tenant_id=_identifier(
                    raw.get("tenant_id", ""),
                    f"inboxes[{index}].tenant_id",
                ),
                domain=domain,
                connector_id=_identifier(
                    raw.get("connector_id", ""),
                    f"inboxes[{index}].connector_id",
                ),
                source_system=_identifier(
                    raw.get("source_system", ""),
                    f"inboxes[{index}].source_system",
                ),
                path=inbox_path,
            )
        )
    raw_backup = payload.get("backup", {})
    if not isinstance(raw_backup, dict):
        raise ValueError("backup must be an object.")
    _reject_unknown(
        raw_backup,
        frozenset(
            {"enabled", "interval_minutes", "keep_last", "apply_retention"}
        ),
        label="backup",
    )
    enabled = raw_backup.get("enabled", True)
    apply_retention = raw_backup.get("apply_retention", False)
    if not isinstance(enabled, bool) or not isinstance(apply_retention, bool):
        raise ValueError("backup enabled and apply_retention must be booleans.")
    interval = raw_backup.get("interval_minutes", 1440)
    keep_last = raw_backup.get("keep_last", 7)
    if not isinstance(interval, int) or not 15 <= interval <= 10080:
        raise ValueError("backup.interval_minutes must be between 15 and 10080.")
    if not isinstance(keep_last, int) or not 2 <= keep_last <= 1000:
        raise ValueError("backup.keep_last must be between 2 and 1000.")
    raw_destination = payload.get("portable_destination", {})
    if not isinstance(raw_destination, dict):
        raise ValueError("portable_destination must be an object.")
    _reject_unknown(
        raw_destination,
        frozenset(
            {
                "enabled",
                "destination_id",
                "kind",
                "path",
                "passphrase_env",
                "keep_last",
                "apply_retention",
            }
        ),
        label="portable_destination",
    )
    destination_enabled = raw_destination.get("enabled", False)
    destination_retention = raw_destination.get("apply_retention", False)
    if not isinstance(destination_enabled, bool) or not isinstance(
        destination_retention,
        bool,
    ):
        raise ValueError(
            "portable destination enabled and apply_retention must be booleans."
        )
    destination_id = _identifier(
        raw_destination.get("destination_id", "portable-recovery"),
        "portable_destination.destination_id",
    )
    destination_kind = str(
        raw_destination.get("kind", "local_folder")
    ).strip()
    if destination_kind != "local_folder":
        raise ValueError("portable_destination.kind must be local_folder.")
    raw_destination_path = raw_destination.get("path", "")
    destination_path = (
        _resolve_directory(
            raw_destination_path,
            base=config_path.parent,
            label="portable_destination.path",
        )
        if raw_destination_path
        else None
    )
    if destination_enabled and destination_path is None:
        raise ValueError("Enabled portable destination requires a path.")
    passphrase_env = str(
        raw_destination.get(
            "passphrase_env",
            "ATLAS_RECOVERY_PASSPHRASE",
        )
    ).strip()
    if not ENVIRONMENT_NAME.fullmatch(passphrase_env):
        raise ValueError("portable_destination.passphrase_env is invalid.")
    destination_keep = raw_destination.get("keep_last", 7)
    if not isinstance(destination_keep, int) or not 2 <= destination_keep <= 1000:
        raise ValueError(
            "portable_destination.keep_last must be between 2 and 1000."
        )
    return AutomationConfig(
        schema_version=AUTOMATION_SCHEMA_VERSION,
        inboxes=tuple(inboxes),
        backup=BackupJob(
            enabled=enabled,
            interval_minutes=interval,
            keep_last=keep_last,
            apply_retention=apply_retention,
        ),
        portable_destination=PortableDestinationJob(
            enabled=destination_enabled,
            destination_id=destination_id,
            kind=destination_kind,
            path=destination_path,
            passphrase_env=passphrase_env,
            keep_last=destination_keep,
            apply_retention=destination_retention,
        ),
    )


class AutomationStore:
    def __init__(self, database: str | Path) -> None:
        database_path = Path(database).resolve()
        self.directory = database_path.with_suffix(
            database_path.suffix + ".automation"
        )
        self.receipts_path = self.directory / "receipts.jsonl"
        self.status_path = self.directory / "status.json"
        self.lock_path = self.directory / "runner.lock"

    @staticmethod
    def _event(payload: dict[str, Any]) -> AutomationEvent:
        detail = payload.get("detail", {})
        if not isinstance(detail, dict):
            detail = {}
        return AutomationEvent(
            event_id=str(payload.get("event_id", "")),
            run_id=str(payload.get("run_id", "")),
            event_type=str(payload.get("event_type", "")),
            status=str(payload.get("status", "")),
            occurred_at=str(payload.get("occurred_at", "")),
            job_id=str(payload.get("job_id", "")),
            source_name=str(payload.get("source_name", "")),
            sha256=str(payload.get("sha256", "")),
            detail=detail,
            error=str(payload.get("error", "")),
        )

    @classmethod
    def _run(cls, payload: dict[str, Any]) -> AutomationRunReceipt:
        events = payload.get("events", [])
        return AutomationRunReceipt(
            run_id=str(payload["run_id"]),
            started_at=str(payload["started_at"]),
            completed_at=str(payload["completed_at"]),
            status=str(payload["status"]),
            imported_files=int(payload["imported_files"]),
            rejected_files=int(payload["rejected_files"]),
            replayed_files=int(payload["replayed_files"]),
            skipped_files=int(payload["skipped_files"]),
            backup_created=bool(payload["backup_created"]),
            portable_exported=bool(payload.get("portable_exported", False)),
            events=tuple(
                cls._event(event)
                for event in events
                if isinstance(event, dict)
            ),
        )

    def acquire(self, *, config_path: Path) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        metadata = json.dumps(
            {
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "config_path": str(config_path),
                "acquired_at": utc_now(),
            },
            sort_keys=True,
        ).encode()
        try:
            descriptor = os.open(
                self.lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError as error:
            raise RuntimeError(
                f"Another Atlas automation runner owns {self.lock_path}."
            ) from error
        try:
            os.write(descriptor, metadata)
        finally:
            os.close(descriptor)

    def release(self) -> None:
        self.lock_path.unlink(missing_ok=True)

    def append_event(self, event: AutomationEvent) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        line = json.dumps(asdict(event), sort_keys=True, separators=(",", ":"))
        with self.receipts_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def save_run(self, receipt: AutomationRunReceipt) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.status_path.with_suffix(".json.partial")
        temporary.write_text(
            json.dumps(asdict(receipt), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(self.status_path)

    def last_run(self) -> AutomationRunReceipt | None:
        if not self.status_path.is_file():
            return None
        try:
            payload = json.loads(self.status_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
            return self._run(payload)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, ValueError):
            return None

    def recent_events(self, limit: int = 20) -> tuple[AutomationEvent, ...]:
        if not 1 <= limit <= MAX_RECEIPTS:
            raise ValueError(f"limit must be between 1 and {MAX_RECEIPTS}.")
        if not self.receipts_path.is_file():
            return ()
        events: list[AutomationEvent] = []
        try:
            with self.receipts_path.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict):
                        events.append(self._event(payload))
        except (OSError, UnicodeDecodeError):
            return ()
        return tuple(events[-limit:][::-1])

    def status(self, *, config_path: str | Path = "") -> AutomationStatus:
        path = str(Path(config_path).resolve()) if config_path else ""
        return AutomationStatus(
            configured=bool(path and Path(path).is_file()),
            config_path=path,
            state_directory=str(self.directory),
            locked=self.lock_path.is_file(),
            last_run=self.last_run(),
            recent_events=self.recent_events(),
        )


class AtlasAutomationRunner:
    def __init__(
        self,
        *,
        database: str | Path,
        config_path: str | Path,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = Path(database).resolve()
        self.config_path = Path(config_path).resolve()
        self.config = load_automation_config(self.config_path)
        self.store = AutomationStore(self.database)
        self.clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _engine(database: Path) -> AtlasCore:
        return AtlasCore(
            database,
            (
                HotelAdapter(),
                ApartmentAdapter(),
                ConstructionAdapter(),
                ManufacturingAdapter(),
                HealthcareAdapter(),
            ),
        )

    @staticmethod
    def _event_id(run_id: str, index: int) -> str:
        return f"autoevt_{hashlib.sha256(f'{run_id}:{index}'.encode()).hexdigest()[:20]}"

    @staticmethod
    def _run_id(started_at: str) -> str:
        seed = f"{started_at}:{os.getpid()}:{time.monotonic_ns()}"
        return f"autorun_{hashlib.sha256(seed.encode()).hexdigest()[:20]}"

    @staticmethod
    def _file_bytes(path: Path) -> bytes:
        if path.is_symlink() or not path.is_file():
            raise ValueError("Inbox entries must be regular files, not links.")
        size = path.stat().st_size
        if not 0 < size <= MAX_FILE_BYTES:
            raise ValueError(
                f"Inbox file must be between 1 and {MAX_FILE_BYTES} bytes."
            )
        return path.read_bytes()

    @staticmethod
    def _archive_path(directory: Path, source: Path, digest: str) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        stem = source.stem[:120]
        return directory / f"{stem}.{digest[:12]}.{stamp}{source.suffix.lower()}"

    def _record(
        self,
        events: list[AutomationEvent],
        *,
        run_id: str,
        event_type: str,
        status: str,
        job_id: str = "",
        source_name: str = "",
        sha256: str = "",
        detail: dict[str, Any] | None = None,
        error: str = "",
    ) -> AutomationEvent:
        event = AutomationEvent(
            event_id=self._event_id(run_id, len(events)),
            run_id=run_id,
            event_type=event_type,
            status=status,
            occurred_at=self.clock().astimezone(UTC).isoformat(),
            job_id=job_id,
            source_name=source_name,
            sha256=sha256,
            detail=detail or {},
            error=error,
        )
        events.append(event)
        self.store.append_event(event)
        return event

    def _process_inbox(
        self,
        engine: AtlasCore,
        job: InboxJob,
        *,
        run_id: str,
        events: list[AutomationEvent],
    ) -> tuple[int, int, int, int]:
        job.path.mkdir(parents=True, exist_ok=True)
        processed = job.path / "processed"
        rejected = job.path / "rejected"
        processed.mkdir(exist_ok=True)
        rejected.mkdir(exist_ok=True)
        imported = rejected_count = replayed = skipped = 0
        files = sorted(
            (
                path
                for path in job.path.iterdir()
                if path.name[0:1] != "."
                and path.suffix.lower() in {".jsonl", ".ndjson", ".csv"}
            ),
            key=lambda path: path.name.casefold(),
        )
        for source in files:
            digest = ""
            try:
                content = self._file_bytes(source)
                digest = hashlib.sha256(content).hexdigest()
                format_name = (
                    "csv" if source.suffix.lower() == ".csv" else "jsonl"
                )
                preview = preview_file(
                    content,
                    format=format_name,
                    source_name=source.name,
                )
                connector = FileSnapshotConnector(
                    connector_id=job.connector_id,
                    domain=job.domain,
                    source_system=job.source_system,
                    preview=preview,
                )
                receipt = engine.import_batch(
                    connector,
                    tenant_id=job.tenant_id,
                    external_batch_id=f"file_{preview.sha256[:24]}",
                )
                if hashlib.sha256(self._file_bytes(source)).hexdigest() != digest:
                    skipped += 1
                    self._record(
                        events,
                        run_id=run_id,
                        event_type="inbox.file",
                        status="changed_after_read",
                        job_id=job.job_id,
                        source_name=source.name,
                        sha256=digest,
                        detail={"batch_id": receipt.batch_id},
                    )
                    continue
                destination = self._archive_path(processed, source, digest)
                source.replace(destination)
                if receipt.replayed:
                    replayed += 1
                    outcome = "replayed"
                else:
                    imported += 1
                    outcome = "imported"
                self._record(
                    events,
                    run_id=run_id,
                    event_type="inbox.file",
                    status=outcome,
                    job_id=job.job_id,
                    source_name=source.name,
                    sha256=digest,
                    detail={
                        "batch_id": receipt.batch_id,
                        "record_count": receipt.record_count,
                        "imported_count": receipt.imported_count,
                        "duplicate_count": receipt.duplicate_count,
                        "archive_name": destination.name,
                    },
                )
            except Exception as error:
                rejected_count += 1
                error_text = _plain_error(error)
                destination_name = ""
                try:
                    if source.exists() and source.is_file() and not source.is_symlink():
                        destination = self._archive_path(
                            rejected,
                            source,
                            digest or ("0" * 64),
                        )
                        source.replace(destination)
                        destination_name = destination.name
                except Exception:
                    destination_name = ""
                self._record(
                    events,
                    run_id=run_id,
                    event_type="inbox.file",
                    status="rejected",
                    job_id=job.job_id,
                    source_name=source.name,
                    sha256=digest,
                    detail={"archive_name": destination_name},
                    error=error_text,
                )
        return imported, rejected_count, replayed, skipped

    def _backup_due(self, now: datetime) -> bool:
        for event in self.store.recent_events(MAX_RECEIPTS):
            if event.event_type == "backup.create" and event.status == "created":
                try:
                    previous = datetime.fromisoformat(event.occurred_at)
                except ValueError:
                    return True
                return now >= previous + timedelta(
                    minutes=self.config.backup.interval_minutes
                )
        return True

    def _destination(self) -> LocalFolderRecoveryDestination | None:
        job = self.config.portable_destination
        if not job.enabled:
            return None
        if job.path is None:
            raise ValueError("Portable recovery destination path is missing.")
        return LocalFolderRecoveryDestination(
            destination_id=job.destination_id,
            path=job.path,
        )

    def _portable_passphrase(self) -> str:
        name = self.config.portable_destination.passphrase_env
        passphrase = os.environ.get(name)
        if passphrase is None:
            raise ValueError(
                f"Portable recovery requires the {name} environment variable."
            )
        return passphrase

    def portable_receipts(self) -> tuple[PortableRecoveryReceipt, ...]:
        destination = self._destination()
        return destination.list() if destination else ()

    def import_portable(
        self,
        export_id: str,
        *,
        confirmation: str,
    ) -> BackupReceipt:
        required = f"IMPORT PORTABLE {export_id}"
        if confirmation != required:
            raise ValueError(f"Import confirmation must be exactly: {required}")
        destination = self._destination()
        if destination is None:
            raise ValueError("Portable recovery destination is not enabled.")
        passphrase = self._portable_passphrase()
        _, artifact = destination.read(export_id, passphrase=passphrase)
        return self._engine(self.database).memory.import_encrypted_backup(
            artifact,
            passphrase=passphrase,
        )

    def _export_latest_backup(
        self,
        engine: AtlasCore,
        *,
        run_id: str,
        events: list[AutomationEvent],
    ) -> bool:
        destination = self._destination()
        if destination is None:
            return False
        verified = tuple(
            receipt
            for receipt in engine.memory.list_backups()
            if receipt.status == "verified"
        )
        if not verified:
            raise ValueError("No verified backup is available for portable export.")
        backup = verified[0]
        if any(
            receipt.status == "verified" and receipt.backup_id == backup.backup_id
            for receipt in destination.list()
        ):
            return False
        passphrase = self._portable_passphrase()
        artifact = engine.memory.export_encrypted_backup(
            backup.backup_id,
            passphrase=passphrase,
        )
        stored = destination.store(artifact, passphrase=passphrase)
        job = self.config.portable_destination
        retention = destination.prune(
            keep_last=job.keep_last,
            dry_run=not job.apply_retention,
            confirmation=(
                f"PRUNE PORTABLE DESTINATION {job.destination_id} "
                f"KEEP {job.keep_last}"
                if job.apply_retention
                else ""
            ),
        )
        self._record(
            events,
            run_id=run_id,
            event_type="backup.portable",
            status="verified",
            detail={
                "destination_id": stored.destination_id,
                "destination_kind": stored.destination_kind,
                "export_id": stored.export_id,
                "backup_id": stored.backup_id,
                "filename": stored.filename,
                "sha256": stored.sha256,
                "size_bytes": stored.size_bytes,
                "cipher": stored.cipher,
                "kdf": stored.kdf,
                "retention_candidates": list(
                    retention.candidate_export_ids
                ),
                "retention_deleted": list(retention.deleted_export_ids),
            },
        )
        return True

    def run_once(self) -> AutomationRunReceipt:
        started = self.clock().astimezone(UTC)
        started_at = started.isoformat()
        run_id = self._run_id(started_at)
        events: list[AutomationEvent] = []
        imported = rejected = replayed = skipped = 0
        backup_created = False
        portable_exported = False
        self.store.acquire(config_path=self.config_path)
        try:
            engine = self._engine(self.database)
            for job in self.config.inboxes:
                result = self._process_inbox(
                    engine,
                    job,
                    run_id=run_id,
                    events=events,
                )
                imported += result[0]
                rejected += result[1]
                replayed += result[2]
                skipped += result[3]
            if self.config.backup.enabled and self._backup_due(started):
                try:
                    receipt = engine.memory.create_backup()
                    backup_created = True
                    detail: dict[str, Any] = {
                        "backup_id": receipt.backup_id,
                        "sha256": receipt.sha256,
                        "size_bytes": receipt.size_bytes,
                    }
                    retention = engine.memory.prune_backups(
                        keep_last=self.config.backup.keep_last,
                        dry_run=not self.config.backup.apply_retention,
                        confirmation=(
                            f"PRUNE BACKUPS KEEP {self.config.backup.keep_last}"
                            if self.config.backup.apply_retention
                            else ""
                        ),
                    )
                    detail["retention_candidates"] = list(
                        retention.candidate_backup_ids
                    )
                    detail["retention_deleted"] = list(
                        retention.deleted_backup_ids
                    )
                    self._record(
                        events,
                        run_id=run_id,
                        event_type="backup.create",
                        status="created",
                        detail=detail,
                    )
                except Exception as error:
                    self._record(
                        events,
                        run_id=run_id,
                        event_type="backup.create",
                        status="failed",
                        error=_plain_error(error),
                    )
            if self.config.portable_destination.enabled:
                try:
                    portable_exported = self._export_latest_backup(
                        engine,
                        run_id=run_id,
                        events=events,
                    )
                except Exception as error:
                    self._record(
                        events,
                        run_id=run_id,
                        event_type="backup.portable",
                        status="failed",
                        detail={
                            "destination_id": (
                                self.config.portable_destination.destination_id
                            ),
                            "destination_kind": (
                                self.config.portable_destination.kind
                            ),
                        },
                        error=_plain_error(error),
                    )
            failed = rejected > 0 or skipped > 0 or any(
                event.status == "failed" for event in events
            )
            receipt = AutomationRunReceipt(
                run_id=run_id,
                started_at=started_at,
                completed_at=self.clock().astimezone(UTC).isoformat(),
                status="attention_required" if failed else "completed",
                imported_files=imported,
                rejected_files=rejected,
                replayed_files=replayed,
                skipped_files=skipped,
                backup_created=backup_created,
                portable_exported=portable_exported,
                events=tuple(events),
            )
            self.store.save_run(receipt)
            return receipt
        finally:
            self.store.release()

    def run_forever(self, *, poll_seconds: int = 60) -> None:
        if not 5 <= poll_seconds <= 3600:
            raise ValueError("poll_seconds must be between 5 and 3600.")
        while True:
            self.run_once()
            time.sleep(poll_seconds)
