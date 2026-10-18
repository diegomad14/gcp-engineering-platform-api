#!/usr/bin/env python3
"""Fixed loopback proxy for a disposable PostgreSQL peer; no Docker access."""

import socket
import subprocess
import sys
import time


def run(arguments):
    proxy = subprocess.Popen(
        [
            "socat",
            "TCP-LISTEN:5432,bind=127.0.0.1,fork,reuseaddr",
            "TCP:eng-platform-postgres:5432",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(30):
            if proxy.poll() is not None:
                raise RuntimeError("Disposable PostgreSQL proxy unavailable")
            try:
                with socket.create_connection(("127.0.0.1", 5432), timeout=1):
                    break
            except OSError:
                time.sleep(1)
        else:
            raise RuntimeError("Disposable PostgreSQL proxy readiness timed out")
        return subprocess.run(
            [sys.executable, "/opt/eng-platform/quality_executor.py", *arguments],
            check=False,
        ).returncode
    finally:
        proxy.terminate()
        try:
            proxy.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proxy.kill()
            proxy.wait(timeout=5)


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
