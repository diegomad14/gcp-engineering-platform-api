#!/usr/bin/env python3
"""Inventory existing builds; --apply writes only the private usage ledger."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from eng_platform_api.config import config  # noqa: E402
from eng_platform_api.services.cloud_build_usage import backfill  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--month", required=True, help="UTC YYYY-MM")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write metering records; never submit builds",
    )
    args = parser.parse_args()
    config.mock_mode = False
    config.cloud_build.project_id = args.project
    config.cloud_build.enabled = True
    config.release_orchestrator.enabled = True
    print(json.dumps(backfill(args.month, apply=args.apply), indent=2))


if __name__ == "__main__":
    main()
