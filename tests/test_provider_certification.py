from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from atlas_core import (
    CredentialStrategy,
    HotelPmsSandbox,
    ProviderCertificationRunner,
    ProviderEnvironment,
    ProviderManifest,
)


class UnmanifestedExecutor:
    executor_id = "legacy-executor"
    action_types = frozenset({"legacy_action"})
    supports_rollback = False

    def execute(self, request):
        raise AssertionError("Static assessment must not execute a provider.")

    def rollback(self, request, receipt):
        raise AssertionError("Static assessment must not execute a provider.")


class VendorSandboxExecutor:
    executor_id = "vendor-sandbox"
    action_types = frozenset({"sync_inventory"})
    supports_rollback = False
    manifest = ProviderManifest(
        provider_id=executor_id,
        display_name="Vendor Certification Sandbox",
        domain="hotel",
        environment=ProviderEnvironment.VENDOR_SANDBOX,
        action_types=("sync_inventory",),
        supports_rollback=False,
        network_access=True,
        credential_strategy=CredentialStrategy.ENVIRONMENT_REFERENCE,
        synthetic_only=True,
        endpoint_hosts=("sandbox.vendor.example",),
    )

    def __init__(self) -> None:
        self.called = False

    def execute(self, request):
        self.called = True
        raise AssertionError("Networked provider must not run in local certification.")

    def rollback(self, request, receipt):
        self.called = True
        raise AssertionError("Networked provider must not run in local certification.")


class ProviderCertificationTests(unittest.TestCase):
    def test_hotel_sandbox_passes_dynamic_conformance_and_cleans_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = HotelPmsSandbox(Path(directory) / "provider.db")
            receipt = ProviderCertificationRunner().certify(provider)
            self.assertEqual(receipt.status, "sandbox_certified")
            self.assertTrue(receipt.certification_id.startswith("cert_"))
            self.assertEqual(receipt.provider_id, provider.executor_id)
            self.assertEqual(receipt.credential_strategy, "none")
            self.assertFalse(receipt.network_access)
            self.assertTrue(receipt.synthetic_only)
            self.assertTrue(receipt.checks)
            self.assertTrue(
                all(check.status == "passed" for check in receipt.checks)
            )
            with provider._connect() as db:
                reservation_count = db.execute(
                    "SELECT COUNT(*) FROM hotel_sandbox_reservations"
                ).fetchone()[0]
                action_count = db.execute(
                    "SELECT COUNT(*) FROM hotel_sandbox_actions"
                ).fetchone()[0]
            self.assertEqual(reservation_count, 0)
            self.assertEqual(action_count, 0)

    def test_networked_vendor_sandbox_is_static_only_and_never_invoked(self) -> None:
        provider = VendorSandboxExecutor()
        runner = ProviderCertificationRunner()
        assessed = runner.assess(provider)
        certified = runner.certify(provider)
        self.assertEqual(assessed.status, "production_review_required")
        self.assertEqual(certified.status, "production_review_required")
        self.assertFalse(provider.called)
        self.assertEqual(certified.credential_strategy, "environment_reference")
        self.assertEqual(
            certified.manifest_sha256,
            assessed.manifest_sha256,
        )

    def test_unmanifested_executor_is_registered_but_not_certified(self) -> None:
        receipt = ProviderCertificationRunner().assess(UnmanifestedExecutor())
        self.assertEqual(receipt.status, "failed")
        self.assertEqual(receipt.checks[0].name, "manifest")
        self.assertNotIn("secret", receipt.__dict__)

    def test_manifest_rejects_urls_and_offline_credentials(self) -> None:
        with self.assertRaisesRegex(ValueError, "hostnames"):
            ProviderManifest(
                provider_id="bad-url",
                display_name="Bad URL",
                domain="hotel",
                environment=ProviderEnvironment.VENDOR_SANDBOX,
                action_types=("sync_inventory",),
                supports_rollback=False,
                network_access=True,
                credential_strategy=CredentialStrategy.ENVIRONMENT_REFERENCE,
                synthetic_only=True,
                endpoint_hosts=("https://sandbox.vendor.example/api",),
            )
        with self.assertRaisesRegex(ValueError, "credentials"):
            ProviderManifest(
                provider_id="bad-offline-secret",
                display_name="Bad Offline Credential",
                domain="hotel",
                environment=ProviderEnvironment.VENDOR_SANDBOX,
                action_types=("sync_inventory",),
                supports_rollback=False,
                network_access=False,
                credential_strategy=CredentialStrategy.ENVIRONMENT_REFERENCE,
                synthetic_only=True,
            )

    def test_manifest_runtime_mismatch_fails_static_assessment(self) -> None:
        provider = VendorSandboxExecutor()
        provider.action_types = frozenset({"different_action"})
        receipt = ProviderCertificationRunner().assess(provider)
        self.assertEqual(receipt.status, "failed")
        mismatch = next(
            check for check in receipt.checks if check.name == "action_allowlist"
        )
        self.assertEqual(mismatch.status, "failed")


if __name__ == "__main__":
    unittest.main()
