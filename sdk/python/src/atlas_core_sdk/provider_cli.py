"""Provider adapter development commands for the official Atlas SDK."""

from __future__ import annotations

import argparse
import importlib
import json
import re
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from atlas_core import (
    CredentialStrategy,
    DeclarativeAdapterSpec,
    DeclarativeApprovalRule,
    DeclarativeField,
    DeclarativeIndustryAdapter,
    DeclarativeNormalization,
    DeclarativeRuleOperator,
    DeclarativeValueType,
    ProviderCertificationRunner,
    ProviderEnvironment,
    ProviderManifest,
    RiskLevel,
    load_declarative_adapter_spec,
)

from . import __version__

MAX_MANIFEST_BYTES = 64 * 1024
MAX_RECORD_BYTES = 1024 * 1024
FACTORY_REFERENCE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$"
)
MANIFEST_FIELDS = {
    "provider_id",
    "display_name",
    "domain",
    "environment",
    "action_types",
    "supports_rollback",
    "network_access",
    "credential_strategy",
    "synthetic_only",
    "live_action_authorized",
    "endpoint_hosts",
    "contract_version",
}


class ProviderCliError(ValueError):
    """A bounded user-facing provider CLI failure."""


def _module_name(provider_id: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_]", "_", provider_id.replace("-", "_"))
    if not value or value[0].isdigit():
        value = f"provider_{value}"
    return value.lower()


def _class_name(module_name: str) -> str:
    return "".join(part.capitalize() for part in module_name.split("_")) + "Sandbox"


def _read_manifest(path: Path) -> ProviderManifest:
    try:
        size = path.stat().st_size
    except OSError as error:
        raise ProviderCliError("Manifest cannot be read.") from error
    if size > MAX_MANIFEST_BYTES:
        raise ProviderCliError("Manifest exceeds the 64 KiB limit.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProviderCliError("Manifest must be valid UTF-8 JSON.") from error
    if not isinstance(value, dict):
        raise ProviderCliError("Manifest must be a JSON object.")
    unknown = set(value) - MANIFEST_FIELDS
    if unknown:
        raise ProviderCliError("Manifest contains unknown fields.")
    required = MANIFEST_FIELDS - {
        "live_action_authorized",
        "endpoint_hosts",
        "contract_version",
    }
    if required - set(value):
        raise ProviderCliError("Manifest is missing required fields.")
    try:
        action_types = value["action_types"]
        endpoint_hosts = value.get("endpoint_hosts", [])
        if not isinstance(action_types, list) or not all(
            isinstance(item, str) for item in action_types
        ):
            raise TypeError
        if not isinstance(endpoint_hosts, list) or not all(
            isinstance(item, str) for item in endpoint_hosts
        ):
            raise TypeError
        return ProviderManifest(
            provider_id=str(value["provider_id"]),
            display_name=str(value["display_name"]),
            domain=str(value["domain"]),
            environment=ProviderEnvironment(str(value["environment"])),
            action_types=tuple(action_types),
            supports_rollback=_strict_bool(value["supports_rollback"]),
            network_access=_strict_bool(value["network_access"]),
            credential_strategy=CredentialStrategy(
                str(value["credential_strategy"])
            ),
            synthetic_only=_strict_bool(value["synthetic_only"]),
            live_action_authorized=_strict_bool(
                value.get("live_action_authorized", False)
            ),
            endpoint_hosts=tuple(endpoint_hosts),
            contract_version=_strict_int(value.get("contract_version", 1)),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ProviderCliError("Manifest values are invalid.") from error


def _strict_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError
    return value


def _strict_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError
    return value


def _manifest_payload(manifest: ProviderManifest) -> dict[str, Any]:
    return {"schema_version": 1, "manifest": manifest.safe_dict()}


def _load_factory(reference: str) -> object:
    if not FACTORY_REFERENCE.fullmatch(reference):
        raise ProviderCliError("Factory must use the form package.module:function.")
    module_name, function_name = reference.split(":", 1)
    try:
        module = importlib.import_module(module_name)
        factory = getattr(module, function_name)
        if not callable(factory):
            raise TypeError
        executor = factory()
    except Exception as error:
        raise ProviderCliError(
            f"Trusted local factory could not be loaded ({error.__class__.__name__})."
        ) from error
    return executor


def _evidence_payload(receipt: object, *, dynamic: bool) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "sdk_version": __version__,
        "mode": "dynamic_local_sandbox" if dynamic else "static_assessment",
        "receipt": asdict(receipt),
    }


def _write_evidence(path: Path, payload: dict[str, Any]) -> None:
    if path.suffix.lower() != ".json":
        raise ProviderCliError("Evidence output must use a .json filename.")
    if not path.parent.is_dir():
        raise ProviderCliError("Evidence output parent directory does not exist.")
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
    except FileExistsError as error:
        raise ProviderCliError("Refusing to overwrite existing evidence.") from error
    except OSError as error:
        raise ProviderCliError("Evidence output could not be written.") from error


def _write_new_json(path: Path, payload: dict[str, Any], *, label: str) -> None:
    if path.suffix.lower() != ".json":
        raise ProviderCliError(f"{label} output must use a .json filename.")
    if not path.parent.is_dir():
        raise ProviderCliError(f"{label} output parent directory does not exist.")
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    except FileExistsError as error:
        raise ProviderCliError(f"Refusing to overwrite existing {label}.") from error
    except OSError as error:
        raise ProviderCliError(f"{label} output could not be written.") from error


def _parse_declarative_field(value: str) -> DeclarativeField:
    parts = value.split(":")
    if len(parts) not in {3, 4, 5}:
        raise ProviderCliError(
            "Adapter fields use source:target:type[:normalization[:required|optional]]."
        )
    source, target, value_type = parts[:3]
    normalization = parts[3] if len(parts) >= 4 and parts[3] else "none"
    required_value = parts[4] if len(parts) == 5 else "required"
    if required_value not in {"required", "optional"}:
        raise ProviderCliError(
            "Adapter field presence must be required or optional."
        )
    try:
        return DeclarativeField(
            source=source,
            target=target,
            value_type=DeclarativeValueType(value_type),
            required=required_value == "required",
            normalization=DeclarativeNormalization(normalization),
        )
    except ValueError as error:
        raise ProviderCliError("Adapter field mapping is invalid.") from error


def _parse_declarative_rule(value: str) -> DeclarativeApprovalRule:
    parts = value.split(":")
    if len(parts) != 6:
        raise ProviderCliError(
            "Adapter rules use rule_id:field:operator:value:action_type:risk."
        )
    rule_id, field, operator_value, expected, action_type, risk = parts
    expected_value: str | None = expected or None
    try:
        return DeclarativeApprovalRule(
            rule_id=rule_id,
            field=field,
            operator=DeclarativeRuleOperator(operator_value),
            value=expected_value,
            action_type=action_type,
            reason=f"Declarative rule {rule_id} requires human review.",
            risk=RiskLevel(risk),
            review_tasks=(
                "confirm the source update",
                "review the mapped field change",
                "record the human decision",
            ),
        )
    except ValueError as error:
        raise ProviderCliError("Adapter approval rule is invalid.") from error


def _scaffold_adapter(args: argparse.Namespace) -> dict[str, Any]:
    try:
        spec = DeclarativeAdapterSpec(
            adapter_id=args.adapter_id,
            domain=args.domain,
            entity_type=args.entity_type,
            id_field=args.id_field,
            observed_at_field=args.observed_at_field,
            source_record_id_field=args.source_record_id_field or "",
            version_field=args.version_field or "",
            fields=tuple(_parse_declarative_field(value) for value in args.field),
            rules=tuple(_parse_declarative_rule(value) for value in args.rule),
        )
    except ValueError as error:
        raise ProviderCliError("Declarative adapter values are invalid.") from error
    output = Path(args.output).expanduser().resolve()
    _write_new_json(output, spec.safe_dict(), label="adapter spec")
    return {
        "schema_version": 1,
        "status": "created",
        "adapter_spec": str(output),
        "adapter_id": spec.adapter_id,
        "domain": spec.domain,
        "spec_sha256": spec.sha256(),
        "mapped_fields": len(spec.fields),
        "approval_rules": len(spec.rules),
    }


def _load_adapter_spec(path: str) -> DeclarativeAdapterSpec:
    try:
        return load_declarative_adapter_spec(Path(path).expanduser().resolve())
    except ValueError as error:
        raise ProviderCliError("Declarative adapter spec is invalid.") from error


def _read_record(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > MAX_RECORD_BYTES:
            raise ProviderCliError("Validation record exceeds the 1 MiB limit.")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ProviderCliError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProviderCliError(
            "Validation record must be valid UTF-8 JSON."
        ) from error
    if not isinstance(payload, dict):
        raise ProviderCliError("Validation record must be a JSON object.")
    return payload


def _provider_source(
    *,
    provider_id: str,
    display_name: str,
    domain: str,
    action_types: tuple[str, ...],
    class_name: str,
) -> str:
    first_action = action_types[0]
    return f'''"""Synthetic local provider scaffold generated by atlas-provider."""

from __future__ import annotations

import hashlib

from atlas_core_sdk import (
    ActionExecutionReceipt,
    ActionExecutionRequest,
    ActionExecutionResult,
    CredentialStrategy,
    ProviderEnvironment,
    ProviderManifest,
)


class {class_name}:
    """Offline, credential-free scaffold. Replace only with reviewed behavior."""

    executor_id = {provider_id!r}
    action_types = frozenset({action_types!r})
    supports_rollback = True
    is_sandbox = True
    manifest = ProviderManifest(
        provider_id=executor_id,
        display_name={display_name!r},
        domain={domain!r},
        environment=ProviderEnvironment.LOCAL_SANDBOX,
        action_types=tuple(sorted(action_types)),
        supports_rollback=True,
        network_access=False,
        credential_strategy=CredentialStrategy.NONE,
        synthetic_only=True,
    )

    def __init__(self) -> None:
        self._state: dict[tuple[str, str], str] = {{}}
        self._actions: dict[str, tuple[str, str, str, bool]] = {{}}

    def execute(self, request: ActionExecutionRequest) -> ActionExecutionResult:
        if request.domain != self.manifest.domain:
            raise ValueError("Provider domain is not allowed.")
        if request.action_type not in self.action_types:
            raise ValueError("Provider action is not allowed.")
        key = (request.tenant_id, request.entity_id)
        prior = self._actions.get(request.idempotency_key)
        if prior is not None:
            execution_id, previous, reference, rolled_back = prior
            if execution_id != request.execution_id:
                raise ValueError("Provider idempotency collision.")
            if rolled_back:
                return ActionExecutionResult(False, "Action was rolled back.")
            return ActionExecutionResult(True, "Action already completed.", reference)
        previous = self._state.get(key, "pending")
        reference = "sandbox:" + hashlib.sha256(
            request.idempotency_key.encode()
        ).hexdigest()[:20]
        self._state[key] = "completed"
        self._actions[request.idempotency_key] = (
            request.execution_id,
            previous,
            reference,
            False,
        )
        return ActionExecutionResult(True, "Synthetic action completed.", reference)

    def rollback(
        self,
        request: ActionExecutionRequest,
        receipt: ActionExecutionReceipt,
    ) -> ActionExecutionResult:
        action = self._actions.get(request.idempotency_key)
        if action is None:
            return ActionExecutionResult(False, "Original action was not found.")
        execution_id, previous, reference, rolled_back = action
        if execution_id != request.execution_id:
            raise ValueError("Provider execution identity mismatch.")
        if not rolled_back:
            self._state[(request.tenant_id, request.entity_id)] = previous
            self._actions[request.idempotency_key] = (
                execution_id,
                previous,
                reference,
                True,
            )
        return ActionExecutionResult(True, "Synthetic action rolled back.", reference)

    def create_conformance_request(
        self, certification_id: str
    ) -> ActionExecutionRequest:
        suffix = hashlib.sha256(certification_id.encode()).hexdigest()[:12]
        tenant_id = f"atlas-cert-{{suffix}}"
        entity_id = f"CERT-{{suffix}}"
        self._state[(tenant_id, entity_id)] = "pending"
        return ActionExecutionRequest(
            execution_id=f"cert-exe-{{suffix}}",
            proposal_id=f"cert-proposal-{{suffix}}",
            tenant_id=tenant_id,
            domain=self.manifest.domain,
            action_type={first_action!r},
            entity_type="synthetic_record",
            entity_id=entity_id,
            payload={{"record_id": entity_id}},
            idempotency_key=f"cert-idem-{{suffix}}",
            attempt=1,
        )

    def inspect_conformance_state(
        self, request: ActionExecutionRequest
    ) -> dict[str, str]:
        state = self._state.get((request.tenant_id, request.entity_id))
        return {{}} if state is None else {{"state": state}}

    def cleanup_conformance(self, request: ActionExecutionRequest) -> None:
        if not request.tenant_id.startswith("atlas-cert-"):
            raise ValueError("Refusing to clean a non-certification tenant.")
        self._state.pop((request.tenant_id, request.entity_id), None)
        self._actions.pop(request.idempotency_key, None)


def create_provider() -> {class_name}:
    """Factory used by the atlas-provider developer CLI."""

    return {class_name}()
'''


def _scaffold(args: argparse.Namespace) -> dict[str, Any]:
    try:
        manifest = ProviderManifest(
            provider_id=args.provider_id,
            display_name=args.display_name,
            domain=args.domain,
            environment=ProviderEnvironment.LOCAL_SANDBOX,
            action_types=tuple(args.action),
            supports_rollback=True,
            network_access=False,
            credential_strategy=CredentialStrategy.NONE,
            synthetic_only=True,
        )
    except ValueError as error:
        raise ProviderCliError("Scaffold manifest values are invalid.") from error
    output = Path(args.output).expanduser().resolve()
    if output.exists():
        raise ProviderCliError("Refusing to overwrite an existing project path.")
    parent = output.parent
    if not parent.is_dir():
        raise ProviderCliError("Project parent directory does not exist.")
    module_name = _module_name(manifest.provider_id)
    class_name = _class_name(module_name)
    staging = Path(tempfile.mkdtemp(prefix=".atlas-provider-", dir=parent))
    try:
        package = staging / "src" / module_name
        tests = staging / "tests"
        package.mkdir(parents=True)
        tests.mkdir()
        (staging / "atlas-provider.json").write_text(
            json.dumps(manifest.safe_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        (staging / "pyproject.toml").write_text(
            f"""[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = {manifest.provider_id!r}
version = "0.1.0"
description = {f"Atlas provider adapter for {manifest.display_name}."!r}
requires-python = ">=3.12"
dependencies = ["atlas-core-sdk>=0.1.0,<0.2.0"]

[tool.setuptools.packages.find]
where = ["src"]
""",
            encoding="utf-8",
            newline="\n",
        )
        (package / "__init__.py").write_text(
            f"from .provider import {class_name}, create_provider\n\n"
            f"__all__ = [{class_name!r}, \"create_provider\"]\n",
            encoding="utf-8",
            newline="\n",
        )
        (package / "provider.py").write_text(
            _provider_source(
                provider_id=manifest.provider_id,
                display_name=manifest.display_name,
                domain=manifest.domain,
                action_types=manifest.action_types,
                class_name=class_name,
            ),
            encoding="utf-8",
            newline="\n",
        )
        (tests / "test_certification.py").write_text(
            f"""import unittest

from atlas_core_sdk import ProviderCertificationRunner
from {module_name} import create_provider


class ProviderCertificationTests(unittest.TestCase):
    def test_local_sandbox_conformance(self) -> None:
        receipt = ProviderCertificationRunner().certify(create_provider())
        self.assertEqual(receipt.status, "sandbox_certified")
        self.assertTrue(all(check.status == "passed" for check in receipt.checks))


if __name__ == "__main__":
    unittest.main()
""",
            encoding="utf-8",
            newline="\n",
        )
        (staging / "README.md").write_text(
            f"""# {manifest.display_name}

Generated by the official Atlas Core SDK for trusted local development.

This starter is offline, credential-free, synthetic-only, and rollback-capable.
It is not a vendor-certified or production adapter.

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
atlas-provider certify --factory {module_name}:create_provider --dynamic --evidence certification.json
```

Review `atlas-provider.json`, replace synthetic behavior deliberately, retain
idempotency and cleanup, and complete a focused security review before adding
network access or credential references.
""",
            encoding="utf-8",
            newline="\n",
        )
        staging.rename(output)
    except OSError as error:
        raise ProviderCliError("Provider project could not be created.") from error
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {
        "schema_version": 1,
        "status": "created",
        "project": str(output),
        "module": module_name,
        "factory": f"{module_name}:create_provider",
        "manifest_sha256": ProviderCertificationRunner.manifest_sha256(manifest),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="atlas-provider",
        description="Scaffold and certify trusted local Atlas provider adapters.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    scaffold = commands.add_parser(
        "scaffold", help="Create an offline synthetic provider starter."
    )
    scaffold.add_argument("--output", required=True)
    scaffold.add_argument("--provider-id", required=True)
    scaffold.add_argument("--display-name", required=True)
    scaffold.add_argument("--domain", required=True)
    scaffold.add_argument("--action", required=True, action="append")

    inspect = commands.add_parser(
        "inspect-manifest", help="Validate and print a safe provider manifest."
    )
    inspect.add_argument("manifest")

    scaffold_adapter = commands.add_parser(
        "scaffold-adapter",
        help="Create a strict declarative source-translation specification.",
    )
    scaffold_adapter.add_argument("--output", required=True)
    scaffold_adapter.add_argument("--adapter-id", required=True)
    scaffold_adapter.add_argument("--domain", required=True)
    scaffold_adapter.add_argument("--entity-type", required=True)
    scaffold_adapter.add_argument("--id-field", required=True)
    scaffold_adapter.add_argument("--observed-at-field", required=True)
    scaffold_adapter.add_argument("--source-record-id-field")
    scaffold_adapter.add_argument("--version-field")
    scaffold_adapter.add_argument("--field", required=True, action="append")
    scaffold_adapter.add_argument("--rule", action="append", default=[])

    inspect_adapter = commands.add_parser(
        "inspect-adapter",
        help="Validate and print a safe declarative adapter specification.",
    )
    inspect_adapter.add_argument("adapter")

    validate_record = commands.add_parser(
        "validate-record",
        help="Validate one local record without storing or printing its values.",
    )
    validate_record.add_argument("--adapter", required=True)
    validate_record.add_argument("--record", required=True)

    certify = commands.add_parser(
        "certify",
        help="Assess a trusted local factory; dynamic mode is local-sandbox only.",
    )
    certify.add_argument("--factory", required=True)
    certify.add_argument("--dynamic", action="store_true")
    certify.add_argument("--evidence")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "scaffold":
            payload = _scaffold(args)
        elif args.command == "inspect-manifest":
            payload = _manifest_payload(
                _read_manifest(Path(args.manifest).expanduser().resolve())
            )
        elif args.command == "scaffold-adapter":
            payload = _scaffold_adapter(args)
        elif args.command == "inspect-adapter":
            spec = _load_adapter_spec(args.adapter)
            payload = {
                "schema_version": 1,
                "spec_sha256": spec.sha256(),
                "adapter": spec.safe_dict(),
            }
        elif args.command == "validate-record":
            spec = _load_adapter_spec(args.adapter)
            record = _read_record(Path(args.record).expanduser().resolve())
            try:
                observation = DeclarativeIndustryAdapter(spec).translate(
                    record,
                    tenant_id="atlas-validation",
                    source_system="local-validation",
                )
            except ValueError as error:
                raise ProviderCliError(
                    "Validation record does not satisfy the adapter."
                ) from error
            payload = {
                "schema_version": 1,
                "status": "valid",
                "spec_sha256": spec.sha256(),
                "adapter_id": spec.adapter_id,
                "domain": observation.domain,
                "entity_type": observation.entity_type,
                "mapped_fields": tuple(sorted(observation.attributes)),
                "source_version_present": observation.source_version is not None,
            }
        else:
            executor = _load_factory(args.factory)
            runner = ProviderCertificationRunner()
            try:
                receipt = (
                    runner.certify(executor)
                    if args.dynamic
                    else runner.assess(executor)
                )
            except Exception as error:
                raise ProviderCliError(
                    "Provider assessment failed "
                    f"({error.__class__.__name__}); no exception text was retained."
                ) from error
            payload = _evidence_payload(receipt, dynamic=args.dynamic)
            if args.evidence:
                _write_evidence(
                    Path(args.evidence).expanduser().resolve(),
                    payload,
                )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    except ProviderCliError as error:
        parser.exit(2, f"atlas-provider: error: {error}\n")
    return 2


def console_main() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    console_main()
