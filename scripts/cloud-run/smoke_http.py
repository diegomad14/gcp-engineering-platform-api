#!/usr/bin/env python3
"""Minimal HTTP smoke checks for service URLs."""

from __future__ import annotations

import argparse
import sys
import urllib.request


def status(url: str, method: str = "GET") -> int:
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=10) as response:
        return int(response.status)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--web-url")
    args = parser.parse_args()

    api_status = status(args.api_url.rstrip("/") + "/health")
    print(f"api_health={api_status}")
    if api_status != 200:
        return 1

    if args.web_url:
        web_status = status(args.web_url.rstrip("/"), method="HEAD")
        print(f"web_head={web_status}")
        if web_status != 200:
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

