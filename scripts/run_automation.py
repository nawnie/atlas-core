from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from atlas_core import AtlasAutomationRunner  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run guarded Atlas Core local automation."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config" / "atlas-automation.json",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=ROOT / "runtime" / "operator.db",
    )
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one pass and exit instead of watching continuously.",
    )
    parser.add_argument(
        "--prompt-passphrase",
        action="store_true",
        help=(
            "Prompt without echo when an enabled portable destination's "
            "runtime passphrase environment variable is absent."
        ),
    )
    args = parser.parse_args()
    runner = AtlasAutomationRunner(
        database=args.database,
        config_path=args.config,
    )
    injected_secret_name: str | None = None
    destination = runner.config.portable_destination
    if (
        args.prompt_passphrase
        and destination.enabled
        and not os.environ.get(destination.passphrase_env)
    ):
        secret = getpass.getpass(
            f"Portable recovery passphrase ({destination.passphrase_env}): "
        )
        if not secret:
            raise SystemExit("A non-empty portable recovery passphrase is required.")
        os.environ[destination.passphrase_env] = secret
        injected_secret_name = destination.passphrase_env
    try:
        if args.once:
            print(json.dumps(asdict(runner.run_once()), indent=2, default=str))
            return
        print("Atlas Core automation is running locally.")
        print(f"Database: {args.database.resolve()}")
        print(f"Configuration: {args.config.resolve()}")
        print("Press Ctrl+C to stop.")
        try:
            runner.run_forever(poll_seconds=args.poll_seconds)
        except KeyboardInterrupt:
            pass
    finally:
        if injected_secret_name is not None:
            os.environ.pop(injected_secret_name, None)


if __name__ == "__main__":
    main()
