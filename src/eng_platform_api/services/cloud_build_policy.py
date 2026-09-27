"""Submission policy: use the standard default pool, never a custom machine.

The default is documented as e2-standard-2. Explicit E2_STANDARD_2 uses a
different billing SKU in our audit, so even that override is disallowed.
This validator applies only to new submissions, not historical inspection.
"""

from typing import Any


def economy_options(**extra: Any) -> dict[str, Any]:
    options = {"logging": "CLOUD_LOGGING_ONLY", **extra}
    validate_submission({"options": options, "timeout": "1800s"})
    return options


def validate_submission(request: dict[str, Any]) -> None:
    options = request.get("options", {})
    if any(
        key in options for key in ("machineType", "pool", "workerPool", "diskSizeGb")
    ):
        raise ValueError("Economy builds require the standard default machine and disk")
    if options.get("logging") != "CLOUD_LOGGING_ONLY":
        raise ValueError("Economy builds require CLOUD_LOGGING_ONLY")
    timeout = str(request.get("timeout", ""))
    if not timeout.endswith("s") or not timeout[:-1].isdigit():
        raise ValueError("Economy builds require an explicit timeout")
    if not 0 < int(timeout[:-1]) <= 3600:
        raise ValueError("Economy build timeout must be between 1 and 3600 seconds")
