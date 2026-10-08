"""The official workflow changes only reviewed pins and fixed private routing."""

import copy
import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from eng_platform_api import prepare_quality_tooling as subject
from eng_platform_api import verify_candidate_config as candidate

OLD = subject.SERVICE + "-old"
OTHER = subject.SERVICE + "-other"


def service(new_pins=False, new_routing=False):
    images = candidate._tooling_images()
    if not new_pins:
        images = {
            name: value.split("@sha256:")[0] + "@sha256:" + "a" * 64
            for name, value in images.items()
        }
    return {
        "metadata": {
            "name": subject.SERVICE,
            "namespace": "546821492326",
            "generation": 7,
            "labels": {"owner": "platform"},
            "annotations": {
                "run.googleapis.com/ingress": "internal-and-cloud-load-balancing"
            },
        },
        "spec": {
            "traffic": [
                {"latestRevision": True, "percent": 70},
                {"revisionName": OTHER, "percent": 30},
            ],
            "template": {
                "metadata": {
                    "name": OLD,
                    "labels": {"commit-sha": "old"},
                    "annotations": {
                        "run.googleapis.com/vpc-access-connector": "connector"
                    },
                },
                "spec": {
                    "serviceAccountName": "runtime@example.com",
                    "containerConcurrency": 80,
                    "timeoutSeconds": 300,
                    "volumes": [
                        {"name": "secret", "secret": {"secretName": "reference-only"}}
                    ],
                    "containers": [
                        {
                            "image": "image@sha256:" + "b" * 64,
                            "resources": {"limits": {"cpu": "1", "memory": "1Gi"}},
                            "startupProbe": {"tcpSocket": {"port": 8080}},
                            "env": [
                                {
                                    "name": candidate.WRITER_ENV,
                                    "value": candidate.EXPECTED_WRITER,
                                },
                                {
                                    "name": "EXISTING_SECRET",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": "reference-only",
                                            "key": "1",
                                        }
                                    },
                                },
                                *[
                                    {"name": name, "value": value}
                                    for name, value in images.items()
                                ],
                                {
                                    "name": candidate.CLOUD_BUILD_ONLY_ENV,
                                    "value": ",".join(
                                        candidate.CLOUD_BUILD_ONLY_SERVICES
                                        if new_routing
                                        else candidate.BASELINE_CLOUD_BUILD_ONLY_SERVICES
                                    ),
                                },
                            ],
                        }
                    ],
                },
            },
        },
        "status": {
            "observedGeneration": 7,
            "conditions": [{"type": "Ready", "status": "True"}],
            "latestReadyRevisionName": OLD,
            "traffic": [
                {"latestRevision": True, "percent": 70},
                {"revisionName": OTHER, "percent": 30, "tag": "stable"},
            ],
        },
    }


def staged():
    data = service(new_pins=True, new_routing=True)
    data["metadata"]["generation"] = 8
    data["status"]["observedGeneration"] = 8
    data["status"]["latestReadyRevisionName"] = subject.SERVICE + "-staging"
    data["status"]["traffic"][0] = {"revisionName": OLD, "percent": 70}
    data["spec"]["traffic"][0] = {"revisionName": OLD, "percent": 70}
    data["spec"]["template"]["metadata"]["name"] = subject.SERVICE + "-staging"
    data["spec"]["template"]["metadata"]["annotations"][
        "run.googleapis.com/client-version"
    ] = "new"
    return data


def run_preparation(after):
    with mock.patch.object(
        subject, "_cloud", side_effect=[service(), {}, after]
    ) as cloud:
        result = subject.prepare()
    return result, cloud


def test_updates_both_pins_and_fixed_routing_once_without_other_mutation_flags():
    passed, cloud = run_preparation(staged())
    assert passed
    assert cloud.call_args_list[0] == mock.call(["describe"])
    arguments = cloud.call_args_list[1].args[0]
    assert arguments[:3] == ["update", "--no-traffic", "--quiet"]
    assert len(arguments) == 4
    values = {
        **candidate._tooling_images(),
        candidate.CLOUD_BUILD_ONLY_ENV: ",".join(candidate.CLOUD_BUILD_ONLY_SERVICES),
    }
    assert arguments[3] == "--update-env-vars=^|^" + "|".join(
        f"{name}={value}" for name, value in values.items()
    )
    # Gcloud's alternate delimiter parses three fields, preserving all commas.
    encoded = arguments[3].removeprefix("--update-env-vars=")
    _, delimiter, assignments = encoded.split("^", 2)
    assert dict(item.split("=", 1) for item in assignments.split(delimiter)) == values
    assert cloud.call_args_list[1].kwargs == {"timeout": 600}
    assert cloud.call_args_list[2] == mock.call(["describe"])


def test_already_coordinated_pins_and_routing_do_not_create_another_revision():
    with mock.patch.object(
        subject, "_cloud", return_value=service(new_pins=True, new_routing=True)
    ) as cloud:
        assert subject.prepare()
    cloud.assert_called_once_with(["describe"])


def routing_row(data):
    return next(
        item
        for item in data["spec"]["template"]["spec"]["containers"][0]["env"]
        if item["name"] == candidate.CLOUD_BUILD_ONLY_ENV
    )


@pytest.mark.parametrize("new_pins", [False, True])
@pytest.mark.parametrize("new_routing", [False, True])
def test_preserves_every_other_field_across_known_transitions(new_pins, new_routing):
    before = service(new_pins=new_pins, new_routing=new_routing)
    after = staged()
    original_before = copy.deepcopy(before)
    original_after = copy.deepcopy(after)
    responses = [before] if new_pins and new_routing else [before, {}, after]
    with mock.patch.object(subject, "_cloud", side_effect=responses) as cloud:
        assert subject.prepare()
    assert cloud.call_count == len(responses)
    assert before == original_before
    assert after == original_after
    assert subject._configuration(before) == subject._configuration(after)
    assert subject._traffic(before) == subject._traffic(after)


@pytest.mark.parametrize("new_pins", [False, True])
def test_already_complete_reordered_routing_is_preserved(new_pins):
    before = service(new_pins=new_pins, new_routing=True)
    routing_row(before)["value"] = ",".join(
        reversed(candidate.CLOUD_BUILD_ONLY_SERVICES)
    )
    after = staged()
    routing_row(after)["value"] = routing_row(before)["value"]
    responses = [before] if new_pins else [before, {}, after]
    with mock.patch.object(subject, "_cloud", side_effect=responses) as cloud:
        assert subject.prepare()
    assert cloud.call_count == len(responses)


def test_reordered_known_baseline_preserves_all_thirteen_services():
    before = service()
    routing_row(before)["value"] = ",".join(
        reversed(candidate.BASELINE_CLOUD_BUILD_ONLY_SERVICES)
    )
    with mock.patch.object(subject, "_cloud", side_effect=[before, {}, staged()]):
        assert subject.prepare()


def test_routing_update_cannot_be_overridden_by_process_environment(monkeypatch):
    monkeypatch.setenv(candidate.CLOUD_BUILD_ONLY_ENV, "arbitrary-service")
    assert run_preparation(staged())[0]


def test_updated_pins_with_unchanged_thirteen_service_routing_block_engine():
    after = staged()
    routing_row(after)["value"] = ",".join(candidate.BASELINE_CLOUD_BUILD_ONLY_SERVICES)
    assert not run_preparation(after)[0]


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "duplicate_field",
        "empty",
        "null",
        "nonstring",
        "reference",
        "value_and_reference",
        "extra_field",
        "missing_service",
        "extra_service",
        "duplicate_service",
        "replaced_service",
        "whitespace",
        "trailing_comma",
        "delimiter",
        "partial_transition",
    ],
)
def test_invalid_routing_fails_closed_before_mutation_or_promotion(phase, kind, capsys):
    data = service(new_pins=True) if phase == "before" else staged()
    env = data["spec"]["template"]["spec"]["containers"][0]["env"]
    row = routing_row(data)
    if kind == "missing":
        env.remove(row)
    elif kind == "duplicate_field":
        env.append(copy.deepcopy(row))
    elif kind == "empty":
        row["value"] = ""
    elif kind == "null":
        row["value"] = None
    elif kind == "nonstring":
        row["value"] = list(candidate.CLOUD_BUILD_ONLY_SERVICES)
    elif kind in {"reference", "value_and_reference"}:
        if kind == "reference":
            row.pop("value")
        row["valueFrom"] = {"secretKeyRef": {"name": "PRIVATE", "key": "latest"}}
    elif kind == "extra_field":
        row["extra"] = "PRIVATE"
    elif kind == "missing_service":
        row["value"] = row["value"].split(",", 1)[1]
    elif kind == "extra_service":
        row["value"] += ",unknown-service"
    elif kind == "duplicate_service":
        row["value"] += ",cgm-artemis-api"
    elif kind == "replaced_service":
        row["value"] = row["value"].replace("cgm-artemis-api", "unknown-service")
    elif kind == "whitespace":
        row["value"] += " "
    elif kind == "trailing_comma":
        row["value"] += ","
    elif kind == "delimiter":
        row["value"] += "|ARBITRARY=PRIVATE"
    else:
        row["value"] = ",".join(
            (*candidate.BASELINE_CLOUD_BUILD_ONLY_SERVICES, "cgm-bot-api")
        )
    responses = [data] if phase == "before" else [service(), {}, data]
    with mock.patch.object(subject, "_cloud", side_effect=responses) as cloud:
        assert not subject.prepare()
    assert cloud.call_count == len(responses)
    assert not capsys.readouterr().out + capsys.readouterr().err


@pytest.mark.parametrize("transition", ["added", "changed", "removed"])
def test_gcloud_generated_template_nonce_does_not_count_as_operational_drift(
    transition,
):
    before = service()
    after = staged()
    before_labels = before["spec"]["template"]["metadata"]["labels"]
    after_labels = after["spec"]["template"]["metadata"]["labels"]
    if transition != "added":
        before_labels["client.knative.dev/nonce"] = "synthetic-old"
    if transition != "removed":
        after_labels["client.knative.dev/nonce"] = "synthetic-new"
    with mock.patch.object(subject, "_cloud", side_effect=[before, {}, after]) as cloud:
        assert subject.prepare()
    assert cloud.call_count == 3
    assert ("client.knative.dev/nonce" in before_labels) == (transition != "added")
    assert ("client.knative.dev/nonce" in after_labels) == (transition != "removed")


@pytest.mark.parametrize(
    "location", ["service_nonce", "template_owner", "nonce_lookalike"]
)
def test_nonce_exception_never_ignores_other_labels(location):
    after = staged()
    if location == "service_nonce":
        after["metadata"]["labels"]["client.knative.dev/nonce"] = "changed"
    else:
        label = (
            "owner"
            if location == "template_owner"
            else "client.knative.dev/nonce-extra"
        )
        after["spec"]["template"]["metadata"]["labels"][label] = "changed"
    assert not run_preparation(after)[0]


@pytest.mark.parametrize("before_empty_labels", [None, {}])
def test_nonce_only_added_label_preserves_absent_or_empty_business_labels(
    before_empty_labels,
):
    before = service()
    after = staged()
    metadata = before["spec"]["template"]["metadata"]
    if before_empty_labels is None:
        metadata.pop("labels")
    else:
        metadata["labels"] = before_empty_labels
    after["spec"]["template"]["metadata"]["labels"] = {
        "client.knative.dev/nonce": "synthetic-new"
    }
    with mock.patch.object(subject, "_cloud", side_effect=[before, {}, after]):
        assert subject.prepare()


@pytest.mark.parametrize(
    "field",
    [
        "image",
        "resources",
        "startupProbe",
        "service_account",
        "timeout",
        "concurrency",
        "scaling",
        "volumes",
        "env",
        "writer",
        "extra_env",
        "removed_env",
        "ingress",
        "vpc",
        "labels",
        "traffic",
        "tag",
        "partial_pins",
        "duplicate_pin",
        "secret_pin",
    ],
)
def test_any_operational_drift_or_partial_update_blocks_engine(field):
    after = staged()
    spec = after["spec"]["template"]["spec"]
    container = spec["containers"][0]
    if field in {"image", "resources", "startupProbe"}:
        container[field] = "changed"
    elif field in {"service_account", "timeout", "concurrency", "volumes"}:
        spec[
            {
                "service_account": "serviceAccountName",
                "timeout": "timeoutSeconds",
                "concurrency": "containerConcurrency",
                "volumes": "volumes",
            }[field]
        ] = "changed"
    elif field == "scaling":
        after["spec"]["template"]["metadata"]["annotations"][
            "autoscaling.knative.dev/maxScale"
        ] = "1"
    elif field == "env":
        container["env"][1]["valueFrom"]["secretKeyRef"]["key"] = "changed"
    elif field == "writer":
        container["env"][0]["value"] = "changed"
    elif field == "extra_env":
        container["env"].append({"name": "ARBITRARY_ALLOWLIST", "value": "changed"})
    elif field == "removed_env":
        container["env"].pop(1)
    elif field == "ingress":
        after["metadata"]["annotations"]["run.googleapis.com/ingress"] = "all"
    elif field == "vpc":
        after["spec"]["template"]["metadata"]["annotations"][
            "run.googleapis.com/vpc-access-connector"
        ] = "changed"
    elif field == "labels":
        after["metadata"]["labels"] = {"changed": "yes"}
    elif field == "traffic":
        after["status"]["traffic"][0]["percent"] = 69
        after["status"]["traffic"][1]["percent"] = 31
    elif field == "tag":
        after["status"]["traffic"][1]["tag"] = "changed"
    elif field == "partial_pins":
        container["env"][2]["value"] = "old"
    elif field == "duplicate_pin":
        container["env"].append(copy.deepcopy(container["env"][2]))
    else:
        row = container["env"][2]
        row.pop("value")
        row["valueFrom"] = {"secretKeyRef": {"name": "reference-only", "key": "1"}}
    assert not run_preparation(after)[0]


@pytest.mark.parametrize(
    "field",
    [
        "wrong_service",
        "multiple_containers",
        "bad_env",
        "nonlist_env",
        "duplicate_env",
        "writer",
        "unready",
        "generation",
        "missing_traffic",
        "bad_percent",
        "incomplete_percent",
        "duplicate_tag",
        "malformed_revision",
    ],
)
def test_invalid_initial_configuration_never_updates_cloud(field):
    before = service()
    spec = before["spec"]["template"]["spec"]
    if field == "wrong_service":
        before["metadata"]["name"] = "other"
    elif field == "multiple_containers":
        spec["containers"].append(copy.deepcopy(spec["containers"][0]))
    elif field == "bad_env":
        spec["containers"][0]["env"] = [None]
    elif field == "nonlist_env":
        spec["containers"][0]["env"] = {}
    elif field == "duplicate_env":
        spec["containers"][0]["env"].append(
            copy.deepcopy(spec["containers"][0]["env"][1])
        )
    elif field == "writer":
        spec["containers"][0]["env"][0]["value"] = "wrong"
    elif field == "unready":
        before["status"]["conditions"][0]["status"] = "False"
    elif field == "generation":
        before["status"]["observedGeneration"] = 6
    elif field == "missing_traffic":
        before["status"]["traffic"] = []
    elif field == "bad_percent":
        before["status"]["traffic"][0]["percent"] = True
    elif field == "incomplete_percent":
        before["status"]["traffic"][0]["percent"] = 69
    elif field == "duplicate_tag":
        before["status"]["traffic"][0]["tag"] = "stable"
    else:
        before["status"]["traffic"][1]["revisionName"] = "other-service-revision"
    with mock.patch.object(subject, "_cloud", return_value=before) as cloud:
        assert not subject.prepare()
    cloud.assert_called_once_with(["describe"])


def test_cloud_arguments_are_fixed_and_provider_output_is_captured():
    data = service()
    with mock.patch.object(subject.subprocess, "run") as run:
        run.return_value.stdout = json.dumps(data)
        assert subject._cloud(["describe"]) == data
    assert run.call_args.args[0] == [
        "gcloud",
        "run",
        "services",
        "describe",
        subject.SERVICE,
        "--project=" + subject.PROJECT,
        "--region=" + subject.REGION,
        "--format=json",
    ]
    assert run.call_args.kwargs == {
        "capture_output": True,
        "text": True,
        "check": True,
        "timeout": 60,
    }


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.CalledProcessError(1, "gcloud", stderr="PRIVATE"),
        subprocess.TimeoutExpired("gcloud", 60, output="PRIVATE"),
        FileNotFoundError("PRIVATE"),
        ValueError("PRIVATE"),
    ],
)
def test_cloud_errors_are_redacted_and_never_retried(failure, capsys):
    with mock.patch.object(subject, "_cloud", side_effect=failure) as cloud:
        assert not subject.prepare()
    assert cloud.call_count == 1
    assert not capsys.readouterr().out + capsys.readouterr().err


def test_invalid_bundle_never_queries_cloud():
    with (
        mock.patch.object(
            candidate, "_tooling_images", side_effect=ValueError("invalid")
        ),
        mock.patch.object(subject, "_cloud") as cloud,
    ):
        assert not subject.prepare()
    cloud.assert_not_called()


@pytest.mark.parametrize("phase", ["update", "verification"])
def test_timeout_after_starting_update_fails_without_retry_or_reversal(phase, capsys):
    timeout = subprocess.TimeoutExpired("gcloud", 60, output="PRIVATE")
    responses = [service(), timeout] if phase == "update" else [service(), {}, timeout]
    with mock.patch.object(subject, "_cloud", side_effect=responses) as cloud:
        assert subject.main() == 1
    assert cloud.call_count == len(responses)
    assert [call.args[0][0] for call in cloud.call_args_list] == (
        ["describe", "update"]
        if phase == "update"
        else ["describe", "update", "describe"]
    )
    assert capsys.readouterr().out == "Coordinated quality tooling preparation: FAIL\n"


@pytest.mark.parametrize("raw", ['{"spec":{},"spec":{}}', "[]", "private-not-json"])
def test_malformed_cloud_json_fails_closed(raw):
    with mock.patch.object(subject.subprocess, "run") as run:
        run.return_value.stdout = raw
        assert not subject.prepare()
    assert run.call_count == 1


@pytest.mark.parametrize("passed", [True, False])
def test_cli_only_prints_fixed_result(passed, capsys):
    with mock.patch.object(subject, "prepare", return_value=passed):
        assert subject.main() == (0 if passed else 1)
    assert (
        capsys.readouterr().out
        == "Coordinated quality tooling preparation: "
        + ("PASS" if passed else "FAIL")
        + "\n"
    )


def test_workflow_only_prepares_after_existing_authorization_and_quality_gates():
    workflow = (
        Path(__file__).parents[1] / ".github/workflows/platform-deploy.yml"
    ).read_text()
    preparation = workflow.index(
        "PYTHONPATH=src python3 -m eng_platform_api.prepare_quality_tooling"
    )
    for earlier in [
        "Verify Engineering Platform authorization",
        "Require exact OSS quality evidence",
        "google-github-actions/auth@",
        "Mark deployment in progress",
    ]:
        assert workflow.index(earlier) < preparation
    # The engine digest is validated before any cloud authentication and passed
    # through an environment variable instead of shell expression interpolation.
    assert preparation < workflow.index('"$RELEASE_EXECUTOR_IMAGE" --service')
    assert 'if [ "$CGM_SERVICE" = eng-platform-api ]; then' in workflow
    for name, value in {
        "CGM_REPOSITORY": "diegomad14/gcp-engineering-platform-api",
        "CGM_PROJECT_ID": subject.PROJECT,
        "CGM_REGION": subject.REGION,
        "CGM_PROFILE_SHA256": "b29b8c450169374047dcddbb019eb27264ceeabf41272306199eb0d70632dbf0",
    }.items():
        assert f'test "${name}" = {value}' in workflow


def test_failed_preparation_exits_workflow_before_engine_or_cloud_auth(tmp_path):
    import yaml

    workflow = yaml.safe_load(
        (
            Path(__file__).parents[1] / ".github/workflows/platform-deploy.yml"
        ).read_text()
    )
    step = next(
        item
        for item in workflow["jobs"]["release"]["steps"]
        if item.get("name") == "Execute the pinned central engine"
    )
    command = step["run"].replace(
        "${{ vars.ENG_PLATFORM_RELEASE_EXECUTOR_IMAGE }}", "unused-digest"
    )
    for name in ("python3", "gcloud", "docker"):
        executable = tmp_path / name
        if name == "python3":
            executable.write_text(
                "#!/bin/sh\nprintf 'Coordinated quality tooling preparation: FAIL\\n'\nexit 1\n"
            )
        else:
            executable.write_text(
                f"#!/bin/sh\nprintf 'unexpected' > '{tmp_path / 'engine-called'}'\nexit 1\n"
            )
        executable.chmod(0o700)
    result = subprocess.run(
        ["/bin/bash", "-c", command],
        env={
            "PATH": str(tmp_path),
            "CGM_SERVICE": subject.SERVICE,
            "CGM_REPOSITORY": "diegomad14/gcp-engineering-platform-api",
            "CGM_PROJECT_ID": subject.PROJECT,
            "CGM_REGION": subject.REGION,
            "CGM_PROFILE_SHA256": "b29b8c450169374047dcddbb019eb27264ceeabf41272306199eb0d70632dbf0",
        },
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 1
    assert result.stdout == "Coordinated quality tooling preparation: FAIL\n"
    assert result.stderr == ""
    assert not (tmp_path / "engine-called").exists()
