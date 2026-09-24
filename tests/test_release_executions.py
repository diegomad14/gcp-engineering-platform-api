"""Unit coverage for durable release-execution state and idempotency."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
from unittest import mock

import pytest

from eng_platform_api.services import release_executions as executions


HEAD = "a" * 40
BASE = "b" * 40


class _Snapshot:
    def __init__(self, value=None):
        self._value = deepcopy(value)
        self.exists = value is not None

    def to_dict(self):
        return deepcopy(self._value)


class _Transaction:
    def set(self, document, value):
        document.value = deepcopy(value)

    def update(self, document, changes):
        document.value.update(deepcopy(changes))


class _Client:
    def transaction(self):
        return _Transaction()


class _Document:
    def __init__(self):
        self._client = _Client()
        self.value = None

    def get(self, transaction=None):
        return _Snapshot(self.value)

    def set(self, changes, merge=False):
        if merge and self.value is not None:
            self.value.update(deepcopy(changes))
        else:
            self.value = deepcopy(changes)


class _Query:
    def __init__(self, documents, predicates=(), limit=None):
        self.documents = documents
        self.predicates = predicates
        self.max_items = limit

    def where(self, field, operator, value):
        assert operator == "=="
        return _Query(
            self.documents,
            (*self.predicates, (field, value)),
            self.max_items,
        )

    def limit(self, value):
        return _Query(self.documents, self.predicates, value)

    def stream(self):
        values = [
            document.value
            for document in self.documents.values()
            if document.value is not None
            and all(
                document.value.get(field) == value for field, value in self.predicates
            )
        ]
        if self.max_items is not None:
            values = values[: self.max_items]
        return [_Snapshot(value) for value in values]


class _Collection(_Query):
    def __init__(self):
        self._documents = {}
        super().__init__(self._documents)

    def document(self, key):
        return self._documents.setdefault(key, _Document())


@pytest.fixture
def firestore_collection(monkeypatch):
    from google.cloud import firestore

    collection = _Collection()
    monkeypatch.setattr(firestore, "transactional", lambda function: function)
    monkeypatch.setattr(executions, "_collection", lambda: collection)
    return collection


def _fingerprint(*, operation: str = "pr_quality", **overrides: str) -> str:
    values = {
        "repository": "Owner/Repository",
        "service_name": "eng-platform-api",
        "operation": operation,
        "head_sha": HEAD,
        "base_sha": BASE,
        "profile_hash": "profile-v1",
        "executor_digest": "quality@sha256:" + "c" * 64,
        "policy_hash": "policy-v1",
        "planner_hash": "planner-v1",
    }
    values.update(overrides)
    return executions.fingerprint(**values)


def _reserve(
    *,
    operation: str = "pr_quality",
    provider: str = "github_actions",
    repository: str = "owner/repository",
    head_sha: str = HEAD,
    base_sha: str = BASE,
) -> tuple[dict, bool]:
    fingerprint = _fingerprint(
        operation=operation,
        repository=repository,
        head_sha=head_sha,
        base_sha=base_sha,
    )
    return executions.reserve(
        fingerprint_value=fingerprint,
        repository=repository,
        service_name="eng-platform-api",
        operation=operation,
        head_sha=head_sha,
        base_sha=base_sha,
        branch="main" if operation == "main_release" else "feature/test",
        profile_hash="profile-v1",
        executor_digest="quality@sha256:" + "c" * 64,
        policy_hash="policy-v1",
        planner_hash="planner-v1" if operation == "main_release" else "",
        provider=provider,
        delivery_id="delivery-1",
    )


def test_fingerprint_is_canonical_and_ignores_planner_for_pr():
    canonical = _fingerprint()
    assert len(canonical) == 64
    assert canonical == _fingerprint(
        repository="owner/repository", head_sha=HEAD.upper(), base_sha=BASE.upper()
    )
    assert canonical == _fingerprint(planner_hash="a-different-planner")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository", "owner/other"),
        ("service_name", "eng-platform-web"),
        ("head_sha", "d" * 40),
        ("base_sha", "e" * 40),
        ("profile_hash", "profile-v2"),
        ("executor_digest", "quality@sha256:" + "d" * 64),
        ("policy_hash", "policy-v2"),
    ],
)
def test_fingerprint_changes_for_every_evidence_input(field, value):
    assert _fingerprint(**{field: value}) != _fingerprint()


def test_main_release_fingerprint_binds_planner():
    assert _fingerprint(operation="main_release") != _fingerprint(
        operation="main_release", planner_hash="planner-v2"
    )


@pytest.mark.parametrize(
    ("provider", "expected_status"),
    [("github_actions", "waiting_github"), ("cloud_build", "submission_pending")],
)
def test_reserve_creates_provider_specific_initial_state(provider, expected_status):
    value, created = _reserve(provider=provider)

    assert created is True
    assert value["execution_id"] == value["fingerprint"]
    assert value["provider"] == provider
    assert value["status"] == expected_status
    assert value["head_sha"] == HEAD
    assert value["base_sha"] == BASE
    assert value["event_sequence"] == 0
    assert value["created_at"] == value["updated_at"]


def test_reserve_is_idempotent_and_returns_defensive_copies():
    original, created = _reserve()
    duplicate, duplicate_created = _reserve()

    assert created is True
    assert duplicate_created is False
    assert duplicate == original
    duplicate["status"] = "tampered"
    assert executions.get(original["execution_id"])["status"] == "waiting_github"


def test_reserve_rejects_fingerprint_not_derived_from_identity():
    with pytest.raises(ValueError, match="fingerprint does not match identity"):
        executions.reserve(
            fingerprint_value="0" * 64,
            repository="owner/repository",
            service_name="eng-platform-api",
            operation="pr_quality",
            head_sha=HEAD,
            base_sha=BASE,
            branch="feature/test",
            profile_hash="profile-v1",
            executor_digest="quality@sha256:" + "c" * 64,
            policy_hash="policy-v1",
        )


def test_reserve_rejects_changed_identity_even_when_canonical_hash_collides():
    value, _ = _reserve(repository="owner/repository")
    assert value["fingerprint"] == _fingerprint(repository="OWNER/REPOSITORY")

    with pytest.raises(ValueError, match="identity cannot change"):
        _reserve(repository="OWNER/REPOSITORY")


def test_get_unknown_execution_returns_none():
    assert executions.get("missing") is None


def test_save_updates_mutable_fields_but_not_callers_copy(monkeypatch):
    value, _ = _reserve()
    monkeypatch.setattr(executions, "_now", lambda: "2026-09-22T10:00:00+00:00")

    updated = executions.save(value["execution_id"], github_run_id=42)

    assert updated["github_run_id"] == 42
    assert updated["updated_at"] == "2026-09-22T10:00:00+00:00"
    updated["github_run_id"] = 99
    assert executions.get(value["execution_id"])["github_run_id"] == 42


@pytest.mark.parametrize(
    "field",
    [
        "fingerprint",
        "repository",
        "service_name",
        "operation",
        "head_sha",
        "base_sha",
        "profile_hash",
        "executor_digest",
        "policy_hash",
        "planner_hash",
    ],
)
def test_save_rejects_all_identity_fields(field):
    value, _ = _reserve()
    with pytest.raises(ValueError, match="identity fields are immutable"):
        executions.save(value["execution_id"], **{field: "changed"})


def test_execution_id_is_the_positional_lookup_key_not_a_mutable_change():
    value, _ = _reserve()
    with pytest.raises(TypeError, match="multiple values for argument 'execution_id'"):
        executions.save(
            value["execution_id"],
            execution_id="changed",  # type: ignore[call-arg]
        )


def test_save_unknown_execution_raises_key_error():
    with pytest.raises(KeyError, match="missing"):
        executions.save("missing", note="value")


@pytest.mark.parametrize(
    ("old_status", "new_status"),
    [
        (old_status, new_status)
        for old_status, allowed in executions._TRANSITIONS.items()
        for new_status in allowed
    ],
)
def test_every_declared_status_transition_is_accepted(old_status, new_status):
    value, _ = _reserve()
    executions._memory[value["execution_id"]]["status"] = old_status

    updated = executions.save(value["execution_id"], status=new_status)

    assert updated["status"] == new_status


@pytest.mark.parametrize(
    "terminal", ["quality_failed", "no_release", "released", "failed"]
)
def test_terminal_statuses_reject_further_transitions(terminal):
    value, _ = _reserve()
    executions._memory[value["execution_id"]]["status"] = terminal

    with pytest.raises(ValueError, match=f"{terminal}->unknown"):
        executions.save(value["execution_id"], status="unknown")


def test_saving_same_status_is_idempotently_allowed():
    value, _ = _reserve()
    assert (
        executions.save(value["execution_id"], status="waiting_github")["status"]
        == "waiting_github"
    )


def test_transition_to_cloud_build_happens_once_and_records_reason():
    value, _ = _reserve()

    transitioned, changed = executions.transition_to_cloud_build(
        value["execution_id"], reason="billing rejected"
    )
    duplicate, duplicate_changed = executions.transition_to_cloud_build(
        value["execution_id"], reason="different reason"
    )

    assert changed is True
    assert transitioned["provider"] == "cloud_build"
    assert transitioned["status"] == "submission_pending"
    assert transitioned["fallback_reason"] == "billing rejected"
    assert duplicate_changed is False
    assert duplicate["fallback_reason"] == "billing rejected"


def test_transition_to_cloud_build_rejects_missing_or_ineligible_execution():
    with pytest.raises(KeyError):
        executions.transition_to_cloud_build("missing", reason="billing")

    value, _ = _reserve()
    executions.save(value["execution_id"], status="running_quality")
    with pytest.raises(ValueError, match="no longer fallback eligible"):
        executions.transition_to_cloud_build(value["execution_id"], reason="billing")


def test_transition_to_cloud_build_rejects_unrecognised_provider():
    value, _ = _reserve()
    executions._memory[value["execution_id"]]["provider"] = "local"
    with pytest.raises(ValueError, match="provider transition is invalid"):
        executions.transition_to_cloud_build(value["execution_id"], reason="billing")


def test_claim_submission_is_granted_once_for_unbound_cloud_build():
    value, _ = _reserve(provider="cloud_build")

    assert executions.claim_submission(value["execution_id"]) is True
    assert executions.get(value["execution_id"])["status"] == "submitting"
    assert executions.claim_submission(value["execution_id"]) is False


def test_claim_submission_rejects_github_or_bound_build():
    github, _ = _reserve()
    assert executions.claim_submission(github["execution_id"]) is False

    cloud, _ = _reserve(provider="cloud_build", head_sha="c" * 40)
    executions.save(cloud["execution_id"], build_id="build-1")
    assert executions.claim_submission(cloud["execution_id"]) is False

    with pytest.raises(KeyError):
        executions.claim_submission("missing")


def test_bind_build_atomically_sets_provider_identity_and_is_idempotent():
    value, _ = _reserve(provider="cloud_build")

    bound = executions.bind_build(
        value["execution_id"],
        build_id="build-1",
        provider_status="QUEUED",
        logs_url="https://console.example/build-1",
    )
    duplicate = executions.bind_build(
        value["execution_id"],
        build_id="build-1",
        provider_status="SUCCESS",
        logs_url="https://attacker.invalid/replacement",
    )

    assert bound["build_id"] == "build-1"
    assert bound["provider_run_id"] == "build-1"
    assert bound["status"] == "submission_pending"
    assert duplicate == bound
    assert duplicate["provider_status"] == "QUEUED"
    assert duplicate["logs_url"] == "https://console.example/build-1"


def test_bind_build_rejects_conflicting_build_provider_and_reserved_metadata():
    value, _ = _reserve(provider="cloud_build")
    executions.bind_build(value["execution_id"], build_id="build-1")

    with pytest.raises(ValueError, match="already bound to another build"):
        executions.bind_build(value["execution_id"], build_id="build-2")

    github, _ = _reserve(head_sha="c" * 40)
    with pytest.raises(ValueError, match="not Cloud Build managed"):
        executions.bind_build(github["execution_id"], build_id="build-1")

    with pytest.raises(ValueError, match="reserved fields"):
        executions.bind_build(
            value["execution_id"],
            build_id="build-1",
            provider_run_id="replacement",
        )

    with pytest.raises(KeyError):
        executions.bind_build("missing", build_id="build-1")


@pytest.mark.parametrize("build_id", ["", "x" * 129])
def test_bind_build_rejects_invalid_build_id(build_id):
    value, _ = _reserve(provider="cloud_build")
    with pytest.raises(ValueError, match="Build ID is invalid"):
        executions.bind_build(value["execution_id"], build_id=build_id)


def test_bind_build_memory_race_has_one_immutable_build_winner():
    value, _ = _reserve(provider="cloud_build")
    candidates = ["build-a", "build-b"] * 16

    def bind(candidate):
        try:
            result = executions.bind_build(
                value["execution_id"],
                build_id=candidate,
                provider_status=f"QUEUED-{candidate}",
            )
            return "bound", result["build_id"]
        except ValueError:
            return "conflict", candidate

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(bind, candidates))

    stored = executions.get(value["execution_id"])
    winner = stored["build_id"]
    assert winner in {"build-a", "build-b"}
    assert stored["provider_run_id"] == winner
    assert [kind for kind, _ in results].count("bound") == 16
    assert [kind for kind, _ in results].count("conflict") == 16
    assert {candidate for kind, candidate in results if kind == "bound"} == {winner}


def test_claim_publish_requires_main_release_plan_and_is_granted_once():
    value, _ = _reserve(operation="main_release", provider="cloud_build")
    executions._memory[value["execution_id"]]["status"] = "release_planned"

    assert executions.claim_publish(value["execution_id"]) is True
    claimed = executions.get(value["execution_id"])
    assert claimed["status"] == "publish_pending"
    assert claimed["publish_claimed_at"]
    assert executions.claim_publish(value["execution_id"]) is False


def test_claim_publish_rejects_pr_wrong_status_and_missing():
    pr, _ = _reserve()
    executions._memory[pr["execution_id"]]["status"] = "release_planned"
    assert executions.claim_publish(pr["execution_id"]) is False

    main, _ = _reserve(operation="main_release", head_sha="c" * 40)
    assert executions.claim_publish(main["execution_id"]) is False
    with pytest.raises(KeyError):
        executions.claim_publish("missing")


def test_claim_source_token_is_cloud_build_only_and_one_time():
    cloud, _ = _reserve(provider="cloud_build")
    executions.bind_build(cloud["execution_id"], build_id="build-1")
    assert (
        executions.claim_source_token(cloud["execution_id"], provider_run_id="build-1")
        is True
    )
    assert (
        executions.claim_source_token(cloud["execution_id"], provider_run_id="build-1")
        is False
    )
    assert executions.get(cloud["execution_id"])["source_token_issued_at"]

    github, _ = _reserve(head_sha="c" * 40)
    assert (
        executions.claim_source_token(github["execution_id"], provider_run_id="run-1")
        is False
    )
    assert executions.claim_source_token("missing", provider_run_id="build-1") is False


def test_claim_event_token_persists_only_digest_and_is_single_use():
    value, _ = _reserve(provider="cloud_build")
    executions.bind_build(value["execution_id"], build_id="build-1")
    plaintext = "raw-event-token-that-must-not-be-stored"
    token_hash = hashlib.sha256(plaintext.encode()).hexdigest()

    assert (
        executions.claim_event_token(
            value["execution_id"], provider_run_id="build-1", token_hash=token_hash
        )
        is True
    )
    assert (
        executions.claim_event_token(
            value["execution_id"],
            provider_run_id="build-1",
            token_hash=hashlib.sha256(b"replacement").hexdigest(),
        )
        is False
    )

    stored = executions.get(value["execution_id"])
    assert stored["event_token_hash"] == token_hash
    assert stored["event_token_issued_at"]
    assert "event_token" not in stored
    assert plaintext not in stored.values()
    assert "replacement" not in stored.values()


@pytest.mark.parametrize(
    "token_hash",
    [
        "",
        "0" * 63,
        "0" * 65,
        "G" * 64,
        "A" * 64,
        "not-a-digest",
    ],
)
def test_claim_event_token_requires_lowercase_sha256(token_hash):
    value, _ = _reserve()

    with pytest.raises(ValueError, match="SHA-256 hex digest"):
        executions.claim_event_token(
            value["execution_id"], provider_run_id="run-1", token_hash=token_hash
        )

    assert "event_token_hash" not in executions.get(value["execution_id"])


@pytest.mark.parametrize("provider", ["github_actions", "cloud_build"])
def test_claim_event_token_supports_both_managed_providers(provider):
    value, _ = _reserve(provider=provider)
    provider_run_id = "build-1" if provider == "cloud_build" else "run-1"
    if provider == "cloud_build":
        executions.bind_build(value["execution_id"], build_id=provider_run_id)
    else:
        value = executions.save(value["execution_id"], provider_run_id=provider_run_id)
    token_hash = hashlib.sha256(f"{provider}-token".encode()).hexdigest()

    assert (
        executions.claim_event_token(
            value["execution_id"],
            provider_run_id=provider_run_id,
            token_hash=token_hash,
        )
        is True
    )
    assert executions.get(value["execution_id"])["event_token_hash"] == token_hash


def test_claim_event_token_missing_execution_is_not_claimed():
    assert (
        executions.claim_event_token(
            "missing",
            provider_run_id="build-1",
            token_hash=hashlib.sha256(b"token").hexdigest(),
        )
        is False
    )


def test_claim_event_token_memory_concurrency_has_exactly_one_winner():
    value, _ = _reserve(provider="cloud_build")
    executions.bind_build(value["execution_id"], build_id="build-1")
    hashes = [
        hashlib.sha256(f"token-{index}".encode()).hexdigest() for index in range(32)
    ]

    def claim(token_hash):
        return executions.claim_event_token(
            value["execution_id"], provider_run_id="build-1", token_hash=token_hash
        )

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(claim, hashes))

    assert results.count(True) == 1
    assert results.count(False) == 31
    winning_hash = hashes[results.index(True)]
    stored = executions.get(value["execution_id"])
    assert stored["event_token_hash"] == winning_hash
    assert not any(f"token-{index}" in stored.values() for index in range(32))


def test_cloud_build_retry_can_claim_fresh_tokens_only_for_new_bound_attempt():
    value, _ = _reserve(operation="main_release", provider="cloud_build")
    executions.bind_build(value["execution_id"], build_id="build-1")
    assert executions.claim_source_token(
        value["execution_id"], provider_run_id="build-1"
    )
    first_hash = hashlib.sha256(b"event-one").hexdigest()
    assert executions.claim_event_token(
        value["execution_id"], provider_run_id="build-1", token_hash=first_hash
    )
    executions._memory[value["execution_id"]].update(
        {
            "status": "failed",
            "engine_event_status": "quality_passed",
            "release_engine_failed": True,
            "evidence_committed": True,
            "report_hash": "a" * 64,
        }
    )

    executions.stage_planner_retry(
        value["execution_id"],
        failed_build_id="build-1",
        planner_image="registry.example/release-planner@sha256:" + "a" * 64,
    )
    executions.bind_build(value["execution_id"], build_id="build-2")

    assert executions.claim_source_token(
        value["execution_id"], provider_run_id="build-2"
    )
    assert not executions.claim_source_token(
        value["execution_id"], provider_run_id="build-2"
    )
    second_hash = hashlib.sha256(b"event-two").hexdigest()
    assert executions.claim_event_token(
        value["execution_id"], provider_run_id="build-2", token_hash=second_hash
    )
    assert not executions.claim_event_token(
        value["execution_id"], provider_run_id="build-2", token_hash=first_hash
    )
    stored = executions.get(value["execution_id"])
    assert stored["event_token_hash"] == second_hash
    assert stored["event_token_build_id"] == "build-2"
    assert stored["source_token_build_id"] == "build-2"


def test_accept_event_requires_strictly_increasing_sequence():
    value, _ = _reserve(provider="cloud_build")

    accepted, changed = executions.accept_event(
        value["execution_id"], 2, status="running_quality", provider_run_id="build-1"
    )
    duplicate, duplicate_changed = executions.accept_event(
        value["execution_id"], 2, status="quality_passed", provider_run_id="build-2"
    )
    stale, stale_changed = executions.accept_event(
        value["execution_id"], 1, status="quality_failed"
    )

    assert changed is True
    assert accepted["event_sequence"] == 2
    assert accepted["provider_run_id"] == "build-1"
    assert duplicate_changed is False
    assert stale_changed is False
    assert duplicate == stale == accepted


def test_accept_event_validates_transition_and_missing_execution():
    value, _ = _reserve(provider="cloud_build")
    with pytest.raises(ValueError, match="submission_pending->released"):
        executions.accept_event(value["execution_id"], 1, status="released")
    assert executions.get(value["execution_id"])["event_sequence"] == 0

    with pytest.raises(KeyError):
        executions.accept_event("missing", 1, status="failed")


@pytest.mark.parametrize("uncertain_status", ["submitting", "unknown"])
def test_reconcile_submission_absent_allows_bounded_retry(uncertain_status):
    value, _ = _reserve(provider="cloud_build")
    executions._memory[value["execution_id"]].update(
        {"status": uncertain_status, "reconciliation_attempts": 2}
    )

    updated = executions.reconcile_submission_absent(value["execution_id"])

    assert updated["status"] == "submission_pending"
    assert updated["reconciliation_attempts"] == 3


def test_reconcile_submission_absent_rejects_known_or_bound_submission():
    value, _ = _reserve(provider="cloud_build")
    with pytest.raises(ValueError, match="not uncertain"):
        executions.reconcile_submission_absent(value["execution_id"])

    executions._memory[value["execution_id"]].update(
        {"status": "unknown", "build_id": "build-1"}
    )
    with pytest.raises(ValueError, match="not uncertain"):
        executions.reconcile_submission_absent(value["execution_id"])

    with pytest.raises(KeyError):
        executions.reconcile_submission_absent("missing")


def _failed_planner_execution(value: dict) -> None:
    executions._memory[value["execution_id"]].update(
        {
            "status": "failed",
            "provider": "cloud_build",
            "operation": "main_release",
            "build_id": "planner-build-1",
            "provider_run_id": "planner-build-1",
            "engine_event_status": "quality_passed",
            "release_engine_failed": True,
            "evidence_committed": True,
            "report_hash": "a" * 64,
        }
    )


def test_planner_retry_is_a_single_exact_evidence_recovery():
    value, _ = _reserve(operation="main_release", provider="cloud_build")
    _failed_planner_execution(value)

    assert executions.planner_retry_candidate(executions.get(value["execution_id"]))
    staged = executions.stage_planner_retry(
        value["execution_id"],
        failed_build_id="planner-build-1",
        planner_image="registry.example/release-planner@sha256:" + "a" * 64,
    )

    assert staged["status"] == "submission_pending"
    assert staged["build_id"] == ""
    assert staged["provider_run_id"] == ""
    assert staged["planner_retry_pending"] is True
    assert staged["planner_retry_count"] == 1
    assert staged["previous_build_ids"] == ["planner-build-1"]
    assert staged["evidence_committed"] is True
    assert not executions.planner_retry_candidate(staged)


def test_planner_retry_rejects_missing_evidence_or_second_attempt():
    value, _ = _reserve(operation="main_release", provider="cloud_build")
    _failed_planner_execution(value)
    executions._memory[value["execution_id"]]["evidence_committed"] = False
    assert not executions.planner_retry_candidate(executions.get(value["execution_id"]))


def test_planner_retry_allows_one_image_change_remediation_only():
    value, _ = _reserve(operation="main_release", provider="cloud_build")
    _failed_planner_execution(value)
    old_image = "registry.example/release-planner@sha256:" + "a" * 64
    first = executions.stage_planner_retry(
        value["execution_id"],
        failed_build_id="planner-build-1",
        planner_image=old_image,
    )
    executions._memory[value["execution_id"]].update(
        {
            "status": "failed",
            "build_id": "planner-build-2",
            "provider_run_id": "planner-build-2",
            "provider_status": "FAILURE",
            "planner_retry_pending": False,
            "release_engine_failed": True,
            "error": "planner still failed",
        }
    )

    state = executions.get(value["execution_id"])
    assert not executions.planner_retry_candidate(state)
    assert executions.planner_retry_candidate(state, allow_remediation=True)
    new_image = "registry.example/release-planner@sha256:" + "b" * 64
    staged = executions.stage_planner_retry(
        value["execution_id"],
        failed_build_id="planner-build-2",
        planner_image=new_image,
        planner_hash_value="c" * 64,
        remediation=True,
        previous_planner_image=old_image,
    )

    assert first["planner_retry_count"] == 1
    assert staged["planner_retry_count"] == 2
    assert staged["planner_remediation_retry_pending"] is True
    assert staged["planner_remediation_retry_checked"] is True
    assert staged["planner_remediation_retry_image"] == new_image
    assert staged["planner_retry_image"] == old_image
    assert staged["previous_build_ids"] == ["planner-build-1", "planner-build-2"]
    assert not executions.planner_retry_candidate(staged, allow_remediation=True)
    with pytest.raises(ValueError, match="not eligible"):
        executions.stage_planner_retry(
            value["execution_id"], failed_build_id="planner-build-1"
        )

    executions._memory[value["execution_id"]]["evidence_committed"] = True
    executions._memory[value["execution_id"]]["planner_retry_count"] = 1
    assert not executions.planner_retry_candidate(executions.get(value["execution_id"]))


def test_planner_contract_retry_allows_one_same_image_recovery_after_remediation():
    value, _ = _reserve(operation="main_release", provider="cloud_build")
    _failed_planner_execution(value)
    old_image = "registry.example/release-planner@sha256:" + "a" * 64
    new_image = "registry.example/release-planner@sha256:" + "b" * 64
    new_hash = "c" * 64
    first = executions.stage_planner_retry(
        value["execution_id"],
        failed_build_id="planner-build-1",
        planner_image=old_image,
    )
    executions._memory[value["execution_id"]].update(
        {
            "status": "failed",
            "build_id": "planner-build-2",
            "provider_run_id": "planner-build-2",
            "provider_status": "FAILURE",
            "planner_retry_count": 2,
            "planner_retry_pending": False,
            "planner_retry_checked": True,
            "planner_remediation_retry_checked": True,
            "planner_remediation_retry_pending": True,
            "planner_remediation_retry_image": new_image,
            "planner_remediation_retry_hash": new_hash,
            "release_engine_failed": True,
            "error": "planner hash/image contract mismatch",
        }
    )

    state = executions.get(value["execution_id"])
    assert executions.planner_retry_candidate(state, allow_contract_retry=True)
    staged = executions.stage_planner_retry(
        value["execution_id"],
        failed_build_id="planner-build-2",
        planner_image=new_image,
        planner_hash_value=new_hash,
        contract_retry=True,
    )

    assert first["planner_retry_count"] == 1
    assert staged["status"] == "submission_pending"
    assert staged["planner_retry_count"] == 3
    assert staged["planner_contract_retry_checked"] is True
    assert staged["planner_contract_retry_pending"] is True
    assert staged["planner_contract_retry_image"] == new_image
    assert staged["planner_contract_retry_hash"] == new_hash
    assert staged["planner_retry_image"] == old_image
    assert staged["previous_build_ids"] == ["planner-build-1", "planner-build-2"]
    assert not executions.planner_retry_candidate(staged, allow_contract_retry=True)
    with pytest.raises(ValueError, match="not eligible"):
        executions.stage_planner_retry(
            value["execution_id"],
            failed_build_id="planner-build-2",
            planner_image=new_image,
            planner_hash_value=new_hash,
            contract_retry=True,
        )


def test_planner_retry_eligible_execution_remains_due_for_reconciliation():
    value, _ = _reserve(operation="main_release", provider="cloud_build")
    _failed_planner_execution(value)

    assert [item["execution_id"] for item in executions.list_due()] == [
        value["execution_id"]
    ]


def test_list_and_find_filter_normalize_sha_and_order_by_creation():
    first, _ = _reserve(head_sha="c" * 40)
    second, _ = _reserve(operation="main_release", head_sha="d" * 40)
    other, _ = _reserve(repository="owner/other", head_sha="e" * 40)
    executions._memory[first["execution_id"]]["created_at"] = "2026-01-01"
    executions._memory[second["execution_id"]]["created_at"] = "2026-02-01"
    executions._memory[other["execution_id"]]["created_at"] = "2026-03-01"

    listed = executions.list_for_repository("owner/repository", limit=1)

    assert [item["execution_id"] for item in listed] == [second["execution_id"]]
    assert (
        executions.find("owner/repository", ("D" * 40), "main_release")["execution_id"]
        == second["execution_id"]
    )
    assert executions.find("owner/repository", "f" * 40, "main_release") is None


def test_list_due_excludes_terminal_states_and_orders_oldest_update_first():
    due_old, _ = _reserve(head_sha="c" * 40)
    terminal, _ = _reserve(head_sha="d" * 40)
    due_new, _ = _reserve(head_sha="e" * 40)
    executions._memory[due_old["execution_id"]]["updated_at"] = "2026-01-01"
    executions._memory[terminal["execution_id"]].update(
        {"updated_at": "2025-01-01", "status": "released"}
    )
    executions._memory[due_new["execution_id"]]["updated_at"] = "2026-02-01"

    listed = executions.list_due(limit=1)

    assert [item["execution_id"] for item in listed] == [due_old["execution_id"]]
    snapshot = deepcopy(listed[0])
    listed[0]["status"] = "tampered"
    assert executions.get(snapshot["execution_id"])["status"] == snapshot["status"]


def test_collection_uses_configured_firestore_project_and_name(monkeypatch):
    from google.cloud import firestore

    client = mock.Mock()
    sentinel = object()
    client.return_value.collection.return_value = sentinel
    monkeypatch.setattr(firestore, "Client", client)
    monkeypatch.setattr(executions.config, "mock_mode", False)
    monkeypatch.setattr(executions.config.release_orchestrator, "enabled", True)
    monkeypatch.setattr(
        executions.config.release_orchestrator,
        "execution_collection",
        "release-executions-test",
    )
    monkeypatch.setattr(executions.config.cloud_build, "project_id", "build-project")

    assert executions._collection() is sentinel
    client.assert_called_once_with(project="build-project")
    client.return_value.collection.assert_called_once_with("release-executions-test")


@pytest.mark.parametrize(
    ("mock_mode", "enabled", "collection_name"),
    [(True, True, "items"), (False, False, "items"), (False, True, "")],
)
def test_collection_is_disabled_without_explicit_production_configuration(
    monkeypatch, mock_mode, enabled, collection_name
):
    monkeypatch.setattr(executions.config, "mock_mode", mock_mode)
    monkeypatch.setattr(executions.config.release_orchestrator, "enabled", enabled)
    monkeypatch.setattr(
        executions.config.release_orchestrator,
        "execution_collection",
        collection_name,
    )
    assert executions._collection() is None


def test_firestore_reserve_get_save_and_duplicate_identity(firestore_collection):
    value, created = _reserve()
    duplicate, duplicate_created = _reserve()

    assert created is True
    assert duplicate_created is False
    assert executions.get(value["execution_id"]) == duplicate
    assert executions.get("missing") is None
    assert (
        executions.save(value["execution_id"], github_run_id=123)["github_run_id"]
        == 123
    )
    with pytest.raises(KeyError):
        executions.save("missing", note="nope")
    with pytest.raises(ValueError, match="identity cannot change"):
        _reserve(repository="OWNER/REPOSITORY")


def test_firestore_transition_to_cloud_build_is_atomic(firestore_collection):
    value, _ = _reserve()
    transitioned, changed = executions.transition_to_cloud_build(
        value["execution_id"], reason="billing"
    )

    assert changed is True
    assert transitioned["provider"] == "cloud_build"
    assert (
        executions.transition_to_cloud_build(
            value["execution_id"], reason="replacement"
        )[1]
        is False
    )
    with pytest.raises(KeyError):
        executions.transition_to_cloud_build("missing", reason="billing")

    other, _ = _reserve(head_sha="c" * 40)
    executions.save(other["execution_id"], status="running_quality")
    with pytest.raises(ValueError, match="no longer fallback eligible"):
        executions.transition_to_cloud_build(other["execution_id"], reason="billing")

    invalid, _ = _reserve(head_sha="d" * 40)
    firestore_collection.document(invalid["execution_id"]).value["provider"] = "local"
    with pytest.raises(ValueError, match="provider transition is invalid"):
        executions.transition_to_cloud_build(invalid["execution_id"], reason="billing")


def test_firestore_bind_build_is_atomic_idempotent_and_conflict_safe(
    firestore_collection,
):
    value, _ = _reserve(provider="cloud_build")

    first = executions.bind_build(
        value["execution_id"],
        build_id="build-1",
        provider_status="QUEUED",
        logs_url="https://console.example/build-1",
    )
    duplicate = executions.bind_build(
        value["execution_id"],
        build_id="build-1",
        provider_status="SUCCESS",
        logs_url="https://attacker.invalid/replacement",
    )

    assert duplicate == first
    assert duplicate["provider_status"] == "QUEUED"
    assert duplicate["logs_url"] == "https://console.example/build-1"
    assert firestore_collection.document(value["execution_id"]).value["build_id"] == (
        "build-1"
    )
    with pytest.raises(ValueError, match="already bound to another build"):
        executions.bind_build(value["execution_id"], build_id="build-2")


def test_firestore_submission_publish_and_token_claims_are_single_use(
    firestore_collection,
):
    cloud, _ = _reserve(provider="cloud_build")
    assert executions.claim_submission(cloud["execution_id"]) is True
    assert executions.claim_submission(cloud["execution_id"]) is False
    executions.bind_build(cloud["execution_id"], build_id="build-1")
    assert (
        executions.claim_source_token(cloud["execution_id"], provider_run_id="build-1")
        is True
    )
    assert (
        executions.claim_source_token(cloud["execution_id"], provider_run_id="build-1")
        is False
    )
    plaintext = "firestore-event-token"
    token_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    assert (
        executions.claim_event_token(
            cloud["execution_id"], provider_run_id="build-1", token_hash=token_hash
        )
        is True
    )
    assert (
        executions.claim_event_token(
            cloud["execution_id"],
            provider_run_id="build-1",
            token_hash=hashlib.sha256(b"replacement").hexdigest(),
        )
        is False
    )
    stored = firestore_collection.document(cloud["execution_id"]).value
    assert stored["event_token_hash"] == token_hash
    assert stored["event_token_issued_at"]
    assert "event_token" not in stored
    assert plaintext not in stored.values()

    main, _ = _reserve(
        operation="main_release", provider="cloud_build", head_sha="c" * 40
    )
    firestore_collection.document(main["execution_id"]).value["status"] = (
        "release_planned"
    )
    assert executions.claim_publish(main["execution_id"]) is True
    assert executions.claim_publish(main["execution_id"]) is False

    with pytest.raises(KeyError):
        executions.claim_submission("missing")
    with pytest.raises(KeyError):
        executions.claim_publish("missing")
    assert executions.claim_source_token("missing", provider_run_id="build-1") is False
    assert (
        executions.claim_event_token(
            "missing",
            provider_run_id="build-1",
            token_hash=hashlib.sha256(b"token").hexdigest(),
        )
        is False
    )


def test_firestore_claims_reject_wrong_provider_operation_and_bound_build(
    firestore_collection,
):
    github, _ = _reserve()
    assert executions.claim_submission(github["execution_id"]) is False
    assert (
        executions.claim_source_token(github["execution_id"], provider_run_id="run-1")
        is False
    )

    pr, _ = _reserve(provider="cloud_build", head_sha="c" * 40)
    firestore_collection.document(pr["execution_id"]).value["status"] = (
        "release_planned"
    )
    assert executions.claim_publish(pr["execution_id"]) is False

    bound, _ = _reserve(provider="cloud_build", head_sha="d" * 40)
    executions.save(bound["execution_id"], build_id="build-1")
    assert executions.claim_submission(bound["execution_id"]) is False


def test_firestore_events_are_monotonic_and_validate_transitions(firestore_collection):
    value, _ = _reserve(provider="cloud_build")

    updated, accepted = executions.accept_event(
        value["execution_id"], 3, status="running_quality"
    )
    duplicate, duplicate_accepted = executions.accept_event(
        value["execution_id"], 3, status="quality_failed"
    )

    assert accepted is True
    assert duplicate_accepted is False
    assert duplicate == updated
    with pytest.raises(ValueError, match="running_quality->released"):
        executions.accept_event(value["execution_id"], 4, status="released")
    with pytest.raises(KeyError):
        executions.accept_event("missing", 1, status="failed")


def test_firestore_reconcile_lists_and_finds_executions(firestore_collection):
    due, _ = _reserve(provider="cloud_build")
    executions.claim_submission(due["execution_id"])
    retried = executions.reconcile_submission_absent(due["execution_id"])
    assert retried["status"] == "submission_pending"
    assert retried["reconciliation_attempts"] == 1

    terminal, _ = _reserve(head_sha="c" * 40)
    executions.save(terminal["execution_id"], status="failed")
    other, _ = _reserve(repository="owner/other", head_sha="d" * 40)

    listed = executions.list_for_repository("owner/repository")
    assert {item["execution_id"] for item in listed} == {
        due["execution_id"],
        terminal["execution_id"],
    }
    assert (
        executions.find("owner/repository", HEAD.upper(), "pr_quality")["execution_id"]
        == due["execution_id"]
    )
    assert [item["execution_id"] for item in executions.list_due()] == [
        due["execution_id"],
        other["execution_id"],
    ]


def test_firestore_planner_retry_is_transactional_and_one_shot(firestore_collection):
    value, _ = _reserve(operation="main_release", provider="cloud_build")
    firestore_collection.document(value["execution_id"]).value.update(
        {
            "status": "failed",
            "build_id": "planner-build-1",
            "provider_run_id": "planner-build-1",
            "engine_event_status": "quality_passed",
            "release_engine_failed": True,
            "evidence_committed": True,
            "report_hash": "a" * 64,
        }
    )

    staged = executions.stage_planner_retry(
        value["execution_id"],
        failed_build_id="planner-build-1",
        planner_image="registry.example/release-planner@sha256:" + "a" * 64,
    )

    assert staged["status"] == "submission_pending"
    assert staged["planner_retry_pending"] is True
    assert staged["planner_retry_count"] == 1
    assert staged["previous_build_ids"] == ["planner-build-1"]
    with pytest.raises(ValueError, match="not eligible"):
        executions.stage_planner_retry(
            value["execution_id"],
            failed_build_id="planner-build-1",
            planner_image="registry.example/release-planner@sha256:" + "a" * 64,
        )
