"""Verify GitHub-issued identity, never identity asserted by dispatch inputs."""

import jwt

from .release_authorization import ReleaseAuthorizationError

ISSUER = "https://token.actions.githubusercontent.com"
AUDIENCE = "engineering-platform-release"
_keys = jwt.PyJWKClient(f"{ISSUER}/.well-known/jwks", timeout=10)


def _decode(token: str, *, platform: bool = False) -> dict:
    if not token or len(token) > 16384:
        raise ReleaseAuthorizationError("Missing workflow identity")
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
                    "sub",
                    "repository",
                    "ref",
                    "workflow_ref",
                    "run_id",
                    "run_attempt",
                    "event_name",
                ]
                + (["sha"] if platform else [])
            },
        )
    except jwt.PyJWTError:
        raise ReleaseAuthorizationError("Invalid workflow identity") from None
    return claims


def verify(token: str, execution_repository: str, kind: str) -> dict:
    if not execution_repository:
        raise ReleaseAuthorizationError("Missing workflow identity")
    claims = _decode(token)
    expected = (
        f"{execution_repository}/.github/workflows/central-release.yml@refs/heads/main"
    )
    if (
        kind not in {"deploy", "rollback"}
        or claims["repository"] != execution_repository
        or claims["ref"] != "refs/heads/main"
        or claims["workflow_ref"] != expected
        or claims.get("job_workflow_ref", expected) != expected
        or claims["event_name"] != "workflow_dispatch"
    ):
        raise ReleaseAuthorizationError("Untrusted release workflow")
    return {
        key: str(claims[key])
        for key in ("repository", "workflow_ref", "run_id", "run_attempt")
    }


def verify_platform(
    token: str,
    repository: str,
    kind: str,
    tag: str,
    release_sha: str,
    *,
    mode: str,
    stage: str,
) -> dict[str, str]:
    """Bind staged platform work to its exact workflow and approved environment."""
    import re

    claims = _decode(token, platform=True)
    reference = f"refs/tags/{tag}" if kind == "deploy" else "refs/heads/main"
    expected = f"{repository}/.github/workflows/platform-{kind}.yml@{reference}"
    environment = ""
    if mode == "continue":
        environments = (
            {"candidate": "candidate", "promote": "production"}
            if kind == "deploy"
            else {"rollback": "production"}
        )
        environment = environments.get(stage, "")
        if not environment:
            raise ReleaseAuthorizationError("Invalid platform continuation stage")
    elif mode != "consume" or stage:
        raise ReleaseAuthorizationError("Invalid platform consumption stage")
    subject = (
        f"repo:{repository}:environment:{environment}"
        if environment
        else f"repo:{repository}:ref:{reference}"
    )
    if (
        kind not in {"deploy", "rollback"}
        or claims["repository"] != repository
        or claims["ref"] != reference
        or claims["workflow_ref"] != expected
        or claims.get("job_workflow_ref", expected) != expected
        or claims["event_name"] != "workflow_dispatch"
        or claims["sub"] != subject
        or claims.get("environment", "") != environment
        or not re.fullmatch(r"[0-9a-f]{40}", str(claims["sha"]))
        or (kind == "deploy" and claims["sha"] != release_sha)
        or not re.fullmatch(r"[1-9][0-9]*", str(claims["run_id"]))
        or not re.fullmatch(r"[1-9][0-9]*", str(claims["run_attempt"]))
    ):
        raise ReleaseAuthorizationError("Untrusted platform workflow")
    return {
        key: str(claims[key])
        for key in ("repository", "ref", "workflow_ref", "sha", "run_id", "run_attempt")
    }
