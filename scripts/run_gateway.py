from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for source in (
    ROOT / "src",
    ROOT / "sdk" / "python" / "src",
    ROOT / "apps" / "gateway" / "src",
):
    sys.path.insert(0, str(source))

from atlas_core.adapters import (  # noqa: E402
    ApartmentAdapter,
    ConstructionAdapter,
    HealthcareAdapter,
    HotelAdapter,
    ManufacturingAdapter,
)
from atlas_gateway import AccessGrant, create_gateway  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the private Atlas Gateway.")
    parser.add_argument("--database", type=Path, default=Path("runtime/atlas-core.db"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8792)
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--tls-certificate", type=Path)
    parser.add_argument("--tls-private-key", type=Path)
    args = parser.parse_args()

    token = os.environ.get("ATLAS_GATEWAY_TOKEN", "")
    tenants = frozenset(
        value.strip()
        for value in os.environ.get("ATLAS_GATEWAY_TENANTS", "").split(",")
        if value.strip()
    )
    permissions = frozenset(
        value.strip()
        for value in os.environ.get(
            "ATLAS_GATEWAY_PERMISSIONS",
            "read,ingest,approve",
        ).split(",")
        if value.strip()
    )
    grant = AccessGrant.from_token(
        name=os.environ.get("ATLAS_GATEWAY_IDENTITY", "environment-service"),
        token=token,
        tenants=tenants,
        permissions=permissions,
    )
    server = create_gateway(
        database=args.database,
        adapters=(
            HotelAdapter(),
            ApartmentAdapter(),
            ConstructionAdapter(),
            ManufacturingAdapter(),
            HealthcareAdapter(),
        ),
        grants=(grant,),
        host=args.host,
        port=args.port,
        allow_remote=args.allow_remote,
        tls_certificate=args.tls_certificate,
        tls_private_key=args.tls_private_key,
    )
    scheme = "https" if server.tls_enabled else "http"
    print(
        f"Atlas Gateway listening on {scheme}://{args.host}:{server.server_address[1]}"
    )
    print("Service token loaded from environment; token value is not logged.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
