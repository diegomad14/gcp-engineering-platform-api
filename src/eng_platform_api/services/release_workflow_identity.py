"""Verify GitHub OIDC identity for quality/release callbacks."""

from __future__ import annotations

import jwt

ISSUER = "https://token.actions.githubusercontent.com"
AUDIENCE = "engineering-platform-release-orchestrator"
_keys = jwt.PyJWKClient(f"{ISSUER}/.well-known/jwks", timeout=10)
_WORKFLOWS = {
    "pr_quality": "eng-platform-quality.yml",
    "main_release": "eng-platform-release.yml",
}


class ReleaseWorkflowIdentityError(RuntimeError):
    pass


def workflow_file(operation: str) -> str:
    """Return the only workflow allowed to drive an operation."""
    try:
        return _WORKFLOWS[operation]
    except KeyError as exc:
        raise ReleaseWorkflowIdentityError("Unknown release operation") from exc


def matches_workflow_path(operation: str, path: str) -> bool:
    """Match the exact repository workflow, not merely another run for a SHA."""
    normalized = path.split("@", 1)[0].lstrip("/")
    return normalized == f".github/workflows/{workflow_file(operation)}"


def verify(token: str, execution: dict) -> dict[str, str]:
    if not token or len(token) > 16_384:
        raise ReleaseWorkflowIdentityError("Missing workflow identity")
    try:
        key = _keys.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={
                "require": [
                    "exp",
                    "iat",
                    "nbf",
                    "iss",
                    "aud",
                    "repository",
                    "ref",
                    "workflow_ref",
                    "run_id",
                    "run_attempt",
                    "event_name",
                    "sha",
                ]
            },
        )
    except jwt.PyJWTError:
        raise ReleaseWorkflowIdentityError("Invalid workflow identity") from None

    operation = str(execution["operation"])
    workflow = workflow_file(operation)
    expected_suffix = f"/.github/workflows/{workflow}@refs/heads/main"
    expected_event = "pull_request_target" if operation == "pr_quality" else "push"
    if (
        claims.get("repository") != execution.get("repository")
        or claims.get("event_name") != expected_event
        or not str(claims.get("workflow_ref", "")).endswith(expected_suffix)
        or claims.get("ref") != "refs/heads/main"
        or (operation == "pr_quality" and claims.get("base_ref") != "main")
        or (
            operation == "main_release"
            and claims.get("sha") != execution.get("head_sha")
        )
    ):
        raise ReleaseWorkflowIdentityError("Untrusted quality workflow")
    return {
        key: str(claims[key])
        for key in (
            "repository",
            "ref",
            "workflow_ref",
            "run_id",
            "run_attempt",
            "event_name",
            "sha",
        )
    }
