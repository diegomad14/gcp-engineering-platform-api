"""Every Factory release proposal has an explicit, kind-safe JSON Schema."""

from copy import deepcopy
import json
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError
import pytest
import yaml

from eng_platform_api.models import ServiceFactoryRequest
from eng_platform_api.services.service_factory import generate_plan


SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "schemas/gcp-service-release.schema.json"
)
SCHEMA = json.loads(SCHEMA_PATH.read_text())
VALIDATOR = Draft202012Validator(SCHEMA)


def contract(kind="cloud_run_service", runtime="python"):
    request = ServiceFactoryRequest(
        repository="example/test-repo",
        service_name="example-resource",
        service_type="worker",
        runtime=runtime,
        runtime_kind=kind,
        gcp_project="test-project",
        owner="platform",
        validation_targets=["external-source", ""],
    )
    return yaml.safe_load(generate_plan(request).yaml_contract)


def test_release_schema_is_valid_draft_2020_12():
    Draft202012Validator.check_schema(SCHEMA)


@pytest.mark.parametrize("kind", ["cloud_run_service", "cloud_run_job"])
@pytest.mark.parametrize("runtime", ["python", "node", "static"])
def test_factory_release_proposals_validate_for_both_kinds(kind, runtime):
    value = contract(kind, runtime)
    VALIDATOR.validate(value)
    assert value["release_target"]["platform"] == "gcp-cloud-run"
    assert value["release_target"]["runtime_kind"] == kind
    assert value["release_target"]["service_name"] == value["service"]["name"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("url", "https://example.invalid"),
        ("health_path", "/health"),
        ("openapi_path", "/openapi.json"),
        ("expected_openapi_paths", 0),
    ],
)
def test_job_proposal_schema_forbids_http_release_fields(field, value):
    data = contract("cloud_run_job")
    data["release_target"][field] = value
    with pytest.raises(ValidationError):
        VALIDATOR.validate(data)


@pytest.mark.parametrize(
    "change",
    [
        {"enabled": True},
        {"enabled": "false"},
        {"enabled": 0},
        {"executor": "auto"},
        {"executor": "github_actions"},
        {"review_required": ""},
        {"workflow_file": "platform-deploy.yml"},
        {"approved": True},
    ],
)
def test_job_proposal_cannot_enable_deployment_or_add_service_workflows(change):
    data = contract("cloud_run_job")
    data["deployment"].update(change)
    with pytest.raises(ValidationError):
        VALIDATOR.validate(data)


@pytest.mark.parametrize(
    "field", ["deployment", "enabled", "executor", "review_required"]
)
def test_job_proposal_requires_the_complete_disabled_deployment_guard(field):
    data = contract("cloud_run_job")
    if field == "deployment":
        del data[field]
    else:
        del data["deployment"][field]
    with pytest.raises(ValidationError):
        VALIDATOR.validate(data)


def test_job_proposal_cannot_add_even_empty_http_validation_targets():
    data = contract("cloud_run_job")
    data["validation_targets"] = {"smoke_endpoints": []}
    with pytest.raises(ValidationError):
        VALIDATOR.validate(data)


@pytest.mark.parametrize("kind", ["cloud_run_service", "cloud_run_job"])
def test_release_proposal_requires_explicit_supported_kind(kind):
    original = contract(kind)
    for value in [None, "job", "cloud_run_revision", ""]:
        data = deepcopy(original)
        if value is None:
            del data["release_target"]["runtime_kind"]
        else:
            data["release_target"]["runtime_kind"] = value
        with pytest.raises(ValidationError):
            VALIDATOR.validate(data)


def test_service_proposal_cannot_smuggle_a_job_deployment_guard():
    data = contract()
    data["deployment"] = contract("cloud_run_job")["deployment"]
    with pytest.raises(ValidationError):
        VALIDATOR.validate(data)


@pytest.mark.parametrize(
    "scope", [None, "release_target", "service", "quality", "security", "finops"]
)
def test_release_proposals_still_reject_unknown_fields(scope):
    data = contract("cloud_run_job")
    (data if scope is None else data[scope])["unknown"] = True
    with pytest.raises(ValidationError):
        VALIDATOR.validate(data)
