from eng_platform_api.config import (
    _RELEASE_PLANNER_BROKEN_DIGEST,
    _RELEASE_PLANNER_RECOVERED_DIGEST,
    _effective_release_planner_image,
)


def test_known_broken_release_planner_digest_rolls_forward_to_pinned_recovery():
    assert (
        _effective_release_planner_image(_RELEASE_PLANNER_BROKEN_DIGEST)
        == _RELEASE_PLANNER_RECOVERED_DIGEST
    )


def test_unrelated_release_planner_digest_is_preserved():
    configured = "registry.example/release-planner@sha256:" + "a" * 64
    assert _effective_release_planner_image(f" {configured} ") == configured
