from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from .models import EncryptedBackupArtifact, utc_now
from .recovery import MAX_ARTIFACT_BYTES, decrypt_recovery_artifact

DESTINATION_SCHEMA_VERSION = 1
DESTINATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
EXPORT_ID = re.compile(r"^exp_[0-9a-f]{20}$")


@dataclass(frozen=True)
class PortableRecoveryReceipt:
    destination_id: str
    destination_kind: str
    export_id: str
    backup_id: str
    created_at: str
    filename: str
    size_bytes: int
    sha256: str
    cipher: str
    kdf: str
    status: str = "verified"
    replayed: bool = False
    error: str = ""


@dataclass(frozen=True)
class PortableRetentionReceipt:
    destination_id: str
    keep_last: int
    candidate_export_ids: tuple[str, ...]
    deleted_export_ids: tuple[str, ...]
    reclaimed_bytes: int
    dry_run: bool
    applied_at: str


class LocalFolderRecoveryDestination:
    """Atomic encrypted recovery storage in one explicitly configured folder."""

    def __init__(self, *, destination_id: str, path: str | Path) -> None:
        destination_id = destination_id.strip()
        if not DESTINATION_ID.fullmatch(destination_id):
            raise ValueError("destination_id is invalid.")
        self.destination_id = destination_id
        self.path = Path(os.path.abspath(path))

    @staticmethod
    def _unsafe_link(path: Path) -> bool:
        return path.is_symlink() or (
            hasattr(path, "is_junction") and path.is_junction()
        )

    def _ensure_directory(self) -> None:
        if self.path == Path(self.path.anchor):
            raise ValueError(
                "Portable recovery destination must not be a filesystem root."
            )
        if self.path.exists() and (
            not self.path.is_dir() or self._unsafe_link(self.path)
        ):
            raise ValueError(
                "Portable recovery destination must be a real local directory."
            )
        self.path.mkdir(parents=True, exist_ok=True)
        if self._unsafe_link(self.path):
            raise ValueError(
                "Portable recovery destination must not be a link or junction."
            )

    @staticmethod
    def _sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _manifest(receipt: PortableRecoveryReceipt) -> dict[str, object]:
        return {
            "schema_version": DESTINATION_SCHEMA_VERSION,
            **asdict(receipt),
        }

    def _paths(self, export_id: str) -> tuple[Path, Path]:
        if not EXPORT_ID.fullmatch(export_id):
            raise ValueError("Invalid portable recovery export identifier.")
        return (
            self.path / f"{export_id}.atlasbackup",
            self.path / f"{export_id}.json",
        )

    def _verified(self, export_id: str) -> PortableRecoveryReceipt:
        artifact_path, manifest_path = self._paths(export_id)
        if not manifest_path.is_file():
            raise KeyError(f"Unknown portable recovery export: {export_id}")
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if payload.pop("schema_version") != DESTINATION_SCHEMA_VERSION:
                raise ValueError
            receipt = PortableRecoveryReceipt(**payload)
        except (
            KeyError,
            OSError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise ValueError("Portable recovery manifest is invalid.") from error
        if (
            receipt.destination_id != self.destination_id
            or receipt.destination_kind != "local_folder"
            or receipt.export_id != export_id
            or receipt.filename != artifact_path.name
            or receipt.status != "verified"
            or receipt.error
        ):
            raise ValueError("Portable recovery manifest identity is invalid.")
        if not artifact_path.is_file() or self._unsafe_link(artifact_path):
            raise ValueError("Portable recovery artifact is missing or linked.")
        if not 0 < artifact_path.stat().st_size <= MAX_ARTIFACT_BYTES:
            raise ValueError("Portable recovery artifact size is invalid.")
        if artifact_path.stat().st_size != receipt.size_bytes:
            raise ValueError("Portable recovery artifact size does not match.")
        if self._file_sha256(artifact_path) != receipt.sha256:
            raise ValueError("Portable recovery artifact checksum does not match.")
        return receipt

    def store(
        self,
        artifact: EncryptedBackupArtifact,
        *,
        passphrase: str,
    ) -> PortableRecoveryReceipt:
        self._ensure_directory()
        artifact_path, manifest_path = self._paths(artifact.export_id)
        if artifact_path.exists() or manifest_path.exists():
            existing = self._verified(artifact.export_id)
            if (
                existing.backup_id != artifact.backup_id
                or existing.sha256 != artifact.sha256
            ):
                raise ValueError(
                    "Portable recovery export conflicts with existing content."
                )
            return PortableRecoveryReceipt(**{**asdict(existing), "replayed": True})
        partial_artifact = self.path / f".{artifact.export_id}.atlasbackup.partial"
        partial_manifest = self.path / f".{artifact.export_id}.json.partial"
        if partial_artifact.exists() or partial_manifest.exists():
            raise RuntimeError(
                "A partial portable recovery export requires operator inspection."
            )
        try:
            with partial_artifact.open("xb") as stream:
                stream.write(artifact.data)
                stream.flush()
                os.fsync(stream.fileno())
            if self._file_sha256(partial_artifact) != artifact.sha256:
                raise ValueError(
                    "Portable recovery artifact checksum changed during write."
                )
            header, plaintext = decrypt_recovery_artifact(
                partial_artifact.read_bytes(),
                passphrase=passphrase,
            )
            if (
                header.get("backup_id") != artifact.backup_id
                or hashlib.sha256(plaintext).hexdigest()
                != header.get("plaintext_sha256")
            ):
                raise ValueError(
                    "Portable recovery read-back metadata does not match."
                )
            receipt = PortableRecoveryReceipt(
                destination_id=self.destination_id,
                destination_kind="local_folder",
                export_id=artifact.export_id,
                backup_id=artifact.backup_id,
                created_at=utc_now(),
                filename=artifact_path.name,
                size_bytes=len(artifact.data),
                sha256=artifact.sha256,
                cipher=artifact.cipher,
                kdf=artifact.kdf,
            )
            with partial_manifest.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(
                    self._manifest(receipt),
                    stream,
                    indent=2,
                    sort_keys=True,
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            partial_artifact.replace(artifact_path)
            partial_manifest.replace(manifest_path)
            return self._verified(artifact.export_id)
        except Exception:
            partial_artifact.unlink(missing_ok=True)
            partial_manifest.unlink(missing_ok=True)
            raise

    def list(self) -> tuple[PortableRecoveryReceipt, ...]:
        if not self.path.is_dir() or self._unsafe_link(self.path):
            return ()
        receipts: list[PortableRecoveryReceipt] = []
        for manifest in self.path.glob("exp_*.json"):
            try:
                receipts.append(self._verified(manifest.stem))
            except (KeyError, OSError, ValueError) as error:
                receipts.append(
                    PortableRecoveryReceipt(
                        destination_id=self.destination_id,
                        destination_kind="local_folder",
                        export_id=manifest.stem,
                        backup_id="",
                        created_at="",
                        filename=f"{manifest.stem}.atlasbackup",
                        size_bytes=0,
                        sha256="",
                        cipher="",
                        kdf="",
                        status="invalid",
                        error=str(error)[:500],
                    )
                )
        return tuple(
            sorted(receipts, key=lambda receipt: receipt.created_at, reverse=True)
        )

    def read(
        self,
        export_id: str,
        *,
        passphrase: str,
    ) -> tuple[PortableRecoveryReceipt, bytes]:
        receipt = self._verified(export_id)
        artifact_path, _ = self._paths(export_id)
        data = artifact_path.read_bytes()
        header, plaintext = decrypt_recovery_artifact(
            data,
            passphrase=passphrase,
        )
        if (
            header.get("backup_id") != receipt.backup_id
            or hashlib.sha256(plaintext).hexdigest()
            != header.get("plaintext_sha256")
        ):
            raise ValueError("Portable recovery read-back metadata does not match.")
        return receipt, data

    def prune(
        self,
        *,
        keep_last: int,
        confirmation: str = "",
        dry_run: bool = True,
    ) -> PortableRetentionReceipt:
        if not 2 <= keep_last <= 1000:
            raise ValueError("Portable destination keep_last must be 2-1000.")
        required = (
            f"PRUNE PORTABLE DESTINATION {self.destination_id} KEEP {keep_last}"
        )
        if not dry_run and confirmation != required:
            raise ValueError(
                f"Portable prune confirmation must be exactly: {required}"
            )
        verified = tuple(
            receipt for receipt in self.list() if receipt.status == "verified"
        )
        candidates = verified[keep_last:]
        deleted: list[str] = []
        reclaimed = sum(receipt.size_bytes for receipt in candidates)
        if not dry_run:
            for receipt in candidates:
                artifact_path, manifest_path = self._paths(receipt.export_id)
                artifact_path.unlink()
                manifest_path.unlink()
                deleted.append(receipt.export_id)
        return PortableRetentionReceipt(
            destination_id=self.destination_id,
            keep_last=keep_last,
            candidate_export_ids=tuple(
                receipt.export_id for receipt in candidates
            ),
            deleted_export_ids=tuple(deleted),
            reclaimed_bytes=reclaimed,
            dry_run=dry_run,
            applied_at=utc_now(),
        )
