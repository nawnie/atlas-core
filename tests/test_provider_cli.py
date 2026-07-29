from __future__ import annotations

import contextlib
import importlib
import io
import json
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from atlas_core_sdk.provider_cli import main


class ProviderCliTests(unittest.TestCase):
    def run_cli(self, *arguments: str) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = main(arguments)
        return status, json.loads(output.getvalue())

    def scaffold(self, root: Path) -> tuple[Path, dict[str, object]]:
        project = root / "demo-provider"
        status, payload = self.run_cli(
            "scaffold",
            "--output",
            str(project),
            "--provider-id",
            "demo-operations-sandbox",
            "--display-name",
            "Demo Operations Sandbox",
            "--domain",
            "demo_operations",
            "--action",
            "review_record",
        )
        self.assertEqual(status, 0)
        return project, payload

    def test_scaffold_manifest_and_generated_conformance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, payload = self.scaffold(root)
            self.assertEqual(payload["status"], "created")
            self.assertEqual(
                payload["factory"],
                "demo_operations_sandbox:create_provider",
            )
            manifest_status, manifest_payload = self.run_cli(
                "inspect-manifest",
                str(project / "atlas-provider.json"),
            )
            self.assertEqual(manifest_status, 0)
            manifest = manifest_payload["manifest"]
            self.assertEqual(manifest["environment"], "local_sandbox")
            self.assertFalse(manifest["network_access"])
            self.assertEqual(manifest["credential_strategy"], "none")
            with (project / "pyproject.toml").open("rb") as handle:
                metadata = tomllib.load(handle)
            self.assertEqual(metadata["project"]["requires-python"], ">=3.12")

            source = str(project / "src")
            sys.path.insert(0, source)
            try:
                importlib.invalidate_caches()
                status, evidence = self.run_cli(
                    "certify",
                    "--factory",
                    "demo_operations_sandbox:create_provider",
                    "--dynamic",
                )
            finally:
                sys.path.remove(source)
                for name in tuple(sys.modules):
                    if name.startswith("demo_operations_sandbox"):
                        sys.modules.pop(name, None)
            self.assertEqual(status, 0)
            self.assertEqual(evidence["mode"], "dynamic_local_sandbox")
            self.assertEqual(
                evidence["receipt"]["status"],
                "sandbox_certified",
            )
            self.assertTrue(
                all(
                    check["status"] == "passed"
                    for check in evidence["receipt"]["checks"]
                )
            )

    def test_scaffold_refuses_existing_path_without_changing_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "existing"
            project.mkdir()
            marker = project / "keep.txt"
            marker.write_text("owned by user", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main(
                        (
                            "scaffold",
                            "--output",
                            str(project),
                            "--provider-id",
                            "safe-sandbox",
                            "--display-name",
                            "Safe Sandbox",
                            "--domain",
                            "safe",
                            "--action",
                            "review",
                        )
                    )
            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(marker.read_text(encoding="utf-8"), "owned by user")
            self.assertEqual(tuple(project.iterdir()), (marker,))

    def test_certification_evidence_is_exclusive_and_payload_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, _ = self.scaffold(root)
            evidence_path = root / "certification.json"
            source = str(project / "src")
            sys.path.insert(0, source)
            try:
                importlib.invalidate_caches()
                status, payload = self.run_cli(
                    "certify",
                    "--factory",
                    "demo_operations_sandbox:create_provider",
                    "--dynamic",
                    "--evidence",
                    str(evidence_path),
                )
                self.assertEqual(status, 0)
                self.assertEqual(
                    json.loads(evidence_path.read_text(encoding="utf-8")),
                    payload,
                )
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        main(
                            (
                                "certify",
                                "--factory",
                                "demo_operations_sandbox:create_provider",
                                "--dynamic",
                                "--evidence",
                                str(evidence_path),
                            )
                        )
                self.assertEqual(raised.exception.code, 2)
            finally:
                sys.path.remove(source)
                for name in tuple(sys.modules):
                    if name.startswith("demo_operations_sandbox"):
                        sys.modules.pop(name, None)
            encoded = evidence_path.read_text(encoding="utf-8").lower()
            self.assertNotIn("password", encoded)
            self.assertNotIn("api_key", encoded)
            self.assertNotIn("endpoint_hosts", encoded)

    def test_manifest_rejects_unknown_fields_and_non_boolean_values(self) -> None:
        base = {
            "provider_id": "demo-sandbox",
            "display_name": "Demo Sandbox",
            "domain": "demo",
            "environment": "local_sandbox",
            "action_types": ["review"],
            "supports_rollback": True,
            "network_access": False,
            "credential_strategy": "none",
            "synthetic_only": True,
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.json"
            path.write_text(
                json.dumps({**base, "credential_value": "do-not-read"}),
                encoding="utf-8",
            )
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    main(("inspect-manifest", str(path)))
            path.write_text(
                json.dumps({**base, "network_access": "false"}),
                encoding="utf-8",
            )
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    main(("inspect-manifest", str(path)))

    def test_invalid_scaffold_values_return_a_bounded_cli_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as raised:
                    main(
                        (
                            "scaffold",
                            "--output",
                            str(Path(temporary) / "invalid"),
                            "--provider-id",
                            "../invalid",
                            "--display-name",
                            "Invalid",
                            "--domain",
                            "demo",
                            "--action",
                            "review",
                        )
                    )
            self.assertEqual(raised.exception.code, 2)
            self.assertIn("Scaffold manifest values are invalid", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
