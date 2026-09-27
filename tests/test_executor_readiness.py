from types import SimpleNamespace

import pytest

from eng_platform_api.services import executor_readiness as readiness


@pytest.mark.parametrize(
    "failure",
    [None, "repository", "connection", "image", "disabled", "identity", "transport"],
)
def test_live_executor_availability_is_explicit_and_cached(monkeypatch, failure):
    readiness._cache.clear()
    calls = []

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, timeout):
            calls.append(url)
            if failure == "transport":
                raise RuntimeError("private credential detail")
            if "dockerImages" in url:
                kind, body = "image", {}
            elif "/repositories/" in url:
                kind, body = (
                    "repository",
                    {
                        "remoteUri": "https://github.com/org/repo.git"
                        if failure != "identity"
                        else "https://github.com/other/repo.git"
                    },
                )
            else:
                kind, body = (
                    "connection",
                    {
                        "disabled": failure == "disabled",
                        "installationState": {"stage": "COMPLETE"},
                    },
                )
            return SimpleNamespace(
                status_code=403 if kind == failure else 200, json=lambda: body
            )

    monkeypatch.setattr(readiness, "default", lambda **kw: (object(), None))
    monkeypatch.setattr(readiness, "AuthorizedSession", lambda credentials: Session())
    args = (
        "org/repo",
        "projects/p/locations/r/connections/c/repositories/repo",
        "us-central1-docker.pkg.dev/p/r/executor@sha256:" + "a" * 64,
    )
    blockers = readiness.availability(*args)
    assert bool(blockers) == bool(failure)
    assert "private credential" not in str(blockers)
    count = len(calls)
    assert readiness.availability(*args) == blockers
    assert len(calls) == count
