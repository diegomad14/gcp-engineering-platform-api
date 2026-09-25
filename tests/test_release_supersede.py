"""Retiring planned releases that will never be published."""

from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from eng_platform_api.main import app
from eng_platform_api.services import release_executions, release_reconciler

client = TestClient(app)

HEAD = "a" * 40
BASE = "b" * 40
REPOSITORY = "diegomad14/cgm-artemis-api"
SUPERSEDE_URL = "/api/internal/release-operations/executions/{}/supersede"


def _reserve() -> dict:
    fingerprint = release_executions.fingerprint(
        repository=REPOSITORY,
        service_name="cgm-artemis-api",
        operation="main_release",
        head_sha=HEAD,
        base_sha=BASE,
        profile_hash="profile-v1",
        executor_digest="quality@sha256:" + "c" * 64,
        policy_hash="policy-v1",
        planner_hash="planner-v1",
    )
    execution, _ = release_executions.reserve(
        fingerprint_value=fingerprint,
        repository=REPOSITORY,
        service_name="cgm-artemis-api",
        operation="main_release",
        head_sha=HEAD,
        base_sha=BASE,
        branch="main",
        profile_hash="profile-v1",
        executor_digest="quality@sha256:" + "c" * 64,
        policy_hash="policy-v1",
        planner_hash="planner-v1",
        delivery_id="delivery-1",
    )
    return execution


def _planned() -> dict:
    execution = _reserve()
    release_executions.save(execution["execution_id"], status="running_quality")
    release_executions.save(
        execution["execution_id"],
        status="quality_passed",
        evidence_committed=True,
    )
    release_executions.save(
        execution["execution_id"],
        status="release_planned",
        release_plan={"git_tag": "v1.42.0", "release_type": "minor"},
    )
    return release_executions.get(execution["execution_id"])


def test_supersede_retires_a_planned_release_without_publishing():
    execution = _planned()
    response = client.post(
        SUPERSEDE_URL.format(execution["execution_id"]),
        json={"reason": "superseded by the corrected deploy release"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "superseded"
    assert body["superseded_by"] == "diegomad14"

    stored = release_executions.get(execution["execution_id"])
    assert stored["status"] == "superseded"
    assert stored["superseded_reason"] == "superseded by the corrected deploy release"
    assert stored["release_plan"]["git_tag"] == "v1.42.0"
    assert all(
        item["execution_id"] != execution["execution_id"]
        for item in release_executions.list_due(limit=100)
    )
    assert release_reconciler.reconcile(execution["execution_id"]) == stored


def test_supersede_rejects_an_approved_canary():
    execution = _planned()
    release_executions.save(
        execution["execution_id"], canary_approved=True, canary_approved_by="diegomad14"
    )
    response = client.post(
        SUPERSEDE_URL.format(execution["execution_id"]),
        json={"reason": "already approved"},
    )
    assert response.status_code == 409
    stored = release_executions.get(execution["execution_id"])
    assert stored["status"] == "release_planned"


def _execution_in_state(state: str) -> dict:
    execution = _reserve()
    release_executions.save(execution["execution_id"], status="running_quality")
    if state != "running_quality":
        release_executions.save(execution["execution_id"], status="quality_passed")
    if state in {"released", "quality_passed"}:
        release_executions.save(execution["execution_id"], status=state)
    if state == "superseded":
        release_executions.save(execution["execution_id"], status=state)
    return release_executions.get(execution["execution_id"])


@pytest.mark.parametrize("state", ["running_quality", "released", "superseded"])
def test_supersede_rejects_states_that_are_not_pending_decisions(state):
    execution = _execution_in_state(state)
    assert execution["status"] == state
    response = client.post(
        SUPERSEDE_URL.format(execution["execution_id"]),
        json={"reason": "not a pending decision"},
    )
    assert response.status_code == 409
    assert release_executions.get(execution["execution_id"])["status"] == state


def test_supersede_requires_a_known_execution():
    response = client.post(
        SUPERSEDE_URL.format("0" * 64), json={"reason": "unknown execution"}
    )
    assert response.status_code == 404


def test_supersede_requires_a_reason():
    execution = _planned()
    response = client.post(
        SUPERSEDE_URL.format(execution["execution_id"]), json={"reason": ""}
    )
    assert response.status_code == 422
