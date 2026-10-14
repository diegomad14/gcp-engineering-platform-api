"""Safety properties of the scoped catalog rollout; no real cloud calls."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import reconnections_catalog_activate as a


def service():
    return {
        "name": a.NAME,
        "etag": "old-etag",
        "reconciling": False,
        "latestReadyRevision": a.OLD_REVISION,
        "latestCreatedRevision": a.OLD_REVISION,
        "uri": "https://eng-platform-api-example-uc.a.run.app",
        "labels": {"unchanged": "owner"},
        "invokerIamDisabled": False,
        "template": {
            "revision": a.OLD_REVISION,
            "serviceAccount": "existing-runtime@example.test",
            "scaling": {"minInstanceCount": 1},
            "volumes": [{"name": "private-catalog"}],
            "containers": [
                {
                    "image": a.preflight.BASE_IMAGE,
                    "env": [
                        {
                            "name": "ENG_PLATFORM_CATALOG_SHA256",
                            "value": a.preflight.BASE_SHA,
                        },
                        {
                            "name": "ENG_PLATFORM_CATALOG_PATH",
                            "value": a.preflight.BASE_PATH,
                        },
                        {"name": "PROTECTED_FLAG", "value": "unchanged"},
                        {
                            "name": "CREDENTIAL",
                            "valueSource": {
                                "secretKeyRef": {
                                    "secret": "existing-ref",
                                    "version": "1",
                                }
                            },
                        },
                    ],
                }
            ],
        },
        "trafficStatuses": [
            {"revision": a.OLD_REVISION, "percent": 100},
            {"revision": "old-other", "percent": 0, "tag": "existing-tag"},
        ],
    }


class FakeCloud:
    def __init__(self, *, fail=None):
        self.state = service()
        self.writes = []
        self.probes = []
        self.fail = fail
        self.reads = 0

    def service(self):
        self.reads += 1
        if self.fail == "race-before-create" and self.reads == 2:
            self.state["etag"] = "other-operator"
        if self.fail == "race-before-promote" and self.reads == 3:
            self.state["template"]["serviceAccount"] = "different-runtime@example.test"
        return copy.deepcopy(self.state)

    def stable_service(self):
        if self.fail == "rollback-reconciling":
            raise TimeoutError("uncertain rollback state")
        if self.fail == "rollback-concurrent":
            self.state["template"]["serviceAccount"] = "different-runtime@example.test"
        return self.service()

    def update(self, payload, fields):
        assert payload["etag"] == self.state["etag"]
        assert payload["name"] == a.NAME
        self.writes.append(copy.deepcopy(payload))
        promotion = fields == "traffic" and any(
            x["revision"] == a.REVISION and x["percent"] for x in payload["traffic"]
        )
        if self.fail == "candidate-denied" and fields == "template,traffic":
            raise a.CloudError(403)
        if promotion and self.fail == "promotion-denied":
            raise a.CloudError(403)
        if "template" in payload:
            self.state["template"] = copy.deepcopy(payload["template"])
            self.state["latestReadyRevision"] = a.REVISION
            self.state["latestCreatedRevision"] = a.REVISION
        rows = copy.deepcopy(payload["traffic"])
        for row in rows:
            if row.get("tag") == a.TAG:
                row["uri"] = (
                    "https://" + a.TAG + "---eng-platform-api-example-uc.a.run.app"
                )
        self.state["trafficStatuses"] = list(
            reversed(rows)
        )  # API ordering is immaterial.
        self.state["etag"] = "etag-" + str(len(self.writes))
        if promotion and self.fail in {
            "promotion-uncertain",
            "rollback-concurrent",
            "rollback-reconciling",
        }:
            raise TimeoutError("uncertain promotion outcome")
        if self.fail == "rollback-bad" and len(self.writes) == 3:
            self.state["trafficStatuses"][0]["percent"] = 33
        if self.fail == "wrong-tag-uri":
            for row in self.state["trafficStatuses"]:
                if row.get("tag") == a.TAG:
                    row["uri"] = "https://other-service.run.app"
        return self.service()

    def smoke(self, url):
        self.probes.append(url)
        if self.fail == "candidate-smoke" and a.TAG in url:
            raise ValueError("candidate failed")
        if self.fail in {"production-smoke", "rollback-bad"} and a.TAG not in url:
            raise ValueError("production failed")


def test_preserves_full_template_and_freezes_prior_traffic():
    old = service()
    original = copy.deepcopy(old)
    template, previous, targets = a.prepare_candidate(old)
    assert old == original
    restored = copy.deepcopy(template)
    restored["revision"] = original["template"]["revision"]
    for row in restored["containers"][0]["env"]:
        if row["name"] == "ENG_PLATFORM_CATALOG_PATH":
            row["value"] = a.preflight.BASE_PATH
        if row["name"] == "ENG_PLATFORM_CATALOG_SHA256":
            row["value"] = a.preflight.BASE_SHA
    assert restored == original["template"]
    assert sum(row["percent"] for row in targets) == 100
    assert next(row for row in targets if row["revision"] == a.REVISION)["percent"] == 0
    assert all(row["type"] == a.REVISION_TYPE for row in targets)
    assert previous == a.traffic_status(original)


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(name="other-service"),
        lambda d: d["template"]["containers"][0].update(image="other-image"),
        lambda d: d["template"]["containers"][0]["env"].append(
            {"name": "PROTECTED_FLAG", "value": "duplicate"}
        ),
        lambda d: d["template"]["containers"][0]["env"][0].update(
            value="different-source"
        ),
        lambda d: d.update(buildConfig={"sourceLocation": "forbidden-build"}),
        lambda d: d.update(etag=""),
        lambda d: d.update(reconciling=True),
        lambda d: d.update(trafficStatuses=[]),
        lambda d: d["trafficStatuses"][0].update(percent=99),
        lambda d: d["trafficStatuses"][0].update(revision="unreviewed-active"),
        lambda d: d["trafficStatuses"][1].update(tag=a.TAG),
        lambda d: d["trafficStatuses"][1].update(revision=a.REVISION),
        lambda d: d["trafficStatuses"][1].update(percent=-1),
    ],
)
def test_changed_source_or_state_blocks_before_mutation(change):
    d = service()
    change(d)
    with pytest.raises(ValueError):
        a.prepare_candidate(d)


def test_success_has_zero_traffic_smoke_before_promotion_and_uses_fresh_etag():
    c = FakeCloud()
    audit = {}
    uploads = []
    a.activate(c, lambda: uploads.append(True), audit)
    assert uploads == [True]
    assert len(c.writes) == 2
    assert c.writes[0]["traffic"][-1]["revision"] == "old-other"  # sorted, not LATEST
    assert (
        next(x for x in c.writes[0]["traffic"] if x["revision"] == a.REVISION)[
            "percent"
        ]
        == 0
    )
    assert c.writes[1]["etag"] == "etag-1"
    assert "template" not in c.writes[1]
    assert audit["candidate_zero_traffic_verified"] and audit["candidate_smoke_passed"]
    assert audit["status"] == "promoted_verified"
    assert a.TAG in c.probes[0] and a.TAG not in c.probes[1]
    assert c.state["labels"] == service()["labels"]
    assert (
        sum(
            x["percent"]
            for x in c.state["trafficStatuses"]
            if x["revision"] == a.REVISION
        )
        == 100
    )


@pytest.mark.parametrize(
    "failure,writes",
    [
        ("race-before-create", 0),
        ("candidate-denied", 1),
        ("candidate-smoke", 1),
        ("wrong-tag-uri", 1),
    ],
)
def test_no_promotion_after_failure(failure, writes):
    c = FakeCloud(fail=failure)
    audit = {}
    with pytest.raises((ValueError, a.CloudError)):
        a.activate(c, lambda: None, audit)
    assert len(c.writes) == writes
    assert not audit.get("candidate_smoke_passed")
    assert (
        sum(
            x["percent"]
            for x in c.state["trafficStatuses"]
            if x["revision"] == a.OLD_REVISION
        )
        == 100
    )


@pytest.mark.parametrize("failure", ["production-smoke", "promotion-uncertain"])
def test_failed_or_uncertain_promotion_restores_previous_reference(failure):
    c = FakeCloud(fail=failure)
    audit = {}
    with pytest.raises(RuntimeError):
        a.activate(c, lambda: None, audit)
    assert len(c.writes) == 3 and "template" not in c.writes[-1]
    assert audit["rollback_verified"]
    assert a.traffic_status(c.state) == a.traffic_status(service())


def test_denied_promotion_preserves_old_traffic_without_retrying_writer():
    c = FakeCloud(fail="promotion-denied")
    audit = {}
    with pytest.raises(RuntimeError):
        a.activate(c, lambda: None, audit)
    assert len(c.writes) == 2
    assert audit["promotion_not_applied_previous_traffic_preserved"]
    assert audit["rollback_verified"]


@pytest.mark.parametrize(
    "failure", ["rollback-concurrent", "rollback-reconciling", "rollback-bad"]
)
def test_unverifiable_rollback_never_claimed_success(failure):
    c = FakeCloud(fail=failure)
    audit = {}
    with pytest.raises((ValueError, TimeoutError)):
        a.activate(c, lambda: None, audit)
    assert not audit.get("rollback_verified")
    if failure != "rollback-bad":
        assert len(c.writes) == 2


def test_candidate_normalizes_only_api_order_and_revision_resource_name():
    old = service()
    t, _, rows = a.prepare_candidate(old)
    current = copy.deepcopy(old)
    current["template"] = copy.deepcopy(t)
    current["latestReadyRevision"] = a.REVISION
    current["latestCreatedRevision"] = a.REVISION
    current["template"]["revision"] = (
        "projects/p/locations/r/services/s/revisions/" + a.REVISION
    )
    current["template"]["containers"][0]["env"].reverse()
    current["trafficStatuses"] = list(reversed(rows))
    a.assert_candidate(current, t, rows)
    current["template"]["serviceAccount"] = "different"
    with pytest.raises(ValueError):
        a.assert_candidate(current, t, rows)
    current["template"] = t
    current["trafficStatuses"][0]["percent"] = 12
    with pytest.raises(ValueError):
        a.assert_candidate(current, t, rows)


class Response:
    def __init__(self, data, status=200):
        self.data = data
        self.status_code = status
        self.content = json.dumps(data).encode()

    def json(self):
        return self.data


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.responses.pop(0)

    def get(self, *args, **kwargs):
        return self.request(*args, **kwargs)


def test_cloud_client_masks_requests_etag_and_polls_operations(monkeypatch):
    s = Session(
        [
            Response({"name": "projects/p/locations/r/operations/op"}),
            Response({"done": True}),
            Response({"reconciling": True}),
            Response(service()),
        ]
    )
    monkeypatch.setattr(a.time, "sleep", lambda _: None)
    c = a.ExistingCloud(s)
    result = c.update({"name": a.NAME, "etag": "bound-etag"}, "traffic")
    assert result == service()
    assert s.calls[0][1]["params"] == {"updateMask": "traffic"}
    assert s.calls[0][1]["json"]["etag"] == "bound-etag"


@pytest.mark.parametrize(
    "case", ["http403", "operation-error", "operation-timeout", "reconcile-timeout"]
)
def test_cloud_client_sanitizes_failure_and_never_retries_patch(monkeypatch, case):
    monkeypatch.setattr(a.time, "sleep", lambda _: None)
    if case == "http403":
        s = Session([Response({"secret": "do not expose"}, 403)])
    elif case == "operation-error":
        s = Session(
            [Response({"done": True, "error": {"code": 7, "message": "private"}})]
        )
    elif case == "operation-timeout":
        s = Session([Response({"name": "op"})])
        ticks = iter([0, 181])
        monkeypatch.setattr(a.time, "monotonic", lambda: next(ticks))
    else:
        s = Session([Response({"done": True}), Response({"reconciling": True})])
        ticks = iter([0, 0, 121])
        monkeypatch.setattr(a.time, "monotonic", lambda: next(ticks))
    with pytest.raises((a.CloudError, TimeoutError)) as e:
        a.ExistingCloud(s).update({"etag": "x"}, "traffic")
    assert "private" not in str(e.value) and "secret" not in str(e.value)
    assert sum(call[0][0] == "PATCH" for call in s.calls) == 1


def test_smoke_sends_no_google_credentials_to_public_runtime():
    google = Session([])
    http = Session(
        [Response({"status": "ok", "mock_mode": False}), Response({"status": "ready"})]
    )
    a.ExistingCloud(google, http).smoke(service()["uri"])
    assert not google.calls
    assert len(http.calls) == 2 and all(
        call[1]["allow_redirects"] is False for call in http.calls
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://x.run.app",
        "https://example.test",
        "https://u:p@x.run.app",
        "https://x.run.app?q=x",
        "https://x.run.app#x",
    ],
)
def test_bad_smoke_destination_rejected(url):
    with pytest.raises(ValueError):
        a.ExistingCloud(Session([]), Session([])).smoke(url)


@pytest.mark.parametrize(
    "data,status",
    [
        ({"status": "ok", "mock_mode": True}, 200),
        ({"status": "broken"}, 200),
        ({"secret": "private"}, 403),
        ({"large": "x" * 33000}, 200),
    ],
)
def test_bad_smoke_fails_closed(data, status):
    with pytest.raises((ValueError, a.CloudError)):
        a.ExistingCloud(Session([]), Session([Response(data, status)])).smoke(
            service()["uri"]
        )


def test_approved_audit_checksum_and_identity(monkeypatch):
    assert a.approved_audit()["candidate_sha256"] == a.CANDIDATE_SHA
    monkeypatch.setattr(Path, "read_bytes", lambda _: b"{}")
    with pytest.raises(ValueError):
        a.approved_audit()


def test_main_preflight_change_blocks_before_cloud_client(tmp_path, monkeypatch):
    import google.auth

    monkeypatch.setenv("CGM_ACTIVATION_REPORT", str(tmp_path / "audit.json"))
    monkeypatch.setenv("CGM_PREFLIGHT_REPORT", str(tmp_path / "preflight.json"))
    monkeypatch.setattr(
        a.preflight, "main", lambda: (tmp_path / "preflight.json").write_text("{}")
    )
    monkeypatch.setattr(
        google.auth,
        "default",
        lambda **kw: pytest.fail("No cloud client before reviewed source check"),
    )
    with pytest.raises(RuntimeError):
        a.main()
    d = json.loads((tmp_path / "audit.json").read_text())
    assert d["stage"] == "verify_reviewed_preflight" and d["error_type"] == "ValueError"


@pytest.mark.parametrize(
    "case",
    [
        "created",
        "already-exists",
        "different-bytes",
        "different-proof",
        "upload-denied",
    ],
)
def test_main_binds_reviewed_generation_and_immutable_object_before_cloud_update(
    tmp_path, monkeypatch, case
):
    import google.auth
    from google.auth.transport import requests as transport
    from google.cloud import storage
    from google.api_core.exceptions import PreconditionFailed

    approved = a.approved_audit()
    monkeypatch.setenv("CGM_ACTIVATION_REPORT", str(tmp_path / "audit.json"))
    monkeypatch.setenv("CGM_PREFLIGHT_REPORT", str(tmp_path / "preflight.json"))
    monkeypatch.setattr(
        a.preflight,
        "main",
        lambda: (tmp_path / "preflight.json").write_text(json.dumps(approved)),
    )
    commands = []

    def read(*args):
        commands.append(args)
        return b"{}" if args[1] == "run" else b"exact-original-private-bytes"

    monkeypatch.setattr(a.preflight, "read_command", read)
    monkeypatch.setattr(
        a.preflight,
        "source_identity",
        lambda _: (
            "actual-mounted-bucket",
            "prefix/sha256-" + a.preflight.BASE_SHA + ".json",
        ),
    )
    proof = copy.deepcopy(approved)
    if case == "different-proof":
        proof["candidate_sha256"] = "different"
    monkeypatch.setattr(
        a.policy, "prepare", lambda raw, sha: (b"exact-approved-candidate-bytes", proof)
    )
    monkeypatch.setattr(
        google.auth, "default", lambda **kwargs: (SimpleNamespace(), None)
    )
    monkeypatch.setattr(transport, "AuthorizedSession", lambda _: SimpleNamespace())
    c = FakeCloud()
    monkeypatch.setattr(a, "ExistingCloud", lambda _: c)
    writes = []

    class Blob:
        generation = 12345

        def upload_from_string(self, data, **kw):
            writes.append((data, kw))
            if case == "already-exists":
                raise PreconditionFailed("already created")
            if case == "upload-denied":
                raise a.CloudError(403)

        def reload(self, **kw):
            assert kw["timeout"] > 0

        def download_as_bytes(self, **kw):
            assert kw["if_generation_match"] == self.generation
            return (
                b"different-private-bytes"
                if case == "different-bytes"
                else b"exact-approved-candidate-bytes"
            )

    names = []

    def bucket(name):
        assert name == "actual-mounted-bucket"

        def blob(key):
            names.append(key)
            return Blob()

        return SimpleNamespace(blob=blob)

    monkeypatch.setattr(
        storage, "Client", lambda **kwargs: SimpleNamespace(bucket=bucket)
    )
    if case in {"different-bytes", "different-proof", "upload-denied"}:
        with pytest.raises(RuntimeError):
            a.main()
        assert not c.writes
        assert json.loads((tmp_path / "audit.json").read_text())["status"] == "blocked"
    else:
        a.main()
        assert len(c.writes) == 2
        d = json.loads((tmp_path / "audit.json").read_text())
        assert (
            d["status"] == "promoted_verified"
            and d["candidate_object_generation"] == "12345"
        )
    if writes:
        assert writes[0][1]["if_generation_match"] == 0
        assert names == ["prefix/sha256-" + a.CANDIDATE_SHA + ".json"]
    assert commands[-1][-1].endswith("#" + approved["source_generation"])


def test_upload_failure_cannot_create_cloud_run_candidate():
    c = FakeCloud()
    audit = {}

    def denied():
        raise a.CloudError(403)

    with pytest.raises(a.CloudError):
        a.activate(c, denied, audit)
    assert not c.writes and audit["stage"] == "create_immutable_catalog_object"


def test_external_template_change_blocks_promotion():
    c = FakeCloud()
    audit = {}
    original_smoke = c.smoke

    def smoke(url):
        original_smoke(url)
        c.state["template"]["serviceAccount"] = "another-operator"

    c.smoke = smoke
    with pytest.raises(ValueError):
        a.activate(c, lambda: None, audit)
    assert (
        len(c.writes) == 1
        and sum(
            x["percent"]
            for x in c.state["trafficStatuses"]
            if x["revision"] == a.OLD_REVISION
        )
        == 100
    )


def test_mutation_intent_is_checkpointed_before_upload(tmp_path, monkeypatch):
    p = tmp_path / "audit.json"
    monkeypatch.setenv("CGM_ACTIVATION_REPORT", str(p))
    c = FakeCloud()
    audit = {}

    def upload():
        d = json.loads(p.read_text())
        assert d["stage"] == "create_immutable_catalog_object"
        assert d["candidate_revision"] == a.REVISION
        raise a.CloudError(403)

    with pytest.raises(a.CloudError):
        a.activate(c, upload, audit)
    assert not c.writes


@pytest.mark.parametrize("key", ["latestReadyRevision", "latestCreatedRevision"])
def test_separate_candidate_is_not_overwritten(key):
    current = service()
    current[key] = "other-operator-candidate"
    with pytest.raises(ValueError):
        a.prepare_candidate(current)


def test_candidate_must_be_the_ready_created_revision():
    current = service()
    template, _, rows = a.prepare_candidate(current)
    current["template"] = template
    current["trafficStatuses"] = rows
    with pytest.raises(ValueError):
        a.assert_candidate(current, template, rows)
