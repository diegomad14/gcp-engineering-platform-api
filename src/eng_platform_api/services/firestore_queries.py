"""Bounded Firestore pages without offsets or a hidden overall result cap."""

from collections.abc import Iterator
from typing import Any


def iter_query_pages(
    query: Any, *, page_size: int = 200, read_time: Any = None
) -> Iterator[Any]:
    """Preserve query ordering and advance with the complete snapshot cursor.

    DocumentSnapshot cursors include document identity, so equal timestamps do
    not skip siblings. Pin later pages to the first page's read time when the
    SDK supplies it; concurrent status/timestamp changes cannot move records
    across that scan's cursor. Query/index failures propagate to the caller.
    """
    if page_size < 1:
        raise ValueError("Firestore page size must be positive")
    cursor = None
    while True:
        page = query.limit(page_size)
        if cursor is not None:
            page = page.start_after(cursor)
        snapshots = list(
            page.stream(read_time=read_time) if read_time else page.stream()
        )
        if not snapshots:
            return
        if read_time is None:
            read_time = getattr(snapshots[-1], "read_time", None)
        yield from snapshots
        if len(snapshots) < page_size:
            return
        cursor = snapshots[-1]


def count_query(query: Any) -> int:
    """Use an index aggregation instead of downloading counted documents."""
    return int(query.count(alias="total").get()[0][0].value)


def count_query_at(query: Any, *, read_time: Any = None) -> tuple[int, Any]:
    """Return a count and server snapshot time for consistent follow-up reads."""
    aggregate = query.count(alias="total")
    rows = aggregate.get(read_time=read_time) if read_time else aggregate.get()
    result = rows[0][0]
    snapshot_time = getattr(result, "read_time", None) or read_time
    if snapshot_time is None:
        raise RuntimeError("Firestore did not return a consistent read timestamp")
    return int(result.value), snapshot_time
