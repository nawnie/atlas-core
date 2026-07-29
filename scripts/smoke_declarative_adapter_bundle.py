from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from atlas_core_sdk import (
    AtlasClient,
    DeclarativeIndustryAdapter,
)


def run_json(*arguments: str) -> dict[str, object]:
    executable = Path(sys.executable).with_name(
        "atlas-provider.exe" if sys.platform == "win32" else "atlas-provider"
    )
    completed = subprocess.run(
        [str(executable), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def record(
    *,
    version: str,
    status: str,
    utilization: object,
) -> dict[str, object]:
    return {
        "asset_id": "INSTALLED-100",
        "updated_at": f"2026-07-24T21:0{version}:00Z",
        "version": version,
        "asset_name": "Installed Fixture Press",
        "status": status,
        "utilization": utilization,
    }


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="atlas-declarative-smoke-") as temporary:
        root = Path(temporary)
        spec_path = root / "asset-adapter.json"
        record_path = root / "record.json"
        created = run_json(
            "scaffold-adapter",
            "--output",
            str(spec_path),
            "--adapter-id",
            "installed-asset-v1",
            "--domain",
            "asset",
            "--entity-type",
            "equipment",
            "--id-field",
            "asset_id",
            "--observed-at-field",
            "updated_at",
            "--version-field",
            "version",
            "--field",
            "asset_name:asset_name:string:strip:required",
            "--field",
            "status:status:string:lower:required",
            "--field",
            "utilization:utilization:decimal:none:required",
            "--rule",
            "asset-review:status:changed_to:review_required:review_asset_status:medium",
        )
        inspected = run_json("inspect-adapter", str(spec_path))
        record_path.write_text(
            json.dumps(record(version="1", status="active", utilization=70)),
            encoding="utf-8",
        )
        validated = run_json(
            "validate-record",
            "--adapter",
            str(spec_path),
            "--record",
            str(record_path),
        )
        adapter = DeclarativeIndustryAdapter.from_file(spec_path)
        client = AtlasClient.local(
            database=root / "company-memory.db",
            adapters=(adapter,),
        )
        first = client.ingest(
            domain="asset",
            tenant_id="installed-company",
            source_system="installed-fixture",
            record=record(version="1", status="active", utilization=70),
        )
        second = client.ingest(
            domain="asset",
            tenant_id="installed-company",
            source_system="installed-fixture",
            record=record(
                version="2",
                status="REVIEW_REQUIRED",
                utilization="94.5",
            )
        )
        overview = client.overview(tenant_id="installed-company")
        cards = client.search_cartography(
            tenant_id="installed-company",
            lane="asset_operations",
        )
        if created["spec_sha256"] != inspected["spec_sha256"]:
            raise RuntimeError("Installed adapter spec hash changed after inspection.")
        if validated["spec_sha256"] != created["spec_sha256"]:
            raise RuntimeError("Installed validation used a different adapter spec.")
        if first.proposals or len(second.proposals) != 1:
            raise RuntimeError("Installed declarative approval behavior is invalid.")
        if overview.observations != 2 or overview.pending != 1:
            raise RuntimeError("Installed declarative Company Memory state is invalid.")
        if overview.cartography_cards != 2 or len(cards) != 2:
            raise RuntimeError("Installed declarative Cartographer state is invalid.")
        if "Installed Fixture Press" in json.dumps(validated):
            raise RuntimeError("Record values leaked into validation output.")
        print(
            json.dumps(
                {
                    "status": "passed",
                    "spec_sha256": created["spec_sha256"],
                    "mapped_fields": validated["mapped_fields"],
                    "observations": overview.observations,
                    "changes": overview.changes,
                    "pending": overview.pending,
                    "cartography_cards": overview.cartography_cards,
                    "proposal_action": second.proposals[0].action_type,
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
