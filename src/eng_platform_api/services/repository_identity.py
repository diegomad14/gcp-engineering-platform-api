"""Stable identities for repositories that are being renamed in place.

GitHub keeps a repository's numeric ID when its owner/name changes.  Stored
release evidence keeps its original spelling; only comparisons use aliases.
"""

from __future__ import annotations


_RENAMES: dict[int, tuple[str, str]] = {
    1306114845: ("diegomad14/cgm-sanplat-api", "diegomad14/cgm-artemis-api"),
    1306114872: ("diegomad14/cgm-sanplat-web", "diegomad14/cgm-artemis-web"),
}
_BY_NAME = {
    name.casefold(): repository_id
    for repository_id, names in _RENAMES.items()
    for name in names
}


def repository_id(repository: str) -> int | None:
    """Return a known immutable GitHub ID, never infer one from a redirect."""
    return _BY_NAME.get(repository.casefold())


def aliases(repository: str) -> tuple[str, ...]:
    """Include both historical and current spellings for read-only lookup."""
    known_id = repository_id(repository)
    if known_id is None:
        return (repository,)
    return _RENAMES[known_id]


def same_repository(left: str, right: str) -> bool:
    """Compare identities without changing the spelling stored on a record."""
    if left.casefold() == right.casefold():
        return True
    known_id = repository_id(left)
    return known_id is not None and known_id == repository_id(right)


def verify_webhook_identity(repository: str, payload_id: object) -> bool:
    """Bind a renamed webhook name to its immutable GitHub repository ID."""
    known_id = repository_id(repository)
    if known_id is None:
        return True
    return type(payload_id) is int and payload_id == known_id
