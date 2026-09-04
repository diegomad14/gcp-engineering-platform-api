"""Trusted engine entrypoint. Application sources are used only as Docker context.

Never print source URLs, authorization tokens, environment values or provider bodies.
Secret references are resolved to numeric versions before any runtime is changed.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def oidc() -> str:
    query = urlencode({"audience": "engineering-platform-release"})
    url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"] + "&" + query
    request = Request(
        url,
        headers={
            "Authorization": "Bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]
        },
    )
    with urlopen(request, timeout=30) as response:
        token = json.load(response)["value"]
    print("::add-mask::" + token, flush=True)
    return token


def api(path: str, body=None):
    root = os.environ["PLATFORM_API_URL"].rstrip("/")
    if not root.startswith("https://"):
        raise RuntimeError("HTTPS platform API required")
    request = Request(
        root + "/api/internal/central-releases/" + path,
        headers={"X-GitHub-OIDC": oidc(), "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None,
    )
    return urlopen(request, timeout=120)


def output(key: str, value: str):
    if "\n" in value or "\r" in value:
        raise RuntimeError("Invalid workflow output")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        stream.write(f"{key}={value}\n")


def prepare():
    token = os.environ["PLATFORM_AUTHORIZATION"]
    print("::add-mask::" + token, flush=True)
    with api("consume", {"token": token}) as response:
        context = json.load(response)
    execution_id = context["execution_id"]
    output("execution_id", execution_id)
    output("kind", context["plan"]["kind"])
    if context["plan"]["kind"] == "deploy":
        with (
            api(f"{execution_id}/source") as response,
            open("release-source.tar.gz", "wb") as destination,
        ):
            shutil.copyfileobj(response, destination)


def plan():
    with api(os.environ["EXECUTION_ID"] + "/context") as response:
        return json.load(response)["plan"]


def command(args, *, structured=False, cwd=None):
    # Capture provider output, which may include configuration. Return only parsed data.
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"Release command failed: {args[0]}")
    return json.loads(result.stdout) if structured else result.stdout.strip()


def cloud(args, selected, *, structured=True):
    return command(
        [
            "gcloud",
            *args,
            "--project=" + selected["project_id"],
            "--quiet",
            "--format=json" if structured else "--format=value(name)",
        ],
        structured=structured,
    )


def build(selected):
    registry = f"{selected['region']}-docker.pkg.dev"
    image = f"{registry}/{selected['project_id']}/{selected['artifact_repository']}/{selected['image_name']}:{selected['sha']}"
    # Extraction is outside the engine checkout and outside WIF credential files.
    with tempfile.TemporaryDirectory(prefix="release-source-") as folder:
        with tarfile.open("release-source.tar.gz") as archive:
            archive.extractall(folder, filter="data")
        roots = list(Path(folder).iterdir())
        if len(roots) != 1 or not roots[0].is_dir():
            raise RuntimeError("Invalid source archive layout")
        source = (roots[0] / selected["build_context"]).resolve()
        if not source.is_relative_to(roots[0].resolve()):
            raise RuntimeError("Build context escapes source archive")
        command(["gcloud", "auth", "configure-docker", registry, "--quiet"])
        command(
            [
                "docker",
                "build",
                "--build-arg",
                "APP_VERSION=" + selected["tag"],
                "--tag",
                image,
                ".",
            ],
            cwd=source,
        )
        command(["docker", "push", image])
        image_digest = command(
            ["docker", "inspect", image, "--format={{index .RepoDigests 0}}"]
        )
        if not re.fullmatch(
            re.escape(image.rsplit(":", 1)[0]) + r"@sha256:[0-9a-f]{64}", image_digest
        ):
            raise RuntimeError("Invalid built image digest")
        output("image_digest", image_digest)


def numeric_secret(selected, secret_name, version):
    info = cloud(
        ["secrets", "versions", "describe", str(version), "--secret=" + secret_name],
        selected,
    )
    number = info["name"].rsplit("/", 1)[-1]
    if not number.isdecimal() or info["state"] != "ENABLED":
        raise RuntimeError("Required secret version is not enabled")
    return secret_name + ":" + number


def describe(selected, kind, name):
    return cloud(
        ["run", kind, "describe", name, "--region=" + selected["region"]], selected
    )


def snapshot_runtime(selected, kind, name):
    resource = describe(selected, kind, name)
    if kind == "services":
        active = [
            item for item in resource["status"]["traffic"] if item.get("percent", 0)
        ]
        if len(active) != 1 or active[0]["percent"] != 100:
            raise RuntimeError(
                "Release requires one production revision at 100 percent"
            )
        revision = describe(selected, "revisions", active[0]["revisionName"])
        containers = revision["spec"]["containers"]
        image = revision["status"].get("imageDigest", containers[0]["image"])
        traffic = {active[0]["revisionName"]: 100}
    else:
        containers = resource["spec"]["template"]["spec"]["template"]["spec"][
            "containers"
        ]
        image = containers[0]["image"]
        traffic = {}
    if "@sha256:" not in image:
        artifact = cloud(["artifacts", "docker", "images", "describe", image], selected)
        image = artifact["image_summary"]["fully_qualified_digest"]
    if not re.fullmatch(r"[a-z0-9.-]+/[A-Za-z0-9_./-]+@sha256:[0-9a-f]{64}", image):
        raise RuntimeError("Runtime image is not immutable")
    if len(containers) != 1:
        raise RuntimeError(
            "Multi-container runtime requires an explicit release profile"
        )
    refs = {}
    for variable in containers[0].get("env", []):
        reference = variable.get("valueFrom", {}).get("secretKeyRef")
        if reference:
            refs[variable["name"]] = numeric_secret(
                selected, reference["name"], reference["key"]
            )
    return {"image": image, "secrets": refs, "traffic": traffic}


def capture(selected):
    names = {
        "services": [selected["service_name"], *selected["auxiliary_services"]],
        "jobs": selected["auxiliary_jobs"],
    }
    return {
        kind: {name: snapshot_runtime(selected, kind, name) for name in values}
        for kind, values in names.items()
    }


def update_runtime(selected, kind, name, image, references, *, suffix=""):
    args = [
        "run",
        kind,
        "update",
        name,
        "--region=" + selected["region"],
        "--image=" + image,
    ]
    if references:
        args.append(
            "--set-secrets="
            + ",".join(f"{key}={value}" for key, value in sorted(references.items()))
        )
    else:
        args.append("--clear-secrets")
    if suffix:
        args.extend(["--no-traffic", "--revision-suffix=" + suffix])
    cloud(args, selected)


def traffic(selected, service, revisions):
    cloud(
        [
            "run",
            "services",
            "update-traffic",
            service,
            "--region=" + selected["region"],
            "--to-revisions="
            + ",".join(
                f"{revision}={percent}" for revision, percent in revisions.items()
            ),
        ],
        selected,
    )


def smoke(url, selected):
    paths = [selected["health_path"]]
    if selected["service_name"] == "cgm-sanplat-api":
        paths.append("/wm/health")
    for path in paths:
        for attempt in range(6):
            try:
                with urlopen(url.rstrip("/") + path, timeout=35) as response:
                    if path == "/wm/health":
                        health = json.load(response)
                        if not all(
                            health.get(key) is True
                            for key in ("configured", "wm_reachable", "perseo_db")
                        ):
                            raise RuntimeError("WM or Perseo candidate check failed")
                break
            except Exception:
                if attempt == 5:
                    raise RuntimeError("Release smoke failed") from None
                time.sleep(5)


def candidate_url(selected, revision):
    tag = "central-" + os.environ["GITHUB_RUN_ID"]
    cloud(
        [
            "run",
            "services",
            "update-traffic",
            selected["service_name"],
            "--region=" + selected["region"],
            "--update-tags=" + tag + "=" + revision,
        ],
        selected,
    )
    service = describe(selected, "services", selected["service_name"])
    return next(
        item["url"] for item in service["status"]["traffic"] if item.get("tag") == tag
    )


def restore(selected, previous):
    # Restore worker images/references before API traffic. Scheduler definitions never change.
    for kind in ("jobs", "services"):
        for name, runtime in sorted(
            previous[kind].items(), key=lambda pair: pair[0] == selected["service_name"]
        ):
            if kind == "services":
                suffix = (
                    "rb"
                    + os.environ["GITHUB_RUN_ID"]
                    + "-"
                    + os.environ["GITHUB_RUN_ATTEMPT"]
                    + "-"
                    + runtime["image"][-8:]
                )
                update_runtime(
                    selected,
                    kind,
                    name,
                    runtime["image"],
                    runtime["secrets"],
                    suffix=suffix,
                )
                traffic(selected, name, {name + "-" + suffix: 100})
            else:
                update_runtime(
                    selected, kind, name, runtime["image"], runtime["secrets"]
                )
    return (
        selected["service_name"]
        + "-rb"
        + os.environ["GITHUB_RUN_ID"]
        + "-"
        + os.environ["GITHUB_RUN_ATTEMPT"]
        + "-"
        + previous["services"][selected["service_name"]]["image"][-8:]
    )


def release(selected):
    result = {"status": "FAILED"}
    mutated = False
    try:
        previous = capture(selected)
        with api(os.environ["EXECUTION_ID"] + "/checkpoint", previous):
            pass
        if selected["kind"] == "rollback":
            # Revalidate every target secret before modifying a runtime.
            for runtimes in selected["target_runtimes"].values():
                for runtime in runtimes.values():
                    for reference in runtime["secrets"].values():
                        secret, version = reference.rsplit(":", 1)
                        numeric_secret(selected, secret, version)
            mutated = True
            revision = restore(selected, selected["target_runtimes"])
            image = selected["target_digest"]
        else:
            image = os.environ["IMAGE_DIGEST"]
            prefix = f"{selected['region']}-docker.pkg.dev/{selected['project_id']}/{selected['artifact_repository']}/{selected['image_name']}"
            if not re.fullmatch(re.escape(prefix) + r"@sha256:[0-9a-f]{64}", image):
                raise RuntimeError("Untrusted image digest")
            suffix = (
                "r"
                + os.environ["GITHUB_RUN_ID"]
                + "-"
                + os.environ["GITHUB_RUN_ATTEMPT"]
            )
            revision = selected["service_name"] + "-" + suffix
            overrides = {}
            for key, reference in selected["configuration"]["secrets"].items():
                parts = reference.split("/")
                overrides[key] = numeric_secret(selected, parts[-3], parts[-1])
            primary = previous["services"][selected["service_name"]]
            update_runtime(
                selected,
                "services",
                selected["service_name"],
                image,
                primary["secrets"] | overrides,
                suffix=suffix,
            )
            url = candidate_url(selected, revision)
            smoke(url, selected)
            result["candidate_revision"] = revision
            # Nothing affecting production is changed until candidate smoke succeeds.
            mutated = True
            for kind in ("jobs", "services"):
                for name, runtime in previous[kind].items():
                    if name != selected["service_name"]:
                        auxiliary_suffix = suffix if kind == "services" else ""
                        update_runtime(
                            selected,
                            kind,
                            name,
                            image,
                            runtime["secrets"] | overrides,
                            suffix=auxiliary_suffix,
                        )
                        if kind == "services":
                            traffic(selected, name, {name + "-" + suffix: 100})
            traffic(selected, selected["service_name"], {revision: 100})
        smoke(
            describe(selected, "services", selected["service_name"])["status"]["url"],
            selected,
        )
        result.update(
            status="SUCCEEDED",
            image_digest=image,
            production_revision=revision,
            runtime_snapshot=capture(selected),
        )
    except Exception:
        if mutated:
            try:
                restore(selected, previous)
                result["status"] = "ROLLED_BACK"
            except Exception:
                result["status"] = "ROLLBACK_FAILED"
    # Reporting is idempotent; never roll back a healthy deployment just because its ACK was lost.
    for attempt in range(5):
        try:
            with api(os.environ["EXECUTION_ID"] + "/result", result):
                break
        except Exception:
            if attempt == 4:
                raise RuntimeError("Release result requires reconciliation") from None
            time.sleep(3)
    if result["status"] != "SUCCEEDED":
        raise RuntimeError("Release failed; consult the sanitized platform result")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["prepare", "build", "release"])
    args = parser.parse_args()
    try:
        if args.operation == "prepare":
            prepare()
        elif args.operation == "build":
            build(plan())
        else:
            release(plan())
    except Exception:
        raise SystemExit(
            "Central release operation failed; no provider payloads logged"
        ) from None


if __name__ == "__main__":
    main()
