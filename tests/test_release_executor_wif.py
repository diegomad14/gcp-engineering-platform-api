"""Offline WIF protocol tests. All credential and token values are synthetic."""

import copy
import importlib.util
import io
import json
import traceback
import urllib.error
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest


BASE = "https://cgm-artemis-mcp-worker-pzzhmu7una-uc.a.run.app"
CANDIDATE = "https://candidate---cgm-artemis-mcp-worker-pzzhmu7una-uc.a.run.app"
SA = "deployer@example-project.iam.gserviceaccount.com"
IAM = f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{SA}"


@pytest.fixture
def engine(monkeypatch):
    path = Path(__file__).parents[1] / "docker/release-executor/release_executor.py"
    spec = importlib.util.spec_from_file_location("release_executor_wif", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for key in (
        "GOOGLE_APPLICATION_CREDENTIALS",
        "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in {
        "CGM_SERVICE": "cgm-artemis-mcp-worker",
        "CGM_PROJECT_ID": "example-project",
        "CGM_REGION": "us-central1",
        "CGM_RELEASE_SHA": "a" * 40,
        "CGM_HEALTH_PATH": "/health",
        "CGM_PRIVATE_RUNTIME": "true",
    }.items():
        monkeypatch.setenv(key, value)
    return module


@pytest.fixture
def wif(engine, monkeypatch):
    config = {
        "type": "external_account",
        "audience": "//iam.googleapis.com/projects/123456789/locations/global/workloadIdentityPools/release/providers/github",
        "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
        "token_url": "https://sts.googleapis.com/v1/token",
        "service_account_impersonation_url": IAM + ":generateAccessToken",
        "credential_source": {
            "url": "https://pipelines.actions.githubusercontent.com/idtoken?audience=fixture",
            "headers": {"Authorization": "Bearer fixture-request-only"},
            "format": {"type": "json", "subject_token_field_name": "value"},
        },
    }
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/fixture/wif.json")
    # Configuration and token-shaped fixtures are kept in memory, never on disk.
    reader = mock.Mock(
        side_effect=lambda *a, **kw: io.BytesIO(json.dumps(config).encode())
    )
    monkeypatch.setattr(engine.pathlib.Path, "open", reader)
    requests = []
    replies = [
        {"value": "fixture-subject-only"},
        {"access_token": "fixture-federated-only", "token_type": "Bearer"},
        {"token": "fixture-id-only"},
    ]

    def request(req, timeout):
        requests.append(req)
        assert timeout == 20
        reply = replies[len(requests) - 1]
        if isinstance(reply, BaseException):
            raise reply
        return io.BytesIO(json.dumps(reply).encode())

    opener = SimpleNamespace(open=request)
    builder = mock.Mock(return_value=opener)
    monkeypatch.setattr(engine.urllib.request, "build_opener", builder)
    legacy = mock.Mock(side_effect=AssertionError("WIF must not use gcloud tokens"))
    monkeypatch.setattr(engine, "run", legacy)
    return SimpleNamespace(
        config=config,
        requests=requests,
        replies=replies,
        reader=reader,
        builder=builder,
        legacy=legacy,
    )


def test_federated_principal_mints_id_for_same_deployer_without_double_impersonation(
    engine, wif
):
    original = copy.deepcopy(wif.config)
    assert engine.runtime_identity_token(BASE) == "fixture-id-only"
    assert wif.config == original
    source, exchange, mint = wif.requests
    assert source.full_url == original["credential_source"]["url"]
    assert source.get_header("Authorization") == "Bearer fixture-request-only"
    assert exchange.full_url == "https://sts.googleapis.com/v1/token"
    assert exchange.get_header("Authorization") is None
    assert urllib.parse.parse_qs(exchange.data.decode()) == {
        "audience": [original["audience"]],
        "grant_type": ["urn:ietf:params:oauth:grant-type:token-exchange"],
        "requested_token_type": ["urn:ietf:params:oauth:token-type:access_token"],
        "subject_token_type": [original["subject_token_type"]],
        "subject_token": ["fixture-subject-only"],
        "scope": ["https://www.googleapis.com/auth/cloud-platform"],
    }
    assert mint.full_url == IAM + ":generateIdToken"
    assert mint.get_header("Authorization") == "Bearer fixture-federated-only"
    assert json.loads(mint.data) == {"audience": BASE, "includeEmail": True}
    assert all(":generateAccessToken" not in req.full_url for req in wif.requests)
    assert all(
        isinstance(call.args[0], engine._NoTokenRedirect)
        for call in wif.builder.call_args_list
    )
    wif.legacy.assert_not_called()


def test_each_call_renews_subject_federated_and_id_tokens(engine, wif):
    wif.replies.extend(
        [
            {"value": "renewed-subject"},
            {"access_token": "renewed-federated"},
            {"token": "renewed-id"},
        ]
    )
    assert engine.runtime_identity_token(BASE) == "fixture-id-only"
    assert engine.runtime_identity_token(BASE) == "renewed-id"
    assert wif.reader.call_count == 2
    assert len(wif.requests) == 6
    assert wif.requests[-1].get_header("Authorization") == "Bearer renewed-federated"


@pytest.mark.parametrize("mode", ["readiness", "smoke"])
def test_private_candidate_uses_base_audience_and_only_id_token_at_runtime(
    engine, wif, monkeypatch, mode
):
    monkeypatch.setattr(engine, "_service_url", lambda *a: BASE)
    probes = []

    def probe(req, timeout):
        probes.append(req)
        assert req.get_header("Authorization") == "Bearer fixture-id-only"
        if mode == "readiness":
            raise urllib.error.HTTPError(
                req.full_url,
                503,
                "inactive",
                {},
                io.BytesIO(
                    json.dumps(
                        {
                            "detail": {
                                "resource": "cgm-artemis-mcp-worker",
                                "release_sha": "a" * 40,
                                "scope": "runtime-v1",
                                "reason": "release_not_activated",
                            },
                        }
                    ).encode()
                ),
            )
        return SimpleNamespace(__enter__=None)

    if mode == "smoke":

        class Response(io.BytesIO):
            status = 200

        def probe(req, timeout):
            probes.append(req)
            assert req.get_header("Authorization") == "Bearer fixture-id-only"
            return Response()

    monkeypatch.setattr(engine.urllib.request, "urlopen", probe)
    if mode == "readiness":
        engine.runtime_ready(CANDIDATE, candidate=True)
    else:
        engine._smoke(CANDIDATE)
    assert probes[0].full_url == CANDIDATE + (
        "/ready" if mode == "readiness" else "/health"
    )
    assert json.loads(wif.requests[-1].data)["audience"] == BASE


@pytest.mark.parametrize("mode", ["readiness", "smoke"])
def test_public_runtime_never_reads_credentials_or_mints(
    engine, wif, monkeypatch, mode
):
    monkeypatch.setenv("CGM_PRIVATE_RUNTIME", "false")

    class Response(io.BytesIO):
        status = 200

    def probe(req, timeout):
        assert req.get_header("Authorization") is None
        return Response(
            json.dumps(
                {
                    "resource": "cgm-artemis-mcp-worker",
                    "release_sha": "a" * 40,
                    "scope": "runtime-v1",
                    "status": "ready",
                    "reason": "active",
                }
            ).encode()
        )

    monkeypatch.setattr(engine.urllib.request, "urlopen", probe)
    if mode == "readiness":
        engine.runtime_ready(BASE)
    else:
        engine._smoke(BASE)
    wif.reader.assert_not_called()
    assert wif.requests == []


@pytest.mark.parametrize("stage", [0, 1, 2])
def test_source_sts_or_iam_failure_is_sanitized_and_has_no_fallback(
    engine, wif, stage, capsys
):
    wif.replies[stage] = urllib.error.HTTPError(
        "https://private.invalid?secret=fixture-secret",
        403,
        "fixture-secret",
        {},
        io.BytesIO(b"fixture-secret"),
    )
    with pytest.raises(
        RuntimeError, match=f"runtime token {['source', 'STS', 'IAM'][stage]} HTTP 403"
    ):
        try:
            engine.runtime_identity_token(BASE)
        except RuntimeError:
            rendered = traceback.format_exc()
            assert "fixture-secret" not in rendered
            raise
    assert len(wif.requests) == stage + 1
    assert "fixture" not in capsys.readouterr().out + capsys.readouterr().err
    wif.legacy.assert_not_called()


@pytest.mark.parametrize(
    "stage,payload",
    [
        (0, {}),
        (0, {"value": ""}),
        (1, {"access_token": None}),
        (2, {"token": "bad\nheader"}),
    ],
)
def test_invalid_token_response_stops_chain(engine, wif, stage, payload):
    wif.replies[stage] = payload
    with pytest.raises(RuntimeError, match="response is invalid"):
        engine.runtime_identity_token(BASE)
    assert len(wif.requests) == stage + 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("token_url", "https://attacker.invalid/token"),
        ("audience", "//iam.googleapis.com/invalid"),
        ("subject_token_type", "arbitrary"),
        (
            "service_account_impersonation_url",
            "https://attacker.invalid:generateAccessToken",
        ),
        ("service_account_impersonation_url", IAM + ":generateIdToken"),
    ],
)
def test_invalid_wif_config_fails_before_network(engine, wif, field, value):
    wif.config[field] = value
    with pytest.raises(RuntimeError, match="configuration is invalid"):
        engine.runtime_identity_token(BASE)
    assert wif.requests == []


@pytest.mark.parametrize(
    "url",
    [
        "http://pipelines.actions.githubusercontent.com/token",
        "https://actions.githubusercontent.com.attacker.invalid/token",
        "https://user@pipelines.actions.githubusercontent.com/token",
        "https://pipelines.actions.githubusercontent.com:443/token",
        "https://pipelines.actions.githubusercontent.com/token#fragment",
    ],
)
def test_untrusted_subject_token_source_is_not_contacted(engine, wif, url):
    wif.config["credential_source"]["url"] = url
    with pytest.raises(RuntimeError, match="configuration is invalid"):
        engine.runtime_identity_token(BASE)
    assert wif.requests == []


@pytest.mark.parametrize(
    "audience", [CANDIDATE, BASE + "/ready", BASE + "?a=b", "https://attacker.invalid"]
)
def test_only_canonical_cloud_run_audience_is_accepted(engine, wif, audience):
    with pytest.raises(RuntimeError, match="canonical"):
        engine.runtime_identity_token(audience)
    wif.reader.assert_not_called()
    assert wif.requests == []


@pytest.mark.parametrize("body", [b"invalid-json", b"[]", b"x" * 65537])
def test_malformed_or_oversized_config_is_sanitized(engine, wif, body):
    wif.reader.side_effect = lambda *a, **kw: io.BytesIO(body)
    with pytest.raises(RuntimeError, match="configuration is invalid"):
        engine.runtime_identity_token(BASE)
    assert wif.requests == []


def test_missing_credential_file_does_not_fall_back(engine, wif):
    wif.reader.side_effect = FileNotFoundError("fixture-secret")
    with pytest.raises(RuntimeError, match="configuration is invalid"):
        engine.runtime_identity_token(BASE)
    wif.legacy.assert_not_called()


def test_credential_override_is_authoritative(engine, wif, monkeypatch):
    monkeypatch.setenv(
        "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE", "/fixture/override.json"
    )
    paths = []

    def read(path, *args, **kwargs):
        paths.append(str(path))
        return io.BytesIO(json.dumps(wif.config).encode())

    monkeypatch.setattr(engine.pathlib.Path, "open", read)
    engine.runtime_identity_token(BASE)
    assert paths == ["/fixture/override.json"]


def test_existing_non_wif_and_absent_adc_paths_retain_cli(engine, monkeypatch):
    legacy = mock.Mock(return_value="legacy-id")
    monkeypatch.setattr(engine, "run", legacy)
    assert engine.runtime_identity_token(BASE) == "legacy-id"
    monkeypatch.setenv(
        "GOOGLE_APPLICATION_CREDENTIALS", "/fixture/service-account.json"
    )
    monkeypatch.setattr(
        engine.pathlib.Path,
        "open",
        lambda *a, **kw: io.BytesIO(b'{"type":"service_account"}'),
    )
    assert engine.runtime_identity_token(BASE) == "legacy-id"
    assert (
        legacy.call_args_list
        == [mock.call("gcloud", "auth", "print-identity-token", f"--audiences={BASE}")]
        * 2
    )


def test_auth_transport_rejects_redirect_and_bounds_or_sanitizes_bad_responses(
    engine, monkeypatch
):
    handler = engine._NoTokenRedirect()
    assert (
        handler.redirect_request(
            None, None, 302, "redirect", {}, "https://attacker.invalid"
        )
        is None
    )
    for body in (b"not-json", b"[]", b"x" * 65537):
        monkeypatch.setattr(
            engine.urllib.request,
            "build_opener",
            lambda *a: SimpleNamespace(open=lambda *a, **kw: io.BytesIO(body)),
        )
        with pytest.raises(RuntimeError, match="runtime token source unavailable"):
            engine._token_json(
                engine.urllib.request.Request("https://fixture.invalid"), "source"
            )


def test_mint_failure_stops_deployment_before_grant_or_traffic(
    engine, wif, monkeypatch
):
    wif.replies[2] = urllib.error.HTTPError(IAM, 403, "fixture-only", {}, None)
    monkeypatch.setattr(engine, "emit", lambda *a, **kw: None)
    monkeypatch.setattr(engine, "_traffic", lambda *a: {"old": 100})
    monkeypatch.setattr(
        engine, "revision_runtime", lambda *a: {"APP_RELEASE_SHA": "b" * 40}
    )
    monkeypatch.setattr(engine, "verify_artemis_web", lambda: None)
    monkeypatch.setattr(
        engine, "_deploy_candidate", lambda *a: ("candidate", CANDIDATE)
    )
    monkeypatch.setattr(engine, "_service_url", lambda *a: BASE)
    grant, traffic, probe = mock.Mock(), mock.Mock(), mock.Mock()
    monkeypatch.setattr(engine, "runtime_grant", grant)
    monkeypatch.setattr(engine, "_set_traffic", traffic)
    monkeypatch.setattr(engine.urllib.request, "urlopen", probe)
    with pytest.raises(RuntimeError, match="runtime token IAM HTTP 403"):
        engine.deploy_runtime("image")
    grant.assert_not_called()
    traffic.assert_not_called()
    probe.assert_not_called()
