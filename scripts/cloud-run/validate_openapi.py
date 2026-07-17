#!/usr/bin/env python3
"""Validate an API tagged URL without printing response bodies."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request


def fetch_json(url: str, timeout: int = 10) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"{url} returned HTTP {response.status}")
        return json.loads(response.read())


def fetch_status(url: str, timeout: int = 10) -> int:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return int(response.status)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--min-paths", type=int, default=1)
    parser.add_argument("--critical-paths", default="")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    last_error: Exception | None = None
    for _ in range(30):
        try:
            if fetch_status(base + "/health") == 200:
                break
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(2)
    else:
        raise SystemExit(f"/health did not pass: {last_error}")

    spec = fetch_json(base + "/openapi.json")
    paths = spec.get("paths", {})
    if len(paths) < args.min_paths:
        raise SystemExit(f"OpenAPI path count {len(paths)} below required {args.min_paths}")

    expected = [item for item in args.critical_paths.split() if item]
    missing = [path for path in expected if path not in paths]
    if missing:
        raise SystemExit(f"Missing critical OpenAPI paths: {', '.join(missing)}")

    print(f"OpenAPI validation passed: {len(paths)} paths")
    return 0


if __name__ == "__main__":
    sys.exit(main())

