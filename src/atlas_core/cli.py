from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .adapters import (
    ApartmentAdapter,
    ConstructionAdapter,
    HealthcareAdapter,
    HotelAdapter,
    ManufacturingAdapter,
)
from .automation import AtlasAutomationRunner, AutomationStore
from .file_connectors import (
    MAX_FILE_BYTES,
    FileSnapshotConnector,
    preview_file,
)
from .engine import AtlasCore


def run_demo(database: Path) -> dict[str, object]:
    engine = AtlasCore(database, (HotelAdapter(),))
    first = {
        "reservation_id": "RES-1042",
        "version": "1",
        "updated_at": "2026-07-24T12:00:00Z",
        "arrival_date": "2026-08-10",
        "departure_date": "2026-08-13",
        "guest_count": 2,
        "room_type": "king",
        "status": "confirmed",
        "total": "729.00",
        "currency": "USD",
    }
    second = {
        **first,
        "version": "2",
        "updated_at": "2026-07-24T12:05:00Z",
        "status": "cancelled",
        "total": "0.00",
    }
    engine.ingest("hotel", first, tenant_id="demo-hotel", source_system="synthetic-pms")
    result = engine.ingest(
        "hotel",
        second,
        tenant_id="demo-hotel",
        source_system="synthetic-pms",
    )
    return {
        "question": "What changed, why did it change, and what should I do next?",
        "answer": {
            "what_changed": result.changes.summary,
            "changes": [asdict(change) for change in result.changes.changes],
            "why": "The synthetic PMS emitted reservation version 2.",
            "next": [asdict(proposal) for proposal in result.proposals],
            "approval_state": "pending human review",
        },
        "audit_event_count": len(engine.memory.audit_events()),
        "database": str(database.resolve()),
    }


def _adapters() -> tuple[object, ...]:
    return (
        HotelAdapter(),
        ApartmentAdapter(),
        ConstructionAdapter(),
        ManufacturingAdapter(),
        HealthcareAdapter(),
    )


def _file_bytes(path: Path) -> bytes:
    if not path.is_file():
        raise ValueError(f"Import file does not exist: {path}")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"Import file may not exceed {MAX_FILE_BYTES} bytes.")
    return path.read_bytes()


def main() -> None:
    parser = argparse.ArgumentParser(prog="atlas-core")
    subparsers = parser.add_subparsers(dest="command", required=True)
    demo = subparsers.add_parser("demo", help="Run the synthetic hotel vertical slice.")
    demo.add_argument("--database", type=Path, default=Path("runtime/atlas-core.db"))
    file_parser = subparsers.add_parser(
        "import-file",
        help="Preview or import a bounded local JSONL or CSV snapshot.",
    )
    file_parser.add_argument("file", type=Path)
    file_parser.add_argument("--format", choices=("jsonl", "csv"), required=True)
    file_parser.add_argument(
        "--domain",
        choices=(
            "hotel",
            "apartment",
            "construction",
            "manufacturing",
            "healthcare",
        ),
        required=True,
    )
    file_parser.add_argument("--tenant", required=True)
    file_parser.add_argument("--connector-id", required=True)
    file_parser.add_argument("--source-system", required=True)
    file_parser.add_argument("--external-batch-id")
    file_parser.add_argument(
        "--database",
        type=Path,
        default=Path("runtime/atlas-core.db"),
    )
    file_parser.add_argument(
        "--apply",
        action="store_true",
        help="Import after validation; without this flag the command is preview-only.",
    )
    automation = subparsers.add_parser(
        "automation-run",
        help="Run guarded local inbox and recovery jobs once or continuously.",
    )
    automation.add_argument("--config", type=Path, required=True)
    automation.add_argument(
        "--database",
        type=Path,
        default=Path("runtime/atlas-core.db"),
    )
    automation.add_argument(
        "--watch",
        action="store_true",
        help="Keep running locally after the first completed pass.",
    )
    automation.add_argument(
        "--poll-seconds",
        type=int,
        default=60,
        help="Polling interval for --watch (5-3600 seconds).",
    )
    automation_status = subparsers.add_parser(
        "automation-status",
        help="Read the latest unattended-operation receipt.",
    )
    automation_status.add_argument("--config", type=Path)
    automation_status.add_argument(
        "--database",
        type=Path,
        default=Path("runtime/atlas-core.db"),
    )
    portable_list = subparsers.add_parser(
        "portable-list",
        help="List verified encrypted exports in the configured destination.",
    )
    portable_list.add_argument("--config", type=Path, required=True)
    portable_list.add_argument(
        "--database",
        type=Path,
        default=Path("runtime/atlas-core.db"),
    )
    portable_import = subparsers.add_parser(
        "portable-import",
        help="Preview or register one encrypted destination export locally.",
    )
    portable_import.add_argument("export_id")
    portable_import.add_argument("--config", type=Path, required=True)
    portable_import.add_argument(
        "--database",
        type=Path,
        default=Path("runtime/atlas-core.db"),
    )
    portable_import.add_argument("--confirmation", default="")
    portable_import.add_argument(
        "--apply",
        action="store_true",
        help="Authenticate and register the export; preview is the default.",
    )
    args = parser.parse_args()
    if args.command == "demo":
        print(json.dumps(run_demo(args.database), indent=2, default=str))
    elif args.command == "import-file":
        content = _file_bytes(args.file)
        preview = preview_file(
            content,
            format=args.format,
            source_name=args.file.name,
        )
        default_batch_id = f"file_{preview.sha256[:24]}"
        output: dict[str, object] = {
            "mode": "apply" if args.apply else "preview",
            "format": preview.format,
            "source_name": preview.source_name,
            "sha256": preview.sha256,
            "size_bytes": preview.size_bytes,
            "record_count": preview.record_count,
            "fields": preview.fields,
            "external_batch_id": args.external_batch_id or default_batch_id,
        }
        if args.apply:
            engine = AtlasCore(args.database, _adapters())
            connector = FileSnapshotConnector(
                connector_id=args.connector_id,
                domain=args.domain,
                source_system=args.source_system,
                preview=preview,
            )
            receipt = engine.import_batch(
                connector,
                tenant_id=args.tenant,
                external_batch_id=args.external_batch_id or default_batch_id,
            )
            output["receipt"] = asdict(receipt)
        print(json.dumps(output, indent=2, default=str))
    elif args.command == "automation-run":
        runner = AtlasAutomationRunner(
            database=args.database,
            config_path=args.config,
        )
        if args.watch:
            try:
                runner.run_forever(poll_seconds=args.poll_seconds)
            except KeyboardInterrupt:
                return
        else:
            print(json.dumps(asdict(runner.run_once()), indent=2, default=str))
    elif args.command == "automation-status":
        status = AutomationStore(args.database).status(
            config_path=args.config or "",
        )
        print(json.dumps(asdict(status), indent=2, default=str))
    elif args.command == "portable-list":
        runner = AtlasAutomationRunner(
            database=args.database,
            config_path=args.config,
        )
        print(
            json.dumps(
                {"items": [asdict(item) for item in runner.portable_receipts()]},
                indent=2,
                default=str,
            )
        )
    elif args.command == "portable-import":
        runner = AtlasAutomationRunner(
            database=args.database,
            config_path=args.config,
        )
        receipts = {
            receipt.export_id: receipt for receipt in runner.portable_receipts()
        }
        if args.export_id not in receipts:
            raise ValueError(f"Unknown portable recovery export: {args.export_id}")
        output: dict[str, object] = {
            "mode": "apply" if args.apply else "preview",
            "export": asdict(receipts[args.export_id]),
            "required_confirmation": f"IMPORT PORTABLE {args.export_id}",
        }
        if args.apply:
            output["backup"] = asdict(
                runner.import_portable(
                    args.export_id,
                    confirmation=args.confirmation,
                )
            )
        print(json.dumps(output, indent=2, default=str))


if __name__ == "__main__":
    main()
