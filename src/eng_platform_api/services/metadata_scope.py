"""Request-local resource scope for partially authorized metadata readers."""

from contextvars import ContextVar
from contextlib import contextmanager

from ..config import catalog_source_identity

visible_services: ContextVar[frozenset[str] | None] = ContextVar(
    "metadata_visible_services", default=None
)


@contextmanager
def reader_scope(services: frozenset[str] | None):
    token = visible_services.set(services)
    try:
        yield
    finally:
        visible_services.reset(token)


def cache_identity():
    source = catalog_source_identity()
    scope = visible_services.get()
    return source if scope is None else (source, scope)
