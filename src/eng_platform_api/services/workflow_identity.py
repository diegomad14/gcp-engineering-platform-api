"""Verify GitHub-issued identity, never identity asserted by dispatch inputs."""

import jwt

from .release_authorization import ReleaseAuthorizationError

ISSUER = "https://token.actions.githubusercontent.com"
AUDIENCE = "engineering-platform-release"
_keys = jwt.PyJWKClient(f"{ISSUER}/.well-known/jwks", timeout=10)


def verify(token: str, execution_repository: str, kind: str) -> dict:
    if not token or len(token) > 16384 or not execution_repository:
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
            },
        )
    except jwt.PyJWTError:
        raise ReleaseAuthorizationError("Invalid workflow identity") from None
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
