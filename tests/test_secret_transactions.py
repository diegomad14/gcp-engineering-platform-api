"""Exercise manifest transactions without publishing real secret values."""

from copy import deepcopy
from types import SimpleNamespace
from unittest import mock

import pytest

from eng_platform_api.models import CatalogService, OperationalSecret
from eng_platform_api.services import operational_secrets as subject


class Ref:
    def __init__(self, store, path=""):
        self.store, self.path = store, path

    def collection(self, key):
        return Ref(self.store, f"{self.path}/{key}")

    def document(self, key):
        return Ref(self.store, f"{self.path}/{key}")

    def get(self, transaction=None):
        value = deepcopy(self.store.get(self.path))
        return SimpleNamespace(to_dict=lambda: value)


class Transaction:
    def set(self, ref, value, merge=False):
        previous = ref.store.get(ref.path, {}) if merge else {}
        ref.store[ref.path] = {**previous, **deepcopy(value)}


@pytest.fixture
def db(monkeypatch):
    store = {}
    root = Ref(store)
    root.transaction = Transaction
    monkeypatch.setattr(subject.firestore, "transactional", lambda fn: fn)
    return root


@pytest.fixture
def selected():
    return CatalogService(
        service_name="service",
        repository="owner/repo",
        owner="owner",
        project_id="project",
        region="region",
        operational_secrets=[
            OperationalSecret(key="WM_PASSWORD", secret_id="wm-password", editable=True)
        ],
    )


def test_reservation_blocks_double_click_and_concurrent_writer(db, selected):
    secret = selected.operational_secrets[0]
    assert subject.reserve(db, selected, secret, "operation", 0, "angel") is None
    for operation in ("operation", "other-operation"):
        with pytest.raises(subject.ConfigurationConflict):
            subject.reserve(db, selected, secret, operation, 0, "angel")
    record = subject.finalize(db, "service", "WM_PASSWORD", "operation", "3")
    assert record["status"] == "SAVED"
    state = subject.document(db, "service").get().to_dict()
    assert state["generation"] == 1
    assert state["versions"] == {"WM_PASSWORD": "3"}
    assert state.get("applied_versions", {}) == {}
    assert state["active_operation"] is None
    manifest = (
        subject.document(db, "service")
        .collection("manifests")
        .document("1")
        .get()
        .to_dict()
    )
    assert manifest == {"generation": 1, "versions": {"WM_PASSWORD": "3"}}
    assert subject.reserve(db, selected, secret, "operation", 0, "angel") == record
    with pytest.raises(subject.ConfigurationConflict):
        subject.reserve(db, selected, secret, "new-operation", 0, "angel")


def test_operation_cannot_be_reused_by_another_operator_or_key(db, selected):
    secret = selected.operational_secrets[0]
    subject.reserve(db, selected, secret, "operation", 0, "angel")
    subject.finalize(db, "service", secret.key, "operation", "1")
    with pytest.raises(subject.ConfigurationConflict):
        subject.reserve(db, selected, secret, "operation", 0, "other")
    other_secret = secret.model_copy(update={"key": "OTHER"})
    with pytest.raises(subject.ConfigurationConflict):
        subject.reserve(
            db,
            selected,
            other_secret,
            "operation",
            0,
            "angel",
        )


def test_finalize_rejects_wrong_operation(db):
    with pytest.raises(subject.ConfigurationConflict):
        subject.finalize(db, "service", "WM_PASSWORD", "unknown", "1")


def test_snapshot_rejects_required_disabled_version(selected):
    with (
        mock.patch.object(
            subject,
            "state",
            return_value={"generation": 1, "versions": {"WM_PASSWORD": "1"}},
        ),
        mock.patch.object(
            subject,
            "metadata",
            return_value={
                "generation": 1,
                "items": [{"required": True, "configured": False}],
            },
        ),
    ):
        with pytest.raises(subject.ConfigurationConflict):
            subject.snapshot(selected)


@pytest.mark.parametrize("state", [{"active_operation": "pending"}, {"generation": 2}])
def test_snapshot_rejects_unresolved_write_or_changed_generation(selected, state):
    with (
        mock.patch.object(subject, "state", return_value=state),
        mock.patch.object(
            subject, "metadata", return_value={"generation": 1, "items": []}
        ),
    ):
        with pytest.raises(subject.ConfigurationConflict):
            subject.snapshot(selected)


def test_snapshot_only_contains_numeric_pinned_references(selected):
    with (
        mock.patch.object(
            subject,
            "state",
            return_value={"generation": 1, "versions": {"WM_PASSWORD": "2"}},
        ),
        mock.patch.object(
            subject,
            "metadata",
            return_value={
                "generation": 1,
                "items": [{"required": True, "configured": True}],
            },
        ),
    ):
        assert subject.snapshot(selected) == {
            "generation": 1,
            "secrets": {
                "WM_PASSWORD": "projects/project/secrets/wm-password/versions/2"
            },
        }


def test_metadata_reads_only_version_metadata(selected):
    client = mock.Mock()
    client.get_secret_version.return_value.state = (
        subject.secretmanager.SecretVersion.State.ENABLED
    )
    with (
        mock.patch.object(
            subject,
            "state",
            return_value={
                "generation": 3,
                "versions": {"WM_PASSWORD": "2"},
                "applied_versions": {"WM_PASSWORD": "1"},
            },
        ),
        mock.patch.object(subject, "writer", return_value=client),
    ):
        result = subject.metadata(selected)
    assert result["items"][0]["configured"] is True
    assert result["items"][0]["applied_version"] == "1"
    client.access_secret_version.assert_not_called()


def test_publish_success_finalizes_numeric_version(selected):
    client = mock.Mock()
    client.add_secret_version.return_value.name = (
        "projects/project/secrets/wm-password/versions/2"
    )
    record = {
        "operation_id": "operation",
        "status": "SAVED",
        "version": "2",
        "generation": 1,
    }
    with (
        mock.patch.object(subject, "writer", return_value=client),
        mock.patch.object(subject, "database") as database,
        mock.patch.object(subject, "reserve", return_value=None),
        mock.patch.object(subject, "finalize", return_value=record) as finalize,
    ):
        assert (
            subject.publish(
                selected,
                selected.operational_secrets[0],
                "private",
                "operation",
                0,
                "angel",
            )
            == record
        )
    finalize.assert_called_once_with(
        database.return_value, "service", "WM_PASSWORD", "operation", "2"
    )
