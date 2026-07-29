from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from atlas_core import (
    AtlasCore,
    DeclarativeIndustryAdapter,
    declarative_adapter_spec_from_dict,
    load_declarative_adapter_spec,
)
from atlas_core_sdk.provider_cli import main
from atlas_core_sdk import DeclarativeIndustryAdapter as SdkDeclarativeIndustryAdapter


def asset_spec() -> dict[str, object]:
    return {
        "schema_version": 1,
        "adapter_id": "asset-operations-v1",
        "domain": "asset",
        "entity_type": "equipment",
        "id_field": "asset_id",
        "observed_at_field": "updated_at",
        "version_field": "version",
        "fields": [
            {
                "source": "asset_name",
                "target": "asset_name",
                "value_type": "string",
                "normalization": "strip",
            },
            {
                "source": "status",
                "target": "status",
                "value_type": "string",
                "normalization": "lower",
            },
            {
                "source": "utilization",
                "target": "utilization",
                "value_type": "decimal",
            },
            {
                "source": "needs_review",
                "target": "needs_review",
                "value_type": "boolean",
            },
        ],
        "rules": [
            {
                "rule_id": "status-review",
                "field": "status",
                "operator": "changed_to",
                "value": "review_required",
                "action_type": "review_asset_status",
                "reason": "Asset status requires a human decision.",
                "risk": "medium",
                "review_tasks": ["confirm source", "inspect asset"],
            },
            {
                "rule_id": "utilization-review",
                "field": "utilization",
                "operator": "numeric_greater_than",
                "value": "90",
                "action_type": "review_asset_utilization",
                "reason": "Asset utilization crossed the review threshold.",
                "risk": "medium",
                "review_tasks": ["confirm meter", "review maintenance window"],
            },
            {
                "rule_id": "flag-review",
                "field": "needs_review",
                "operator": "truthy",
                "action_type": "review_asset_flag",
                "reason": "Asset review flag is active.",
                "risk": "low",
                "review_tasks": ["confirm review flag"],
            },
        ],
    }


def asset_record(
    *,
    version: str,
    status: str,
    utilization: object,
    needs_review: bool,
) -> dict[str, object]:
    return {
        "asset_id": "A-100",
        "updated_at": f"2026-07-24T21:0{version}:00Z",
        "version": version,
        "asset_name": "  Line One Press  ",
        "status": status,
        "utilization": utilization,
        "needs_review": needs_review,
    }


class DeclarativeAdapterTests(unittest.TestCase):
    def test_full_path_reaches_memory_cartographer_and_pending_approval(self) -> None:
        spec = declarative_adapter_spec_from_dict(asset_spec())
        adapter = DeclarativeIndustryAdapter(spec)
        with tempfile.TemporaryDirectory() as temporary:
            engine = AtlasCore(Path(temporary) / "atlas.db", adapters=(adapter,))
            first = engine.ingest(
                "asset",
                asset_record(
                    version="1",
                    status="active",
                    utilization=80,
                    needs_review=False,
                ),
                tenant_id="factory-a",
                source_system="asset-export",
            )
            self.assertEqual(first.proposals, ())
            second = engine.ingest(
                "asset",
                asset_record(
                    version="2",
                    status="REVIEW_REQUIRED",
                    utilization="97.5",
                    needs_review=True,
                ),
                tenant_id="factory-a",
                source_system="asset-export",
            )
            self.assertEqual(second.observation.attributes["status"], "review_required")
            self.assertEqual(second.observation.attributes["utilization"], "97.50")
            self.assertEqual(len(second.proposals), 3)
            stored = engine.memory.list_proposals(
                tenant_id="factory-a",
                status="pending",
            )
            self.assertTrue(
                all(proposal.status == "pending" for proposal in stored)
            )
            cards = engine.cartographer.search(
                tenant_id="factory-a",
                lane="asset_operations",
            )
            self.assertEqual(len(cards), 2)
            self.assertIn("DeclarativeIndustryAdapter", cards[0].details)
            self.assertIn('"status": "attributes.status"', cards[0].details)
            overview = engine.memory.overview(tenant_id="factory-a")
            self.assertEqual(overview["observations"], 2)
            self.assertEqual(overview["pending"], 3)
            self.assertEqual(overview["cartography_cards"], 2)

    def test_unknown_source_fields_are_denied_before_storage(self) -> None:
        adapter = DeclarativeIndustryAdapter(
            declarative_adapter_spec_from_dict(asset_spec())
        )
        with tempfile.TemporaryDirectory() as temporary:
            engine = AtlasCore(Path(temporary) / "atlas.db", adapters=(adapter,))
            record = asset_record(
                version="1",
                status="active",
                utilization=80,
                needs_review=False,
            )
            record["unmapped_note"] = "must not enter memory"
            with self.assertRaisesRegex(ValueError, "unmapped"):
                engine.ingest(
                    "asset",
                    record,
                    tenant_id="factory-a",
                    source_system="asset-export",
                )
            self.assertEqual(
                engine.memory.overview(tenant_id="factory-a")["observations"],
                0,
            )

    def test_sensitive_fields_require_a_reviewed_coded_adapter(self) -> None:
        payload = asset_spec()
        payload["fields"] = [
            {
                "source": "patient_name",
                "target": "operator",
                "value_type": "string",
            }
        ]
        with self.assertRaisesRegex(ValueError, "values are invalid"):
            declarative_adapter_spec_from_dict(payload)
        healthcare = asset_spec()
        healthcare["domain"] = "healthcare"
        with self.assertRaisesRegex(ValueError, "values are invalid"):
            declarative_adapter_spec_from_dict(healthcare)

    def test_strict_types_timezone_and_finite_numbers(self) -> None:
        adapter = DeclarativeIndustryAdapter(
            declarative_adapter_spec_from_dict(asset_spec())
        )
        base = asset_record(
            version="1",
            status="active",
            utilization=80,
            needs_review=False,
        )
        for field, value in (
            ("needs_review", "false"),
            ("utilization", "NaN"),
            ("updated_at", "2026-07-24T21:00:00"),
        ):
            record = {**base, field: value}
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    adapter.translate(
                        record,
                        tenant_id="factory-a",
                        source_system="asset-export",
                    )

    def test_loader_is_bounded_and_rejects_unknown_spec_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "adapter.json"
            path.write_text(
                json.dumps({**asset_spec(), "credential": "not-allowed"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "unknown"):
                load_declarative_adapter_spec(path)
            path.write_text("x" * (64 * 1024 + 1), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "64 KiB"):
                load_declarative_adapter_spec(path)

    def test_loader_rejects_duplicate_keys_and_non_string_identifiers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "adapter.json"
            rendered = json.dumps(asset_spec())
            path.write_text(
                rendered.replace(
                    '"adapter_id": "asset-operations-v1"',
                    '"adapter_id": "first", "adapter_id": "second"',
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate"):
                load_declarative_adapter_spec(path)
            payload = asset_spec()
            payload["adapter_id"] = None
            with self.assertRaisesRegex(ValueError, "values are invalid"):
                declarative_adapter_spec_from_dict(payload)

    def test_official_sdk_exports_declarative_adapter_contract(self) -> None:
        spec = declarative_adapter_spec_from_dict(asset_spec())
        adapter = SdkDeclarativeIndustryAdapter(spec)
        self.assertEqual(adapter.domain, "asset")
        self.assertEqual(adapter.spec.sha256(), spec.sha256())

    def test_cli_scaffolds_inspects_and_validates_without_record_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec_path = root / "asset-adapter.json"
            record_path = root / "record.json"
            scaffold_output = io.StringIO()
            with contextlib.redirect_stdout(scaffold_output):
                status = main(
                    (
                        "scaffold-adapter",
                        "--output",
                        str(spec_path),
                        "--adapter-id",
                        "asset-operations-v1",
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
                        "--field",
                        "needs_review:needs_review:boolean:none:required",
                        "--rule",
                        "status-review:status:changed_to:review_required:review_asset_status:medium",
                    )
                )
            self.assertEqual(status, 0)
            created = json.loads(scaffold_output.getvalue())
            self.assertEqual(created["mapped_fields"], 4)
            self.assertEqual(created["approval_rules"], 1)

            inspect_output = io.StringIO()
            with contextlib.redirect_stdout(inspect_output):
                self.assertEqual(main(("inspect-adapter", str(spec_path))), 0)
            inspected = json.loads(inspect_output.getvalue())
            self.assertEqual(inspected["spec_sha256"], created["spec_sha256"])

            record = asset_record(
                version="1",
                status="active",
                utilization=80,
                needs_review=False,
            )
            record["asset_name"] = "PRIVATE FIXTURE VALUE"
            record_path.write_text(json.dumps(record), encoding="utf-8")
            validate_output = io.StringIO()
            with contextlib.redirect_stdout(validate_output):
                self.assertEqual(
                    main(
                        (
                            "validate-record",
                            "--adapter",
                            str(spec_path),
                            "--record",
                            str(record_path),
                        )
                    ),
                    0,
                )
            rendered = validate_output.getvalue()
            receipt = json.loads(rendered)
            self.assertEqual(receipt["status"], "valid")
            self.assertEqual(receipt["mapped_fields"], [
                "asset_name",
                "needs_review",
                "status",
                "utilization",
            ])
            self.assertNotIn("PRIVATE FIXTURE VALUE", rendered)

    def test_cli_record_failure_is_bounded_and_payload_free(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec_path = root / "adapter.json"
            record_path = root / "record.json"
            spec_path.write_text(json.dumps(asset_spec()), encoding="utf-8")
            record = asset_record(
                version="1",
                status="active",
                utilization=80,
                needs_review=False,
            )
            record["owner_email"] = "private@example.invalid"
            record_path.write_text(json.dumps(record), encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as raised:
                    main(
                        (
                            "validate-record",
                            "--adapter",
                            str(spec_path),
                            "--record",
                            str(record_path),
                        )
                    )
            self.assertEqual(raised.exception.code, 2)
            self.assertNotIn("private@example.invalid", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
