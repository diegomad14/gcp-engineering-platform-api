#!/usr/bin/env python3
"""Smoke the built Python image under the private quality runtime restrictions.

Only the host downloads an official, hash-pinned wheel. The Docker probe has no
network and does not run scanners, audit tools, repository scripts or telemetry.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path


WHEEL = (
    "psycopg2_binary-2.9.11-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.whl"
)
WHEEL_URL = (
    "https://files.pythonhosted.org/packages/30/da/"
    "4e42788fb811bbbfd7b7f045570c062f49e350e1d1f3df056c3fb5763353/" + WHEEL
)
WHEEL_SHA256 = "fa0f693d3c68ae925966f0b14b8edda71696608039f4ed61b1fe9ffa468d16db"
SCRATCH = Path("/eng-platform-scratch")
QUALITY_SCRATCH = SCRATCH / "quality"
OUTPUT = Path("/eng-platform-output")
UID = GID = 65532
CAPABILITIES = (1 << 0) | (1 << 5) | (1 << 6) | (1 << 7)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def status() -> dict[str, str]:
    return dict(
        line.split(":", 1)
        for line in Path("/proc/self/status").read_text().splitlines()
        if ":" in line
    )


def check_tmpfs(path: Path, size_mib: int, *, noexec: bool) -> None:
    entries = [
        line.split()
        for line in Path("/proc/self/mountinfo").read_text().splitlines()
        if line.split()[4] == str(path)
    ]
    require(len(entries) == 1, f"{path} must have exactly one explicit mount")
    entry = entries[0]
    separator = entry.index("-")
    options = set(entry[5].split(","))
    require(entry[separator + 1] == "tmpfs", f"{path} must be tmpfs")
    require({"rw", "nosuid", "nodev"} <= options, f"Unsafe {path} flags")
    require(("noexec" in options) == noexec, f"Wrong {path} exec policy")
    filesystem = os.statvfs(path)
    require(
        filesystem.f_blocks * filesystem.f_frsize == size_mib * 1024 * 1024,
        f"Unexpected {path} capacity",
    )


def child(command: list[str], environment: dict[str, str]) -> None:
    subprocess.run(
        command,
        env=environment,
        user=UID,
        group=GID,
        extra_groups=[],
        check=True,
        timeout=60,
    )


def probe(args: argparse.Namespace) -> None:
    sys.path.insert(0, "/opt/eng-platform")
    import quality_executor
    import trusted_scanner

    require(os.geteuid() == 0, "Trusted quality supervisor must start as root")
    require(sys.version_info[:2] == (3, 12), "Fixture requires CPython 3.12")
    require(
        not Path("/run/secrets/build-ca-bundle").exists()
        and not Path("/tmp/quality-build-apt.conf").exists(),
        "Build-only CA material persisted in the image",
    )
    for name in ("CapEff", "CapPrm", "CapBnd"):
        require(int(status()[name], 16) == CAPABILITIES, f"Unexpected {name}")
    require(status()["NoNewPrivs"].strip() == "1", "no-new-privileges is missing")
    check_tmpfs(Path("/tmp"), 64, noexec=True)
    check_tmpfs(SCRATCH, 6080, noexec=False)
    require(os.stat("/tmp").st_dev != SCRATCH.stat().st_dev, "Scratch is not separate")
    require(64 + 6080 == 6 * 1024, "Quality tmpfs differs from the 6 GiB budget")
    try:
        Path("/root-read-only-smoke").write_text("must fail")
    except OSError as error:
        require(error.errno == errno.EROFS, "Root filesystem failure is not EROFS")
    else:
        raise RuntimeError("Root filesystem is writable")

    # Output contains only trusted evidence, never the checkout or its runtime.
    os.chown(OUTPUT, 0, 0)
    os.chmod(OUTPUT, 0o700)
    try:
        identity = {"head_sha": args.head_sha, "base_sha": args.base_sha}
        require(not QUALITY_SCRATCH.exists(), "Nested quality scratch must start fresh")
        parent = quality_executor._prepare_scanner_runtime_parent(QUALITY_SCRATCH)
        checkout, git_directory, runtime = quality_executor._isolated_checkout(
            Path("/workspace"), QUALITY_SCRATCH, identity
        )
        require(
            checkout == QUALITY_SCRATCH / "runtime/repository",
            "Wrong checkout contract",
        )
        require(
            git_directory == QUALITY_SCRATCH / "trusted/repository.git",
            "Wrong Git contract",
        )
        require(not list(OUTPUT.iterdir()), "Code or runtime leaked into output")
        environment = quality_executor._child_environment(
            runtime, identity, {"runtime": "python"}, checkout, git_directory
        )
        (runtime / "tmp").mkdir()
        reports = checkout / "quality-reports"
        reports.mkdir()
        quality_executor._chown_tree(runtime, UID, GID)
        quality_executor._anchor_report_exchange(runtime, checkout, checkout, reports)
        python = str(runtime / "venv/bin/python")
        requirements = Path("/eng-platform-smoke/requirements.txt")
        child(
            [
                python,
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                "--no-cache-dir",
                "--only-binary=:all:",
                "--require-hashes",
                "--find-links=/eng-platform-smoke",
                "-r",
                str(requirements),
            ],
            environment,
        )
        trusted_runtime = QUALITY_SCRATCH / "trusted-runtime"
        trusted_runtime.mkdir(mode=0o700)
        gate_environment = quality_executor._trusted_gate_environment(
            trusted_runtime, checkout, git_directory, parent
        )
        control = "ENG_PLATFORM_SCANNER_RUNTIME_PARENT"
        require(
            control not in environment, "Scanner control leaked to repo environment"
        )
        require(
            gate_environment[control] == str(parent), "Gate lost derived scanner parent"
        )
        previous_parent = os.environ.get(control)
        os.environ[control] = gate_environment[control]
        try:
            require(not trusted_scanner._uid_processes(), "Repo UID phase is not empty")
            remaining_repo = subprocess.Popen(
                ["/usr/bin/sleep", "30"],
                env=environment,
                user=UID,
                group=GID,
                extra_groups=[],
            )
            try:
                require(
                    remaining_repo.pid in trusted_scanner._uid_processes(),
                    "Reduced process fixture did not start",
                )
                try:
                    trusted_scanner._runtime_environment()
                except trusted_scanner.TrustedScannerError as error:
                    require(
                        "Untrusted processes remain" in str(error),
                        "Scanner failed for a different phase error",
                    )
                else:
                    raise RuntimeError(
                        "Scanner accepted an active repository UID process"
                    )
            finally:
                remaining_repo.terminate()
                remaining_repo.wait(timeout=5)
            require(
                not trusted_scanner._uid_processes(), "Repo UID cleanup did not finish"
            )
            scanner_environment = trusted_scanner._runtime_environment()
        finally:
            if previous_parent is None:
                os.environ.pop(control, None)
            else:
                os.environ[control] = previous_parent
        parent_metadata = parent.stat()
        require(
            (parent_metadata.st_uid, parent_metadata.st_gid) == (0, 0)
            and stat.S_IMODE(parent_metadata.st_mode) == 0o711,
            "Scanner parent is not root-owned 0711",
        )
        scanner_home = Path(scanner_environment["HOME"])
        metadata = scanner_home.stat()
        require(scanner_home.parent == parent, "Scanner escaped derived scratch parent")
        require((metadata.st_uid, metadata.st_gid) == (UID, GID), "Wrong scanner owner")
        require(
            stat.S_IMODE(metadata.st_mode) == 0o700, "Scanner runtime is not private"
        )
        require(control not in scanner_environment, "Scanner retained trusted control")
        for key in ("HOME", "TMPDIR", "XDG_CACHE_HOME", "TRIVY_CACHE_DIR"):
            require(
                Path(scanner_environment[key]).is_relative_to(scanner_home)
                and Path(scanner_environment[key]).is_relative_to(SCRATCH),
                f"Scanner {key} escaped scratch runtime",
            )
        child(
            [
                python,
                "-c",
                (
                    "import os; from pathlib import Path; "
                    "assert os.geteuid() == os.getegid() == 65532; "
                    "cache = Path(os.environ['TRIVY_CACHE_DIR']); "
                    "cache.mkdir(parents=True); "
                    "assert cache.stat().st_dev == Path('/eng-platform-scratch').stat().st_dev; "
                    "probe = cache / 'scanner-cache-probe'; "
                    "stream = probe.open('wb'); "
                    "[stream.write(bytes(1024 * 1024)) for _ in range(65)]; "
                    "stream.close(); "
                    "assert probe.stat().st_size == 65 * 1024 * 1024"
                ),
            ],
            scanner_environment,
        )

        # Real kernel execution checks complement mountinfo. Native import below
        # verifies the shared object came from the scratch venv, not the image.
        for directory in (Path("/tmp"), runtime):
            executable = directory / "exec-probe"
            shutil.copyfile("/usr/bin/true", executable)
            executable.chmod(0o755)
            try:
                child([str(executable)], environment)
            except PermissionError as error:
                require(
                    directory == Path("/tmp") and error.errno == errno.EACCES,
                    "Unexpected execution denial",
                )
            else:
                require(directory == runtime, "Private /tmp permits execution")
        child([python, "/eng-platform-smoke/smoke.py", "--native-probe"], environment)
        evidence = {
            "root_read_only": True,
            "no_new_privileges": True,
            "capabilities": ["CHOWN", "KILL", "SETGID", "SETUID"],
            "tmp_mib": 64,
            "tmp_noexec": True,
            "scratch_mib": 6080,
            "scratch_exec": True,
            "uid": UID,
            "gid": GID,
            "head_sha": args.head_sha,
            "base_sha": args.base_sha,
            "native_wheel_sha256": WHEEL_SHA256,
            "scanner_runtime": "created",
            "scanner_cache_mib": 65,
            "scanner_cache_on_scratch": True,
            "scanner_phase_guard": True,
        }
        (OUTPUT / "python-isolation-smoke.json").write_text(
            json.dumps(evidence, indent=2)
        )
        print(json.dumps(evidence, sort_keys=True), flush=True)
    finally:
        # Return the host-owned evidence directory without needing CAP_FOWNER.
        os.chmod(OUTPUT, 0o700)
        os.chown(OUTPUT, args.host_uid, args.host_gid)


def native_probe() -> None:
    import ctypes

    import psycopg2
    import psycopg2._psycopg

    require(os.geteuid() == os.getegid() == UID, "Native module did not run reduced")
    require(int(status()["CapEff"], 16) == 0, "Reduced user retained capabilities")
    require(status()["NoNewPrivs"].strip() == "1", "Child lost no-new-privileges")
    native = Path(psycopg2._psycopg.__file__).resolve()
    require(
        native.is_relative_to(QUALITY_SCRATCH / "runtime/venv"),
        "Native fixture bypassed venv",
    )
    require(native.suffix == ".so", "Native shared object was not loaded")
    # A shared object on /tmp must reproduce the original mmap failure even
    # though its contents and native dependencies are otherwise valid.
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="native-noexec-") as temporary:
        blocked = Path(temporary) / native.name
        shutil.copyfile(native, blocked)
        try:
            ctypes.CDLL(str(blocked))
        except OSError as error:
            require(
                "failed to map segment" in str(error), "Unexpected native load denial"
            )
        else:
            raise RuntimeError("Private /tmp permits loading native shared objects")
    for directory in (
        Path("/workspace"),
        Path("/opt/eng-platform"),
        QUALITY_SCRATCH / "trusted/repository.git",
        OUTPUT,
    ):
        try:
            (directory / "forbidden-write-probe").write_text("must fail")
        except OSError as error:
            require(
                error.errno in (errno.EACCES, errno.EROFS), "Unexpected isolation error"
            )
        else:
            raise RuntimeError(f"Reduced user can alter {directory}")
    print(f"Native psycopg2 {psycopg2.__version__}: UID {UID}, {native}", flush=True)


def smoke(image: str) -> None:
    metadata = json.loads(
        subprocess.check_output(["docker", "image", "inspect", image])
    )[0]
    require(
        not metadata["Config"].get("Volumes"),
        "Image declares implicit anonymous volumes",
    )
    require(metadata["Architecture"] == "amd64", "Pinned native fixture requires amd64")
    print(f"Built image under test: {metadata['Id']}", flush=True)
    with tempfile.TemporaryDirectory(prefix="quality-python-smoke-") as temporary:
        root = Path(temporary)
        fixture, source, output = (
            root / name for name in ("fixture", "source", "output")
        )
        for directory in (fixture, source, output):
            directory.mkdir(mode=0o755)
            directory.chmod(0o755)
        wheel = fixture / WHEEL
        with urllib.request.urlopen(WHEEL_URL, timeout=60) as response:
            require(response.url == WHEEL_URL, "Unexpected wheel redirect")
            wheel.write_bytes(response.read(8 * 1024 * 1024))
        require(
            hashlib.sha256(wheel.read_bytes()).hexdigest() == WHEEL_SHA256,
            "Official native fixture integrity mismatch",
        )
        (fixture / "requirements.txt").write_text(
            f"psycopg2-binary==2.9.11 --hash=sha256:{WHEEL_SHA256}\n"
        )
        shutil.copyfile(__file__, fixture / "smoke.py")
        git_environment = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Quality smoke",
            "GIT_AUTHOR_EMAIL": "smoke@example.invalid",
            "GIT_COMMITTER_NAME": "Quality smoke",
            "GIT_COMMITTER_EMAIL": "smoke@example.invalid",
        }
        subprocess.run(
            ["git", "init", "--quiet", str(source)], check=True, env=git_environment
        )
        commits = []
        for content in ("base", "head"):
            (source / "fixture.txt").write_text(content)
            subprocess.run(
                ["git", "add", "fixture.txt"],
                cwd=source,
                check=True,
                env=git_environment,
            )
            subprocess.run(
                ["git", "commit", "--quiet", "-m", content],
                cwd=source,
                check=True,
                env=git_environment,
            )
            commits.append(
                subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=source, env=git_environment
                )
                .decode()
                .strip()
            )
        # The cloud workspace uses a private umask. Make only these synthetic
        # fixtures readable to the reduced container user, never the workspace.
        for directory, _names, files in os.walk(source):
            Path(directory).chmod(0o755)
            for name in files:
                (Path(directory) / name).chmod(0o644)
        for entry in fixture.iterdir():
            entry.chmod(0o644)
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--pull=never",
                "--init",
                "--network=none",
                "--read-only",
                "--user=0:0",
                "--cap-drop=ALL",
                "--cap-add=CHOWN",
                "--cap-add=SETUID",
                "--cap-add=SETGID",
                "--cap-add=KILL",
                "--security-opt=no-new-privileges",
                "--pids-limit=2048",
                "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m",
                "--tmpfs=/eng-platform-scratch:rw,exec,nosuid,nodev,size=6080m",
                "--mount",
                f"type=bind,source={source},target=/workspace,readonly",
                "--mount",
                f"type=bind,source={fixture},target=/eng-platform-smoke,readonly",
                "--mount",
                f"type=bind,source={output},target={OUTPUT}",
                "--env=PYTHONDONTWRITEBYTECODE=1",
                "--entrypoint=python3",
                image,
                "/eng-platform-smoke/smoke.py",
                "--probe",
                "--base-sha",
                commits[0],
                "--head-sha",
                commits[1],
                "--host-uid",
                str(os.getuid()),
                "--host-gid",
                str(os.getgid()),
            ],
            check=True,
            timeout=180,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", nargs="?")
    parser.add_argument("--probe", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--native-probe", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--head-sha")
    parser.add_argument("--base-sha")
    parser.add_argument("--host-uid", type=int)
    parser.add_argument("--host-gid", type=int)
    arguments = parser.parse_args()
    if arguments.native_probe:
        native_probe()
    elif arguments.probe:
        probe(arguments)
    elif arguments.image:
        smoke(arguments.image)
    else:
        parser.error("the built image is required")
