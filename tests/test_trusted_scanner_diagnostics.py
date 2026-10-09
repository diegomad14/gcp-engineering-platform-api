"""Synthetic diagnostics only; never run a scanner or use application data."""

import importlib.util
import json
import os
from pathlib import Path
import shlex
import stat
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
CATEGORIES = (
    "parse",
    "partial_parse",
    "timeout",
    "memory",
    "rule",
    "internal",
    "other",
    "malformed",
)
SENTINEL = "SYNTHETIC_PRIVATE_CANARY\n\x1b[31m\u2603"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def scanner(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "docker/quality-executor"))
    return _load(
        "scanner_diagnostics", ROOT / "docker/quality-executor/trusted_scanner.py"
    )


def _value(errors):
    return {"paths": {"scanned": ["synthetic.py"]}, "results": [], "errors": errors}


def _fields(detail):
    assert detail.startswith("Semgrep reported scan errors: ")
    line = f"trusted scanner: {detail}"
    assert line.isascii() and line.isprintable() and len(line.encode("ascii")) <= 500
    return dict(field.split("=") for field in detail.partition(": ")[2].split())


KNOWN_LABELS = {
    "parse": [
        "Lexical error",
        "Syntax error",
        "Other syntax error",
        "AST builder error",
    ],
    "partial_parse": ["PartialParsing"],
    "timeout": ["Timeout", "Fixpoint timeout", "Timeout during interfile analysis"],
    "memory": ["Out of memory", "OOM during interfile analysis", "Stack overflow"],
    "rule": [
        "Rule parse error",
        "InvalidRuleSchemaError",
        "UnknownLanguageError",
        "Invalid YAML",
        "PatternParseError",
        "Pattern parse error",
        "IncompatibleRule",
        "Incompatible rule",
        "Missing plugin",
    ],
    "internal": ["Internal matching error", "SemgrepError", "Fatal error"],
}


@pytest.mark.parametrize(
    ("category", "label"),
    [
        (category, label)
        for category, labels in KNOWN_LABELS.items()
        for label in labels
    ],
)
@pytest.mark.parametrize("variant", [False, True])
def test_closed_categories_do_not_change_rejection(scanner, category, label, variant):
    error = {"type": [label, SENTINEL] if variant else label, "message": SENTINEL}
    fields = _fields(scanner._semgrep_error_detail([error]))
    assert fields[category] == "1"
    assert sum(int(fields[key]) for key in CATEGORIES) == 1
    # Existing policy accepts only a list beginning with PartialParsing.
    if variant and label == "PartialParsing":
        assert scanner._validate_scanner_result("semgrep", _value([error])) is None
    else:
        with pytest.raises(scanner.TrustedScannerError, match="reported scan errors"):
            scanner._validate_scanner_result("semgrep", _value([error]))


@pytest.mark.parametrize("size", [0, 1, 4095, 4096, 4097, 65534, 65535, 65536, 70000])
def test_count_boundaries_are_explicit_and_bounded(scanner, size):
    errors = [{"type": "Timeout"}] * size
    fields = _fields(scanner._semgrep_error_detail(errors))
    assert fields == {
        "state": "COLLECTED",
        "total": str(min(size, 65535)),
        "capped": str(int(size > 65535)),
        "processed": str(min(size, 4096)),
        "incomplete": str(int(size > 4096)),
        "entry_limit": "4096",
        "count_limit": "65535",
        **{
            category: str(min(size, 4096)) if category == "timeout" else "0"
            for category in CATEGORIES
        },
    }
    assert sum(int(fields[key]) for key in CATEGORIES) == int(fields["processed"])


def test_summary_never_visits_entries_beyond_limit(scanner):
    class BoundedList(list):
        def __getitem__(self, index):
            assert isinstance(index, int) and index < 4096
            return super().__getitem__(index)

    fields = _fields(
        scanner._semgrep_error_detail(BoundedList([{"type": "Timeout"}] * 4097))
    )
    assert fields["processed"] == "4096" and fields["incomplete"] == "1"


@pytest.mark.parametrize("errors", [None, False, 4, SENTINEL, {}, {"type": SENTINEL}])
def test_invalid_errors_shape_has_no_invented_count(scanner, errors):
    with pytest.raises(scanner.TrustedScannerError) as failure:
        scanner._validate_scanner_result("semgrep", _value(errors))
    assert _fields(str(failure.value)) == {"state": "INVALID_OUTPUT"}


@pytest.mark.parametrize(
    "kind",
    [SENTINEL, "Timeout\n", "Timeout ", "timeout", "x" * 100000, [SENTINEL, SENTINEL]],
)
def test_unknown_labels_never_escape(scanner, kind):
    fields = _fields(scanner._semgrep_error_detail([{"type": kind}]))
    assert fields["other"] == "1"
    assert "SYNTHETIC_PRIVATE_CANARY" not in str(fields)


@pytest.mark.parametrize(
    "error",
    [
        None,
        False,
        4,
        SENTINEL,
        [],
        {},
        {"type": None},
        {"type": []},
        {"type": [None]},
        {"type": {"payload": SENTINEL}},
    ],
)
def test_malformed_entries_never_escape(scanner, error):
    fields = _fields(scanner._semgrep_error_detail([error]))
    assert fields["malformed"] == "1"


def test_mixed_categories_use_only_closed_keys(scanner):
    errors = [{"type": labels[0]} for labels in KNOWN_LABELS.values()]
    errors += [{"type": "unrecognized"}, None]
    fields = _fields(scanner._semgrep_error_detail(errors))
    assert all(fields[key] == "1" for key in CATEGORIES)


@pytest.mark.parametrize(
    "bad_summary", ["x" * 485, "bad\nline", "bad\tline", "bad\u2603", None]
)
def test_faulty_formatter_falls_back(scanner, monkeypatch, bad_summary):
    monkeypatch.setattr(scanner, "_semgrep_error_summary", lambda _: bad_summary)
    assert scanner._semgrep_error_detail([None]) == scanner._SEMGREP_ERROR_GENERIC


def test_summary_failure_keeps_generic_rejection(scanner, monkeypatch):
    def broken(_):
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(scanner, "_semgrep_error_summary", broken)
    with pytest.raises(scanner.TrustedScannerError) as failure:
        scanner._validate_scanner_result("semgrep", _value([None]))
    assert str(failure.value) == "Semgrep reported scan errors"
    assert failure.value.__context__ is None


@pytest.mark.parametrize(
    "errors",
    [
        [],
        [{"type": ["PartialParsing"]}],
        [{"type": ["PartialParsing", SENTINEL]}] * 4097,
    ],
)
def test_accepted_scans_never_request_a_summary(scanner, monkeypatch, errors):
    def must_not_run(_):
        pytest.fail("diagnostics must not decide acceptance")

    monkeypatch.setattr(scanner, "_semgrep_error_detail", must_not_run)
    assert scanner._validate_scanner_result("semgrep", _value(errors)) is None
    value = _value([])
    del value["errors"]
    assert scanner._validate_scanner_result("semgrep", value) is None
    assert scanner._validate_scanner_result("trivy", {}) is None


def test_rejection_after_summary_window_still_blocks(scanner):
    errors = [{"type": ["PartialParsing"]}] * 4096 + [{"type": "Timeout"}]
    with pytest.raises(scanner.TrustedScannerError) as failure:
        scanner._validate_scanner_result("semgrep", _value(errors))
    fields = _fields(str(failure.value))
    assert fields["partial_parse"] == "4096"
    assert fields["timeout"] == "0" and fields["incomplete"] == "1"


@pytest.fixture
def capture_run(scanner, monkeypatch, tmp_path):
    reports, staging = tmp_path / "reports", tmp_path / "staging"
    reports.mkdir(mode=0o700)
    staging.mkdir(mode=0o700)
    target = reports / "semgrep.json"
    target.write_text("original")
    target.chmod(0o600)
    monkeypatch.setenv("ENG_PLATFORM_TRUSTED_REPORT_DIRECTORY", str(reports))
    monkeypatch.setenv("ENG_PLATFORM_SCANNER_STAGING_DIRECTORY", str(staging))
    monkeypatch.setattr(scanner.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        scanner, "_trusted_binary", lambda _: Path("/synthetic/semgrep")
    )
    real_stat = Path.stat

    def image_owned_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if path.is_relative_to(tmp_path):
            fields = list(result)
            fields[4] = fields[5] = 0
            return os.stat_result(fields)
        return result

    # Emulate image ownership only. Parsing, sealing, modes, and cleanup are real.
    monkeypatch.setattr(Path, "stat", image_owned_stat)

    def run(raw, returncode=0):
        def process(binary, args, output):
            output.write(raw)
            return returncode

        monkeypatch.setattr(scanner, "_run_process", process)
        return scanner.run("semgrep", ["--output", str(target)])

    return run, target, staging


@pytest.mark.parametrize("helper_failure", [False, True])
def test_rejection_cleans_capture_and_does_not_seal(
    scanner, monkeypatch, capture_run, helper_failure
):
    run, target, staging = capture_run
    if helper_failure:

        def broken(_):
            raise RuntimeError(SENTINEL)

        monkeypatch.setattr(scanner, "_semgrep_error_summary", broken)
    with pytest.raises(scanner.TrustedScannerError, match="reported scan errors"):
        run(json.dumps(_value([{"type": "Timeout", "message": SENTINEL}])).encode())
    assert target.read_text() == "original"
    assert list(staging.iterdir()) == []
    assert list(target.parent.iterdir()) == [target]


@pytest.mark.parametrize("raw", [b"{not-json", b"[]", b"\xff", b'{"paths":NaN}'])
def test_invalid_json_and_top_level_remain_rejected(scanner, capture_run, raw):
    run, target, staging = capture_run
    with pytest.raises(scanner.TrustedScannerError):
        run(raw)
    assert target.read_text() == "original" and list(staging.iterdir()) == []


@pytest.mark.parametrize("returncode", [0, 2, 124])
@pytest.mark.parametrize("errors", [None, [], [{"type": ["PartialParsing", []]}]])
def test_successful_validation_preserves_seal_and_scanner_exit(
    capture_run, returncode, errors
):
    run, target, staging = capture_run
    value = _value(errors)
    if errors is None:
        del value["errors"]
    assert run(json.dumps(value).encode(), returncode) == returncode
    assert json.loads(target.read_text()) == value
    assert target.stat().st_mode & 0o777 == 0o600
    assert list(staging.iterdir()) == []


@pytest.mark.parametrize(
    ("index", "unsafe_value"),
    [
        (0, stat.S_IFREG | 0o777),
        (0, stat.S_IFREG | 0o444),
        (0, stat.S_IFDIR | 0o555),
        (3, 2),
        (4, 1),
        (5, 1),
    ],
)
def test_unsafe_binary_metadata_still_blocks(scanner, monkeypatch, index, unsafe_value):
    fields = [stat.S_IFREG | 0o555, 0, 0, 1, 0, 0, 0, 0, 0, 0]
    monkeypatch.setattr(Path, "stat", lambda *args, **kwargs: os.stat_result(fields))
    assert scanner._trusted_binary("semgrep") == Path("/usr/local/bin/semgrep")
    fields[index] = unsafe_value
    with pytest.raises(
        scanner.TrustedScannerError, match="binary permissions are unsafe"
    ):
        scanner._trusted_binary("semgrep")


def test_real_cli_transports_safe_summary_to_normalized_detail(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.syspath_prepend(str(ROOT / "scripts/quality"))
    monkeypatch.syspath_prepend(str(ROOT / "docker/quality-executor"))
    gate = _load("diagnostic_gate", ROOT / "scripts/quality/quality_gate.py")
    executor = _load(
        "diagnostic_executor", ROOT / "docker/quality-executor/quality_executor.py"
    )
    reports, staging = [tmp_path / name for name in ("reports", "staging")]
    scanner_parent = tmp_path / "trusted-scanner-runtime"
    # Emulate the motor's traversable root-owned scratch ancestor in this
    # synthetic CLI fixture; root ownership is modeled below.
    tmp_path.chmod(0o711)
    scanner_parent.mkdir(mode=0o711)
    scanner_parent.chmod(0o711)
    runtime = scanner_parent / "runtime"
    for directory in (reports, staging, runtime):
        directory.mkdir(mode=0o700)
    target = reports / "semgrep.json"
    target.touch(mode=0o600)
    payload = tmp_path / "synthetic-input.json"
    payload.write_text(
        json.dumps(
            _value(
                [
                    {
                        "type": "Timeout",
                        "message": SENTINEL,
                        "path": "/private/" + SENTINEL,
                        "rule_id": "rule-" + SENTINEL,
                        "lines": "source-" + SENTINEL,
                        "help": "https://example.invalid/" + SENTINEL,
                    }
                ]
            )
        )
    )
    cli = tmp_path / "synthetic_cli.py"
    cli.write_text("""import os, runpy, stat, sys, time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
import untrusted_command
root, payload = Path(sys.argv[2]), Path(sys.argv[3])
real_stat = Path.stat

def owned_stat(path, *args, **kwargs):
    if str(path) == "/usr/local/bin/semgrep":
        fields = [stat.S_IFREG | 0o555, 0, 0, 1, 0, 0, 0, 0, 0, 0]
    else:
        fields = list(real_stat(path, *args, **kwargs))
    fields[4] = fields[5] = 0
    if path in root.parents:
        # Pytest's host-private ancestors model the image's traversable root
        # scratch mount. This fixture never executes a reduced-UID process.
        fields[0] |= 0o001
    return os.stat_result(fields)

class SyntheticProcess:
    pid = 123456789
    def __init__(self, command, **kwargs):
        kwargs["stdout"].write(payload.read_bytes())
    def wait(self, **kwargs):
        return 0

os.environ["ENG_PLATFORM_TRUSTED_REPORT_DIRECTORY"] = str(root / "reports")
os.environ["ENG_PLATFORM_SCANNER_STAGING_DIRECTORY"] = str(root / "staging")
os.environ["ENG_PLATFORM_SCANNER_DEADLINE"] = str(time.monotonic() + 30)
os.environ["ENG_PLATFORM_SCANNER_RUNTIME_PARENT"] = str(root / "trusted-scanner-runtime")
wrapper = str(Path(sys.argv[1]) / "trusted_scanner.py")
sys.argv = [wrapper, "semgrep", "--output", str(root / "reports/semgrep.json")]
with patch("os.geteuid", return_value=0), patch("os.chown"), \\
     patch.object(Path, "stat", owned_stat), \\
     patch("tempfile.mkdtemp", return_value=str(root / "trusted-scanner-runtime/runtime")), \\
     patch("subprocess.Popen", SyntheticProcess), \\
     patch.object(untrusted_command, "_kill_descendants"):
    runpy.run_path(wrapper, run_name="__main__")
""")
    command = shlex.join(
        [
            sys.executable,
            str(cli),
            str(ROOT / "docker/quality-executor"),
            str(tmp_path),
            str(payload),
        ]
    )
    log = tmp_path / "scanner.log"
    result = gate._run(command, tmp_path, log)
    assert result["returncode"] == 1
    check = gate._check(name="Semgrep SAST", category="sast", result=result, findings=0)
    normalized = executor._normalize_report({"checks": [check]})
    assert normalized["checks"][0]["status"] == "FAILED"
    detail = check["details"]
    assert detail == log.read_text().strip() == result["output"].strip()
    assert detail.startswith("trusted scanner: Semgrep reported scan errors: ")
    assert _fields(detail.removeprefix("trusted scanner: "))["timeout"] == "1"
    assert list(staging.iterdir()) == [] and target.read_bytes() == b""
    output = capsys.readouterr()
    for text in (
        log.read_text(),
        result["output"],
        json.dumps(normalized),
        output.out,
        output.err,
    ):
        assert "SYNTHETIC_PRIVATE_CANARY" not in text
        assert "https://example.invalid/" not in text
        assert "/private/" not in text
        assert "Traceback" not in text
