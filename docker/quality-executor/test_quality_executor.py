from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))

import quality_executor  # noqa: E402
import quality_profiles  # noqa: E402
import trusted_scanner  # noqa: E402
import untrusted_command  # noqa: E402


class QualityProfilesTest(unittest.TestCase):
    def test_trivy_uses_only_the_pinned_exception_policy(self) -> None:
        self.assertEqual(
            ["fs", ".", "--ignorefile", "/opt/eng-platform/trivyignore.yaml"],
            trusted_scanner._trusted_scan_args("trivy", ["fs", "."]),
        )
        with self.assertRaisesRegex(
            trusted_scanner.TrustedScannerError, "server-owned"
        ):
            trusted_scanner._trusted_scan_args("trivy", ["--ignorefile=./own.yaml"])

    def test_hash_is_exact_canonical_shared_payload(self) -> None:
        document = quality_profiles.profile_document()
        for service in quality_profiles.available_services():
            payload = {
                "schema_version": document["schema_version"],
                "service_name": service,
                "profile": document["profiles"][service],
            }
            expected = hashlib.sha256(
                json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
            self.assertEqual(expected, quality_profiles.profile_hash(service))

    def test_profile_hash_mismatch_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            quality_profiles.QualityProfileError, "Authorized profile hash"
        ):
            quality_profiles.verify_profile_hash("eng-platform-api", "0" * 64)

    def test_profiles_preserve_security_and_coverage_contracts(self) -> None:
        api = quality_profiles.profile_for("eng-platform-api")
        self.assertTrue(api["container_smoke"])
        self.assertEqual("", api["commands"]["build"])

        for service in ("eng-platform-web", "cgm-sanplat-web"):
            tests = quality_profiles.profile_for(service)["commands"]["tests"]
            self.assertIn("coverage.reportsDirectory=quality-reports", tests)
            self.assertIn("coverage.reporter=lcov", tests)

        bot = quality_profiles.profile_for("cgm-bot-api")
        self.assertIn("git diff --name-only -z", bot["commands"]["format"])
        advisory = {item["name"]: item for item in bot["extra"]}
        self.assertFalse(advisory["Mypy advisory"]["blocking"])
        self.assertFalse(advisory["Dependency audit advisory"]["blocking"])


class QualityIsolationTest(unittest.TestCase):
    def _identity_environment(self) -> dict[str, str]:
        return {
            "ENG_PLATFORM_RELEASE_EXECUTION_ID": "execution-1",
            "ENG_PLATFORM_RELEASE_FINGERPRINT": "f" * 64,
            "ENG_PLATFORM_RELEASE_SERVICE": "eng-platform-api",
            "ENG_PLATFORM_RELEASE_REPOSITORY": "diegomad14/eng-platform-api",
            "ENG_PLATFORM_RELEASE_HEAD_SHA": "a" * 40,
            "ENG_PLATFORM_RELEASE_BASE_SHA": "b" * 40,
            "ENG_PLATFORM_RELEASE_OPERATION": "pr_quality",
            "ENG_PLATFORM_RELEASE_PROFILE_SHA256": quality_profiles.profile_hash(
                "eng-platform-api"
            ),
            "ENG_PLATFORM_PROVIDER_RUN_ID": "1234",
            "ENG_PLATFORM_API_URL": "https://platform.example.test",
        }

    def test_node_child_environment_does_not_inherit_credentials(self) -> None:
        identity = {
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
        }
        profile = quality_profiles.profile_for("eng-platform-web")
        sensitive = {
            name: "must-not-leak" for name in quality_executor._SENSITIVE_ENV_NAMES
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(os.environ, sensitive, clear=False),
        ):
            environment = quality_executor._child_environment(
                Path(directory), identity, profile
            )
        self.assertTrue(quality_executor._SENSITIVE_ENV_NAMES.isdisjoint(environment))

    def test_event_token_is_private_and_required_on_callback(self) -> None:
        token = "secret-" + "x" * 58
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(os.environ, self._identity_environment(), clear=True),
            mock.patch.object(quality_executor, "_identity_token", return_value="oidc"),
            mock.patch.object(
                quality_executor,
                "_json_request",
                side_effect=[{"event_token": token}, {"accepted": True}],
            ) as request,
        ):
            control = Path(directory)
            self.assertEqual(token, quality_executor._claim_event_token(control))
            self.assertEqual(0, (control / "event-token").stat().st_mode & 0o077)
            quality_executor._event(1, "running_quality", control)

        headers = request.call_args_list[1].kwargs["headers"]
        self.assertEqual(token, headers["X-Eng-Platform-Event-Token"])

    def test_report_hash_applies_backend_quality_check_defaults(self) -> None:
        report = {
            "checks": [
                {
                    "name": "example",
                    "category": "tests",
                    "status": "PASSED",
                }
            ]
        }
        normalized = quality_executor._normalize_report(report)
        self.assertEqual(0, normalized["checks"][0]["findings"])
        self.assertEqual(0.0, normalized["checks"][0]["duration_seconds"])
        self.assertEqual(64, len(quality_executor._report_hash(report)))

    def test_untrusted_environment_manifest_is_private_and_rejects_oidc(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "environment.json"
            quality_executor._write_private_json(path, {"PATH": "/usr/bin"})
            self.assertEqual(0, path.stat().st_mode & 0o077)
            self.assertEqual({"PATH": "/usr/bin"}, untrusted_command._environment(path))

            quality_executor._write_private_json(
                path, {"ACTIONS_ID_TOKEN_REQUEST_TOKEN": "forbidden"}
            )
            with self.assertRaisesRegex(RuntimeError, "Credential environment"):
                untrusted_command._environment(path)

    def test_profile_command_is_wrapped_by_fixed_root_supervisor(self) -> None:
        wrapped = quality_executor._supervised_command(
            "npm run test:coverage",
            Path("/trusted/environment.json"),
            Path("/workspace/quality-reports"),
            time.monotonic() + 60,
        )
        self.assertIn("/opt/eng-platform/untrusted_command.py", wrapped)
        self.assertIn("--environment /trusted/environment.json", wrapped)
        self.assertIn("--report-directory /workspace/quality-reports", wrapped)
        self.assertNotIn("npm run test:coverage", wrapped)

    @unittest.skipUnless(
        os.geteuid() == 0 and Path("/proc").is_dir(),
        "adversarial supervisor test requires the Linux executor container",
    )
    def test_supervisor_kills_detached_writer_and_protects_trusted_report(
        self,
    ) -> None:
        marker = Path(f"/tmp/eng-platform-supervisor-attempted-{os.getpid()}")
        late_report = Path(f"/tmp/eng-platform-supervisor-late-{os.getpid()}")
        marker.unlink(missing_ok=True)
        late_report.unlink(missing_ok=True)
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                root.chmod(0o1777)
                workspace = root / "workspace"
                workspace.mkdir(mode=0o1777)
                exchange = workspace / "quality-reports"
                exchange.mkdir(mode=0o1777)
                external = root / "external"
                external.mkdir(mode=0o777)
                external.chmod(0o755)
                external_result = external / "api-container-smoke.json"
                external_result.write_text('{"trusted":true}\n', encoding="utf-8")
                external_result.chmod(0o444)
                external.chmod(0o555)
                trusted = root / "trusted"
                trusted.mkdir(mode=0o700)
                report = trusted / "quality-report.json"
                report.write_text('{"trusted":true}\n', encoding="utf-8")
                report.chmod(0o600)
                environment_path = trusted / "environment.json"
                quality_executor._write_private_json(
                    environment_path,
                    {
                        "PATH": "/usr/local/bin:/usr/bin:/bin",
                        "TRUSTED_REPORT": str(report),
                        "ATTEMPT_MARKER": str(marker),
                        "LATE_REPORT": str(late_report),
                        "REPORT_DIRECTORY": str(exchange),
                        "MOVED_DIRECTORY": str(workspace / "replaced-reports"),
                        "CHECKOUT_DIRECTORY": str(workspace),
                        "MOVED_CHECKOUT": str(root / "replaced-workspace"),
                        "EXTERNAL_RESULT": str(external_result),
                    },
                )
                script = """
import os
import time

child = os.fork()
if child == 0:
    os.setsid()
    renamed_report = False
    renamed_checkout = False
    replaced_external = False
    try:
        os.rename(os.environ["REPORT_DIRECTORY"], os.environ["MOVED_DIRECTORY"])
        renamed_report = True
    except OSError:
        pass
    try:
        os.unlink(os.environ["EXTERNAL_RESULT"])
        with open(os.environ["EXTERNAL_RESULT"], "w", encoding="utf-8") as handle:
            handle.write("forged")
        replaced_external = True
    except OSError:
        pass
    try:
        os.rename(os.environ["CHECKOUT_DIRECTORY"], os.environ["MOVED_CHECKOUT"])
        renamed_checkout = True
    except OSError:
        pass
    try:
        with open(os.environ["TRUSTED_REPORT"], "w", encoding="utf-8") as handle:
            handle.write("forged")
    except OSError:
        pass
    with open(os.environ["ATTEMPT_MARKER"], "w", encoding="utf-8") as handle:
        handle.write(
            f"{os.getpid()}:{int(renamed_report)}:{int(renamed_checkout)}:"
            f"{int(replaced_external)}"
        )
    time.sleep(0.75)
    with open(os.environ["LATE_REPORT"], "w", encoding="utf-8") as handle:
        handle.write("forged")
    os._exit(0)

deadline = time.monotonic() + 2
while not os.path.exists(os.environ["ATTEMPT_MARKER"]):
    if time.monotonic() >= deadline:
        raise RuntimeError("detached writer did not start")
    time.sleep(0.01)
"""
                command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(Path(untrusted_command.__file__).resolve()),
                        "--environment",
                        str(environment_path),
                        "--report-directory",
                        str(exchange),
                        "--deadline",
                        f"{time.monotonic() + 5:.6f}",
                        "--command",
                        base64.b64encode(command.encode()).decode("ascii"),
                    ],
                    check=False,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
                self.assertEqual(0, completed.returncode, completed.stdout)
                self.assertTrue(marker.is_file())
                (
                    pid_value,
                    renamed_report,
                    renamed_checkout,
                    replaced_external,
                ) = marker.read_text(encoding="utf-8").split(":")
                detached_pid = int(pid_value)
                self.assertEqual("0", renamed_report)
                self.assertEqual("0", renamed_checkout)
                self.assertEqual("0", replaced_external)
                time.sleep(1)
                self.assertFalse(late_report.exists())
                self.assertFalse(Path(f"/proc/{detached_pid}").exists())
                self.assertEqual(
                    '{"trusted":true}\n', report.read_text(encoding="utf-8")
                )
                self.assertEqual(
                    '{"trusted":true}\n',
                    external_result.read_text(encoding="utf-8"),
                )
        finally:
            marker.unlink(missing_ok=True)
            late_report.unlink(missing_ok=True)

    @unittest.skipUnless(
        os.geteuid() == 0 and Path("/proc").is_dir(),
        "deadline supervisor test requires the Linux executor container",
    )
    def test_supervisor_enforces_repository_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o1777)
            exchange = root / "quality-reports"
            exchange.mkdir(mode=0o1777)
            trusted = root / "trusted"
            trusted.mkdir(mode=0o700)
            environment_path = trusted / "environment.json"
            quality_executor._write_private_json(
                environment_path, {"PATH": "/usr/local/bin:/usr/bin:/bin"}
            )
            started = time.monotonic()
            completed = subprocess.run(
                [
                    sys.executable,
                    str(Path(untrusted_command.__file__).resolve()),
                    "--environment",
                    str(environment_path),
                    "--report-directory",
                    str(exchange),
                    "--deadline",
                    f"{time.monotonic() + 0.1:.6f}",
                    "--command",
                    base64.b64encode(b"sleep 10").decode("ascii"),
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self.assertEqual(124, completed.returncode, completed.stdout)
            self.assertLess(time.monotonic() - started, 2)

    def test_postgres_profile_passes_both_disposable_loopback_dsns(self) -> None:
        fnd = "postgresql://postgres:disposable@127.0.0.1:5432/fnd_test"
        wm = "postgresql+psycopg://postgres:disposable@localhost/wm_test"
        with mock.patch.dict(
            os.environ,
            {"FND_TEST_POSTGRES_DSN": fnd, "WM_TEST_POSTGRES_DSN": wm},
            clear=True,
        ):
            self.assertEqual(
                {
                    "FND_TEST_POSTGRES_DSN": fnd,
                    "WM_TEST_POSTGRES_DSN": wm,
                },
                quality_executor._postgres_environment(),
            )

    def test_postgres_profile_rejects_missing_or_non_loopback_dsn(self) -> None:
        safe = "postgresql://postgres:disposable@127.0.0.1:5432/wm_test"
        unsafe = "postgresql://postgres:secret@database.example:5432/fnd_test"
        with mock.patch.dict(
            os.environ,
            {"FND_TEST_POSTGRES_DSN": unsafe, "WM_TEST_POSTGRES_DSN": safe},
            clear=True,
        ):
            with self.assertRaisesRegex(
                quality_executor.QualityExecutorError, "must use loopback"
            ):
                quality_executor._postgres_environment()

        with mock.patch.dict(os.environ, {"FND_TEST_POSTGRES_DSN": safe}, clear=True):
            with self.assertRaisesRegex(
                quality_executor.QualityExecutorError, "must use loopback"
            ):
                quality_executor._postgres_environment()

    def test_prepare_fetches_exact_repository_with_host_scoped_token(self) -> None:
        identity = {
            "repository": "diegomad14/eng-platform-api",
            "base_sha": "b" * 40,
        }
        calls: list[tuple[tuple[str, ...], dict[str, str] | None]] = []

        def fake_git(_cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
            calls.append((args, dict(env) if env is not None else None))
            return identity["base_sha"]

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(quality_executor, "_verify_checkout"),
            mock.patch.object(
                quality_executor, "_source_token", return_value="read-only-token"
            ),
            mock.patch.object(quality_executor, "_git", side_effect=fake_git),
        ):
            quality_executor._prepare_history(Path(directory), identity)

        fetch_args, fetch_environment = calls[0]
        self.assertIn("https://github.com/diegomad14/eng-platform-api.git", fetch_args)
        self.assertIn(
            "+" + identity["base_sha"] + ":refs/heads/eng-platform-quality-base",
            fetch_args,
        )
        self.assertNotIn("origin", fetch_args)
        self.assertIsNotNone(fetch_environment)
        assert fetch_environment is not None
        self.assertEqual(
            "http.https://github.com/.extraHeader",
            fetch_environment["GIT_CONFIG_KEY_0"],
        )
        self.assertNotIn("http.extraHeader", fetch_environment.values())
        header = fetch_environment["GIT_CONFIG_VALUE_0"]
        self.assertTrue(header.startswith("Authorization: Basic "))
        encoded = header.removeprefix("Authorization: Basic ")
        self.assertEqual(
            "x-access-token:read-only-token",
            base64.b64decode(encoded, validate=True).decode(),
        )
        self.assertEqual("false", fetch_environment["GIT_CONFIG_VALUE_1"])

    def test_shallow_source_keeps_fetched_base_in_isolated_clone(self) -> None:
        def git(cwd: Path, *args: str) -> str:
            return subprocess.run(
                ["git", *args],
                cwd=cwd,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ).stdout.strip()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            git(repository, "init", "-q")
            git(repository, "config", "user.email", "quality@example.test")
            git(repository, "config", "user.name", "Quality Test")
            (repository / "source.txt").write_text("base\n")
            git(repository, "add", "source.txt")
            git(repository, "commit", "-qm", "base")
            base_sha = git(repository, "rev-parse", "HEAD")
            (repository / "source.txt").write_text("head\n")
            git(repository, "commit", "-qam", "head")
            head_sha = git(repository, "rev-parse", "HEAD")

            shallow = root / "shallow"
            git(root, "clone", "-q", "--depth=1", repository.as_uri(), str(shallow))
            self.assertEqual(head_sha, git(shallow, "rev-parse", "HEAD"))
            git(
                shallow,
                "fetch",
                "-q",
                repository.as_uri(),
                f"+{base_sha}:refs/heads/eng-platform-quality-base",
            )
            isolated = root / "isolated"
            git(root, "clone", "-q", "--local", "--no-single-branch", "--no-checkout", str(shallow), str(isolated))
            self.assertEqual(base_sha, git(isolated, "rev-parse", f"{base_sha}^{{commit}}"))

    def test_prepare_rejects_repository_url_injection(self) -> None:
        identity = {
            "repository": "github.com/owner/repo",
            "base_sha": "b" * 40,
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(quality_executor, "_verify_checkout"),
            mock.patch.object(quality_executor, "_source_token") as source_token,
        ):
            with self.assertRaisesRegex(
                quality_executor.QualityExecutorError, "repository identity"
            ):
                quality_executor._prepare_history(Path(directory), identity)
        source_token.assert_not_called()


if __name__ == "__main__":
    unittest.main()
