from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for source in (
    ROOT / "src",
    ROOT / "sdk" / "python" / "src",
    ROOT / "apps" / "operator" / "src",
):
    sys.path.insert(0, str(source))

from atlas_operator import create_server  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local Atlas Core operator.")
    parser.add_argument("--database", type=Path, default=ROOT / "runtime" / "operator.db")
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument(
        "--automation-config",
        type=Path,
        help="Optional JSON config used by atlas-core automation-run.",
    )
    args = parser.parse_args()
    automation_config = args.automation_config
    default_automation_config = ROOT / "config" / "atlas-automation.json"
    if automation_config is None and default_automation_config.is_file():
        automation_config = default_automation_config
    server = create_server(
        database=args.database,
        port=args.port,
        automation_config=automation_config,
    )
    print(f"Atlas Core Operator: http://127.0.0.1:{server.server_address[1]}")
    print(f"Database: {args.database.resolve()}")
    print("Localhost only. Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
