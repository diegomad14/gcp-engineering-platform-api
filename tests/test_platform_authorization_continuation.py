"""Approval waits never renew a ticket or authorize another workflow attempt."""

import base64
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.testclient import TestClient

from eng_platform_api.config import config
from eng_platform_api.main import app
from eng_platform_api.services import (
    mcp_grants,
    release_authorization as authorization,
    release_authorization_store as store,
    workflow_identity,
)


class AlreadyExists(Exception):
    pass


class Document:
    def __init__(self, database, key):
        self.database, self.key = database, key

    def create(self, value):
        if self.key in self.database.entries:
            raise AlreadyExists()
        self.database.entries[self.key] = copy.deepcopy(value)
        self.database.creates += 1

    def get(self):
        value = copy.deepcopy(self.database.entries.get(self.key))
        return SimpleNamespace(exists=value is not None, to_dict=lambda: value)

    def set(self, value, *, merge):
        assert merge
        self.database.entries.setdefault(self.key, {}).update(value)


class Collection:
    def __init__(self):
        self.entries, self.creates = {}, 0

    def document(self, key):
        return Document(self, key)


@pytest.fixture
def platform(monkeypatch):
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    monkeypatch.setattr(config.github, "release_signing_private_key", pem)
    monkeypatch.setattr(config.github, "release_signing_public_key", "")
    oidc_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(
        workflow_identity._keys,
        "get_signing_key_from_jwt",
        lambda _: SimpleNamespace(key=oidc_key.public_key()),
    )
    database = Collection()
    monkeypatch.setattr(store, "_firestore_collection", lambda: database)
    return SimpleNamespace(
        database=database, oidc_key=oidc_key, signer=key, client=TestClient(app)
    )


def ticket(*, kind="deploy", enabled=True, service="cgm-artemis-api"):
    expected = {
        "repository": f"diegomad14/{service}",
        "service_name": service,
        "tag": "v1.2.3",
        "sha": "a" * 40,
        "github_deployment_id": "123",
        "kind": kind,
    }
    token, claims = authorization.issue(
        **{
            key: value
            for key, value in expected.items()
            if key != "github_deployment_id"
        },
        github_deployment_id=123,
        requested_by="diegomad14",
        configuration={"server_owned": "approved"},
        workflow_continuation=enabled,
        target_revision="previous-revision" if kind == "rollback" else "",
    )
    body = {
        **expected,
        "token": token,
        "configuration_hash": claims["configuration_hash"],
        "target_revision": claims.get("target_revision", ""),
    }
    return body, claims


def oidc(platform, body, *, stage="", changes=None):
    now = int(datetime.now(timezone.utc).timestamp())
    repo = body["repository"]
    reference = (
        f"refs/tags/{body['tag']}" if body["kind"] == "deploy" else "refs/heads/main"
    )
    environment = "candidate" if stage == "candidate" else "production" if stage else ""
    claims = {
        "iss": workflow_identity.ISSUER,
        "aud": workflow_identity.AUDIENCE,
        "iat": now,
        "nbf": now - 1,
        "exp": now + 300,
        "repository": repo,
        "sub": f"repo:{repo}:environment:{environment}"
        if environment
        else f"repo:{repo}:ref:{reference}",
        "ref": reference,
        "workflow_ref": f"{repo}/.github/workflows/platform-{body['kind']}.yml@{reference}",
        # Rollback's workflow main SHA differs from its immutable runtime target.
        "sha": body["sha"] if body["kind"] == "deploy" else "b" * 40,
        "run_id": "456",
        "run_attempt": "1",
        "event_name": "workflow_dispatch",
    }
    if environment:
        claims["environment"] = environment
    claims.update(changes or {})
    return jwt.encode(claims, platform.oidc_key, algorithm="RS256")


def request(platform, body, *, mode="consume", stage="", changes=None):
    return platform.client.post(
        "/api/internal/release-authorizations/consume",
        json={**body, "mode": mode, "stage": stage},
        headers={"x-github-oidc": oidc(platform, body, stage=stage, changes=changes)},
    )


@pytest.mark.parametrize(
    "kind,stage",
    [("deploy", "candidate"), ("deploy", "promote"), ("rollback", "rollback")],
)
def test_consumed_ticket_continues_same_run_after_wait_without_extending_ttl(
    platform, monkeypatch, kind, stage
):
    body, claims = ticket(kind=kind)
    assert claims["exp"] - claims["iat"] == authorization.TOKEN_TTL_SECONDS == 300
    assert request(platform, body).status_code == 200
    initial = copy.deepcopy(platform.database.entries[claims["jti"]])
    monkeypatch.setattr(authorization.time, "time", lambda: claims["exp"] + 3600)
    with pytest.raises(authorization.ReleaseAuthorizationError):
        authorization.verify(body["token"], {"kind": kind})
    assert request(platform, body, mode="continue", stage=stage).status_code == 200
    assert platform.database.creates == 1
    assert platform.database.entries[claims["jti"]] == initial
    assert request(platform, body).status_code == 401
    if kind == "rollback":
        assert initial["workflow_identity"]["sha"] == "b" * 40
        assert initial["sha"] == "a" * 40


def test_continuation_revalidates_each_approved_stage_without_consuming_again(platform):
    body, claims = ticket()
    assert request(platform, body).status_code == 200
    for stage in ["candidate", "promote"]:
        assert request(platform, body, mode="continue", stage=stage).status_code == 200
    assert platform.database.creates == 1
    assert platform.database.entries[claims["jti"]]["exp"] == claims["exp"]
    assert request(platform, body).status_code == 409


@pytest.mark.parametrize("expired", [False, True])
def test_unconsumed_or_revoked_ticket_never_continues(platform, monkeypatch, expired):
    body, claims = ticket()
    if expired:
        monkeypatch.setattr(authorization.time, "time", lambda: claims["exp"] + 1)
    assert request(platform, body, mode="continue", stage="promote").status_code == 401
    assert platform.database.creates == 0
    monkeypatch.setattr(authorization.time, "time", lambda: claims["iat"])
    assert request(platform, body).status_code == 200
    store.revoke(claims["jti"])
    assert request(platform, body, mode="continue", stage="promote").status_code == 401


@pytest.mark.parametrize(
    "field,value",
    [
        ("repository", "owner/different"),
        ("ref", "refs/heads/main"),
        (
            "workflow_ref",
            "diegomad14/cgm-artemis-api/.github/workflows/other.yml@refs/tags/v1.2.3",
        ),
        (
            "job_workflow_ref",
            "owner/caller/.github/workflows/reusable.yml@refs/heads/main",
        ),
        ("sha", "c" * 40),
        ("run_id", "457"),
        ("run_attempt", "2"),
        ("event_name", "push"),
        ("environment", "candidate"),
        ("sub", "repo:diegomad14/cgm-artemis-api:environment:candidate"),
        ("exp", 1),
    ],
)
def test_continue_rejects_another_identity_or_environment(platform, field, value):
    body, claims = ticket()
    assert request(platform, body).status_code == 200
    previous = copy.deepcopy(platform.database.entries[claims["jti"]])
    assert (
        request(
            platform, body, mode="continue", stage="promote", changes={field: value}
        ).status_code
        == 401
    )
    assert platform.database.entries[claims["jti"]] == previous


@pytest.mark.parametrize(
    "field,value",
    [
        ("configuration_hash", "c" * 64),
        ("target_revision", "different-revision"),
        ("service_name", "cgm-artemis-job-worker"),
        ("github_deployment_id", "124"),
    ],
)
def test_continue_rejects_changed_signed_context(platform, field, value):
    body, _ = ticket(kind="rollback")
    assert request(platform, body).status_code == 200
    assert (
        request(
            platform, body | {field: value}, mode="continue", stage="rollback"
        ).status_code
        == 401
    )


@pytest.mark.parametrize(
    "kind,stage",
    [
        ("deploy", "rollback"),
        ("rollback", "promote"),
        ("rollback", "candidate"),
        ("deploy", ""),
    ],
)
def test_continue_stage_must_match_operation(platform, kind, stage):
    body, _ = ticket(kind=kind)
    assert request(platform, body).status_code == 200
    assert request(platform, body, mode="continue", stage=stage).status_code == 401


def test_initial_consume_requires_oidc_without_environment_and_durable_backend(
    platform, monkeypatch
):
    body, _ = ticket()
    assert (
        request(platform, body, changes={"environment": "production"}).status_code
        == 401
    )
    assert (
        platform.client.post(
            "/api/internal/release-authorizations/consume", json=body
        ).status_code
        == 401
    )
    monkeypatch.setattr(store, "_firestore_collection", lambda: None)
    assert request(platform, body).status_code == 401
    assert request(platform, body, mode="continue", stage="promote").status_code == 401
    assert platform.database.creates == 0


def test_continue_rechecks_operator_and_current_mcp_grant(platform, monkeypatch):
    body, _ = ticket()
    assert request(platform, body).status_code == 200
    monkeypatch.setattr(config.auth, "allowed_logins", ())
    assert request(platform, body, mode="continue", stage="promote").status_code == 401
    monkeypatch.setattr(
        mcp_grants,
        "release_claims",
        lambda _: {
            "mcp_grant_id": "current-grant",
            "mcp_authority_policy": mcp_grants.POLICY,
        },
    )
    monkeypatch.setattr(mcp_grants, "validate", lambda *_: {})
    body, _ = ticket()
    assert request(platform, body).status_code == 200

    def revoked(*_):
        raise HTTPException(401, "Grant revoked during approval")

    monkeypatch.setattr(mcp_grants, "validate", revoked)
    assert request(platform, body, mode="continue", stage="promote").status_code == 401


def test_legacy_ticket_cannot_gain_continuation_and_unknown_modes_rejected(platform):
    body, _ = ticket(enabled=False)
    assert request(platform, body).status_code == 200
    assert request(platform, body, mode="continue", stage="promote").status_code == 401
    assert request(platform, body, mode="renew", stage="promote").status_code == 422
    assert (
        request(platform, body, mode="continue", stage="arbitrary").status_code == 422
    )
    assert request(platform, body, stage="promote").status_code == 401


def test_continuation_capability_requires_server_platform_scope(platform):
    with pytest.raises(authorization.ReleaseAuthorizationError):
        authorization.issue(
            repository="owner/repo",
            service_name="example-api",
            tag="v1.2.3",
            sha="a" * 40,
            github_deployment_id=1,
            requested_by="diegomad14",
            kind="deploy",
            configuration={},
            workflow_continuation=True,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"execution_repository": "owner/central"},
        {"execution_mode": "cloud-build"},
        {"audience": "different-audience"},
        {"configuration_hash": "short"},
    ],
)
def test_continuation_cannot_be_issued_for_another_execution_scope(platform, changes):
    with pytest.raises(authorization.ReleaseAuthorizationError):
        authorization.issue(
            repository="diegomad14/cgm-artemis-api",
            service_name="cgm-artemis-api",
            tag="v1.2.3",
            sha="a" * 40,
            github_deployment_id=123,
            requested_by="diegomad14",
            kind="deploy",
            workflow_continuation=True,
            **{"configuration_hash": "a" * 64, **changes},
        )


@pytest.mark.parametrize(
    "mode,enabled,expired,response,expected",
    [
        ("consume", True, False, 200, 0),
        ("consume", False, False, 200, 0),
        ("consume", True, True, 200, 1),
        ("continue", True, True, 200, 0),
        ("continue", True, True, 401, 1),
        ("continue", False, True, 200, 1),
    ],
)
def test_action_only_allows_expired_ticket_via_authenticated_continue(
    platform, monkeypatch, mode, enabled, expired, response, expected
):
    now = int(datetime.now(timezone.utc).timestamp())
    if expired:
        monkeypatch.setattr(authorization.time, "time", lambda: now - 600)
    body, _ = ticket(enabled=enabled)
    public = (
        platform.signer.public_key()
        .public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        .decode()
    )
    script = (
        Path(__file__).resolve().parents[1]
        / ".github/actions/verify-release-authorization/verify-release-authorization.mjs"
    )
    env = {
        **__import__("os").environ,
        "PLATFORM_AUTHORIZATION": body["token"],
        "ENG_PLATFORM_RELEASE_SIGNING_PUBLIC_KEY": public,
        "PLATFORM_API_URL": "https://platform.invalid",
        "AUTHORIZATION_MODE": mode,
        "AUTHORIZATION_STAGE": "promote" if mode == "continue" else "",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.invalid/token",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "test-request-token",
        **{
            "EXPECTED_" + key.upper(): str(value)
            for key, value in body.items()
            if key not in {"token", "configuration_hash"}
        },
        "EXPECTED_DEPLOYMENT_ID": body["github_deployment_id"],
        "CONTINUATION_ENABLED": "true" if enabled else "false",
    }
    shim = """
      import {pathToFileURL} from 'node:url';
      globalThis.fetch = async (url, options) => {
        if (String(url).startsWith('https://oidc.invalid/'))
          return {ok: true, json: async () => ({value: 'fresh-test-oidc'})};
        if (process.env.CONTINUATION_ENABLED === 'true' && !options.headers['x-github-oidc'])
          throw Error('Missing fresh OIDC');
        const body = JSON.parse(options.body);
        if (process.env.CONTINUATION_ENABLED === 'true') {
          if (body.mode !== process.env.AUTHORIZATION_MODE || body.stage !== process.env.AUTHORIZATION_STAGE)
            throw Error('Mode or stage was not forwarded');
        } else if ('mode' in body || 'stage' in body) {
          throw Error('Legacy consume unexpectedly changed the published request schema');
        }
        return {ok: Number(process.env.MOCK_STATUS) === 200, status: Number(process.env.MOCK_STATUS)};
      };
      await import(pathToFileURL(process.argv[1]).href);
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", shim, str(script)],
        env=env | {"MOCK_STATUS": str(response)},
        capture_output=True,
        text=True,
    )
    assert bool(result.returncode) == bool(expected), result.stderr


def test_signed_context_hash_cannot_be_changed_without_invalidating_ticket(platform):
    body, _ = ticket()
    assert request(platform, body).status_code == 200
    parts = body["token"].split(".")
    payload = json.loads(
        base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
    )
    payload["configuration_hash"] = hashlib.sha256(b"modified").hexdigest()
    parts[1] = (
        base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    )
    assert (
        request(
            platform,
            body | {"token": ".".join(parts)},
            mode="continue",
            stage="promote",
        ).status_code
        == 401
    )


def immutable_claims(body, stage=""):
    owner, name = body["repository"].split("/", 1)
    reference = (
        f"refs/tags/{body['tag']}" if body["kind"] == "deploy" else "refs/heads/main"
    )
    environment = "candidate" if stage == "candidate" else "production" if stage else ""
    context = f"environment:{environment}" if environment else f"ref:{reference}"
    repository_id = "1306114872" if name == "cgm-artemis-web" else "1306114845"
    return {
        "repository_owner": owner,
        "repository_owner_id": "97854332",
        "repository_id": repository_id,
        "sub": f"repo:{owner}@97854332/{name}@{repository_id}:{context}",
    }


@pytest.mark.parametrize(
    "kind,stage",
    [("deploy", "candidate"), ("deploy", "promote"), ("rollback", "rollback")],
)
@pytest.mark.parametrize("service", ["cgm-artemis-api", "cgm-artemis-web"])
def test_immutable_subject_consumes_and_continues_same_identity(
    platform, kind, stage, service
):
    body, claims = ticket(kind=kind, service=service)
    assert request(platform, body, changes=immutable_claims(body)).status_code == 200
    stored = platform.database.entries[claims["jti"]]["workflow_identity"]
    assert stored["repository_owner_id"] == "97854332"
    assert stored["repository_id"] == immutable_claims(body)["repository_id"]
    assert (
        request(
            platform,
            body,
            mode="continue",
            stage=stage,
            changes=immutable_claims(body, stage),
        ).status_code
        == 200
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("repository_owner", "other"),
        ("repository_owner_id", "999"),
        ("repository_owner_id", ""),
        ("repository_owner_id", "0"),
        ("repository_owner_id", "01"),
        ("repository_owner_id", 97854332),
        ("repository_id", "999"),
        ("repository_id", ""),
        ("repository_id", "0"),
        ("repository_id", "01"),
        ("repository_id", True),
        ("sub", "repo:diegomad14@97854332/other@1306114845:ref:refs/tags/v1.2.3"),
        (
            "sub",
            "repo:diegomad14@97854332/cgm-artemis-api@1306114845:environment:production",
        ),
    ],
)
def test_inconsistent_immutable_subject_is_rejected_before_consumption(
    platform, field, value
):
    body, claims = ticket()
    changes = immutable_claims(body) | {field: value}
    assert request(platform, body, changes=changes).status_code == 401
    assert claims["jti"] not in platform.database.entries


@pytest.mark.parametrize("field", ["repository_owner_id", "repository_id"])
def test_immutable_continuation_cannot_change_identities(platform, field):
    body, _ = ticket()
    assert request(platform, body, changes=immutable_claims(body)).status_code == 200
    changes = immutable_claims(body, "candidate")
    changes[field] = "999"
    changes["sub"] = changes["sub"].replace(
        "@97854332" if field == "repository_owner_id" else "@1306114845", "@999"
    )
    assert (
        request(
            platform, body, mode="continue", stage="candidate", changes=changes
        ).status_code
        == 401
    )
