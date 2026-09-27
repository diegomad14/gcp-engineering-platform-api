"""Bounded, cached availability checks before admitting a managed deployment."""

from threading import Lock
from time import monotonic
from urllib.parse import quote, urlsplit

from google.auth import default
from google.auth.transport.requests import AuthorizedSession

from .repository_identity import aliases

_cache: dict[tuple[str, str, str], tuple[float, list[str]]] = {}
_lock = Lock()


def availability(repository: str, connected_repository: str, image: str) -> list[str]:
    key = (repository, connected_repository, image)
    with _lock:
        cached = _cache.get(key)
        if cached and monotonic() - cached[0] < 60:
            return list(cached[1])
    blockers = []
    try:
        credentials, _ = default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        session = AuthorizedSession(credentials)
        with session:
            response = session.get(
                "https://cloudbuild.googleapis.com/v2/" + connected_repository,
                timeout=5,
            )
            if response.status_code != 200:
                blockers.append(
                    f"Connected repository is unavailable (HTTP {response.status_code})"
                )
            else:
                remote = str(response.json().get("remoteUri", "")).removesuffix(".git")
                allowed = {repository, *aliases(repository)}
                if remote.removeprefix("https://github.com/") not in allowed:
                    blockers.append(
                        "Connected repository does not match catalog identity"
                    )
            connection = connected_repository.rsplit("/repositories/", 1)[0]
            response = session.get(
                "https://cloudbuild.googleapis.com/v2/" + connection, timeout=5
            )
            if response.status_code != 200:
                blockers.append(
                    f"Cloud Build connection is unavailable (HTTP {response.status_code})"
                )
            else:
                info = response.json()
                stage = info.get("installationState", {}).get("stage")
                if info.get("disabled") or stage not in {None, "COMPLETE"}:
                    blockers.append("Cloud Build repository connection is not ready")
            parsed = urlsplit("https://" + image)
            parts = parsed.path.strip("/").split("/", 2)
            if (
                not parsed.hostname
                or not parsed.hostname.endswith("-docker.pkg.dev")
                or len(parts) != 3
            ):
                blockers.append(
                    "Executor image is not in the configured Artifact Registry"
                )
            else:
                location = parsed.hostname.removesuffix("-docker.pkg.dev")
                project, registry, artifact = parts
                endpoint = (
                    f"https://artifactregistry.googleapis.com/v1/projects/{project}/locations/{location}"
                    f"/repositories/{registry}/dockerImages/{quote(artifact, safe='')}"
                )
                response = session.get(endpoint, timeout=5)
                if response.status_code != 200:
                    blockers.append(
                        f"Pinned executor image is unavailable (HTTP {response.status_code})"
                    )
    except Exception:
        blockers.append(
            "Executor availability could not be verified; retry the readiness check"
        )
    with _lock:
        _cache[key] = (monotonic(), blockers)
    return list(blockers)
