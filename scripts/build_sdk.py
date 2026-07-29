from __future__ import annotations

import base64
import csv
import hashlib
import io
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SDK_ROOT = ROOT / "sdk" / "python"
DIST = SDK_ROOT / "dist"
VERSION = "0.1.0"
CRYPTOGRAPHY_VERSION = "46.0.7"


def digest(data: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return f"sha256={encoded.decode()}"


def metadata_files(
    *,
    distribution: str,
    package: str,
    summary: str,
    dependencies: tuple[str, ...] = (),
    console_scripts: tuple[str, ...] = (),
) -> dict[str, bytes]:
    normalized = distribution.replace("-", "_")
    dist_info = f"{normalized}-{VERSION}.dist-info"
    requires = "".join(f"Requires-Dist: {item}\n" for item in dependencies)
    metadata = f"""Metadata-Version: 2.1
Name: {distribution}
Version: {VERSION}
Summary: {summary}
Author: AI Embedded Systems
Requires-Python: >=3.12
{requires}\
License: Proprietary
"""
    wheel = """Wheel-Version: 1.0
Generator: atlas-core-sdk-build
Root-Is-Purelib: true
Tag: py3-none-any
"""
    files = {
        f"{dist_info}/METADATA": metadata.encode(),
        f"{dist_info}/WHEEL": wheel.encode(),
        f"{dist_info}/top_level.txt": f"{package}\n".encode(),
    }
    if console_scripts:
        body = "[console_scripts]\n" + "".join(
            f"{script}\n" for script in console_scripts
        )
        files[f"{dist_info}/entry_points.txt"] = body.encode()
    return files


def build_wheel(
    *,
    source_root: Path,
    distribution: str,
    package: str,
    summary: str,
    dependencies: tuple[str, ...] = (),
    console_scripts: tuple[str, ...] = (),
) -> Path:
    normalized = distribution.replace("-", "_")
    dist_info = f"{normalized}-{VERSION}.dist-info"
    wheel_path = DIST / f"{normalized}-{VERSION}-py3-none-any.whl"
    payloads: dict[str, bytes] = {}
    for source in sorted((source_root / package).rglob("*")):
        if source.is_file() and "__pycache__" not in source.parts:
            archive_path = source.relative_to(source_root).as_posix()
            payloads[archive_path] = source.read_bytes()
    payloads.update(
        metadata_files(
            distribution=distribution,
            package=package,
            summary=summary,
            dependencies=dependencies,
            console_scripts=console_scripts,
        )
    )

    record = io.StringIO()
    writer = csv.writer(record, lineterminator="\n")
    for archive_path, data in sorted(payloads.items()):
        writer.writerow((archive_path, digest(data), len(data)))
    record_path = f"{dist_info}/RECORD"
    writer.writerow((record_path, "", ""))
    payloads[record_path] = record.getvalue().encode()

    timestamp = (2026, 7, 24, 12, 0, 0)
    with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for archive_path, data in sorted(payloads.items()):
            info = zipfile.ZipInfo(archive_path, timestamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
    return wheel_path


def build() -> tuple[Path, Path, Path, Path]:
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)
    core_wheel = build_wheel(
        source_root=ROOT / "src",
        distribution="atlas-core",
        package="atlas_core",
        summary="Local-first company memory and reasoning core.",
        dependencies=("cryptography (>=46.0.7,<47)",),
    )
    sdk_wheel = build_wheel(
        source_root=SDK_ROOT / "src",
        distribution="atlas-core-sdk",
        package="atlas_core_sdk",
        summary="Official Python SDK for Atlas Core.",
        dependencies=("atlas-core (>=0.1.0,<0.2.0)",),
        console_scripts=(
            "atlas-provider = atlas_core_sdk.provider_cli:console_main",
        ),
    )
    gateway_wheel = build_wheel(
        source_root=ROOT / "apps" / "gateway" / "src",
        distribution="atlas-core-gateway",
        package="atlas_gateway",
        summary="Authenticated private gateway for Atlas Core.",
        dependencies=("atlas-core-sdk (>=0.1.0,<0.2.0)",),
    )
    operator_wheel = build_wheel(
        source_root=ROOT / "apps" / "operator" / "src",
        distribution="atlas-core-operator",
        package="atlas_operator",
        summary="Local operator console and Hotel/PMS sandbox for Atlas Core.",
        dependencies=("atlas-core-sdk (>=0.1.0,<0.2.0)",),
    )
    for wheel_path in (
        core_wheel,
        sdk_wheel,
        gateway_wheel,
        operator_wheel,
    ):
        print(wheel_path)
        print(hashlib.sha256(wheel_path.read_bytes()).hexdigest())
    vendor = DIST / "vendor"
    vendor.mkdir()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--only-binary=:all:",
            "--dest",
            str(vendor),
            f"cryptography=={CRYPTOGRAPHY_VERSION}",
        ],
        check=True,
    )
    for wheel_path in sorted(vendor.glob("*.whl")):
        print(wheel_path)
        print(hashlib.sha256(wheel_path.read_bytes()).hexdigest())
    return core_wheel, sdk_wheel, gateway_wheel, operator_wheel


if __name__ == "__main__":
    build()
