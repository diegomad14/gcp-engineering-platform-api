"""Resolve operator intent independently for release and deployment.

Legacy null/auto defaults resolve to GitHub Actions at admission. Once an
operation has been reserved, its selection is authoritative even if the service
default changes. Recorded historical provider values remain compatible.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator

from fastapi import HTTPException

from ..models import CatalogService, ExecutionProvider, ProviderSelection

_selection: ContextVar[ProviderSelection | None] = ContextVar(
    "provider_selection", default=None
)


def current() -> ProviderSelection | None:
    return _selection.get()


@contextmanager
def using(selection: ProviderSelection) -> Iterator[None]:
    token = _selection.set(selection)
    try:
        yield
    finally:
        _selection.reset(token)


def select(
    service: CatalogService,
    requested: ExecutionProvider | None,
    *,
    operation: str,
    legacy_provider: ExecutionProvider = "github_actions",
) -> ProviderSelection:
    if requested is not None and requested not in {"github_actions", "cloud_build"}:
        raise HTTPException(422, "Unknown execution provider")
    default = (
        service.release.executor
        if operation == "release"
        else service.deployment.executor
    )
    configured: ExecutionProvider | None = (
        default if default in {"github_actions", "cloud_build"} else None
    )  # type: ignore[assignment]
    return ProviderSelection(
        requested=requested,
        service_default=configured,
        effective=requested or configured or legacy_provider,
        source="request"
        if requested
        else "service_default"
        if configured
        else "legacy_policy",
    )


def require_same(
    selection: ProviderSelection | dict | None,
    requested: ExecutionProvider | None,
    *,
    legacy_provider: str | None = None,
) -> None:
    effective = (
        selection.effective
        if isinstance(selection, ProviderSelection)
        else (selection or {}).get("effective", legacy_provider)
    )
    if requested and effective != requested:
        raise HTTPException(
            409,
            "Existing operation keeps its original provider; a new selection "
            "cannot change an idempotent request",
        )


def locked(record: dict) -> bool:
    return bool((record.get("provider_selection") or {}).get("locked"))
