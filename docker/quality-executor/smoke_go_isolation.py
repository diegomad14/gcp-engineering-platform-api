#!/usr/bin/env python3
"""Run native Go tests with the canonical noexec scratch and sealed volume."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid


OUTPUT = Path("/eng-platform-output")
UID = GID = 65532


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def drop_privileges() -> None:
    os.setgroups([])
    os.setgid(GID)
    os.setuid(UID)


def probe() -> None:
    sys.path.insert(0, "/opt/eng-platform")
    import quality_executor

    require(os.geteuid() == 0, "Supervisor must start as root")
    require(bool(os.statvfs("/tmp").f_flag & os.ST_NOEXEC), "Scratch must stay noexec")
    require(
        not (os.statvfs(OUTPUT).f_flag & os.ST_NOEXEC),
        "Output volume must permit execution",
    )
    root = Path("/tmp/go-isolation")
    source = root / "source"
    source.mkdir(parents=True)
    (source / "go.mod").write_text("module example.org/smoke\ngo 1.27.2\n")
    (source / "value.go").write_text(
        "package smoke\nfunc Positive(v int) bool { return v > 0 }\n"
    )
    (source / "value_test.go").write_text(r"""
package smoke
import ("os"; "path/filepath"; "strings"; "testing")
func TestIsolation(t *testing.T) {
    if os.Getuid() != 65532 || os.Getgid() != 65532 { t.Fatal("wrong UID/GID") }
    data, err := os.ReadFile("/proc/self/status")
    if err != nil || !strings.Contains(string(data), "CapEff:\t0000000000000000") ||
        !strings.Contains(string(data), "NoNewPrivs:\t1") { t.Fatal("privileges leaked") }
    for _, key := range []string{"GITHUB_TOKEN", "GH_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS"} {
        if os.Getenv(key) != "" { t.Fatal("credential environment leaked") }
    }
    if os.Getenv("GOTMPDIR") != "/eng-platform-output/.go-temporary" { t.Fatal("wrong GOTMPDIR") }
    if !Positive(1) || Positive(-1) { t.Fatal("fixture failed") }
    if err := os.WriteFile("/eng-platform-output/quality-result.json", []byte("forged"), 0644);
        !os.IsPermission(err) { t.Fatal("sealed manifest writable") }
    if err := os.WriteFile("/eng-platform-output/new-result.json", []byte("forged"), 0644);
        !os.IsPermission(err) { t.Fatal("evidence parent writable") }
    if err := os.Chmod("/eng-platform-output", 0777); !os.IsPermission(err) { t.Fatal("parent ownership leaked") }
    attack := filepath.Join(os.Getenv("GOTMPDIR"), "replace-result")
    if err := os.WriteFile(attack, []byte("forged"), 0644); err != nil { t.Fatal(err) }
    if err := os.Rename(attack, "/eng-platform-output/quality-result.json");
        !os.IsPermission(err) { t.Fatal("sealed manifest replaceable") }
}
""")
    temporary = quality_executor._prepare_go_temporary_directory(OUTPUT)
    quality_executor._write_json(OUTPUT / "quality-result.json", {"sealed": True})
    environment = quality_executor._child_environment(
        root,
        {"head_sha": "a" * 40, "base_sha": "b" * 40},
        {"runtime": "go"},
        go_temporary_directory=temporary,
    )
    (root / "tmp").mkdir()
    quality_executor._chown_tree(root, UID, GID)
    try:
        subprocess.run(
            [
                "go",
                "test",
                "-race",
                "-count=1",
                "-covermode=atomic",
                "-coverprofile=coverage.out",
                "./...",
            ],
            cwd=source,
            env=environment,
            preexec_fn=drop_privileges,
            check=True,
            timeout=300,
        )
        require(
            (source / "coverage.out").read_text().startswith("mode: atomic\n"),
            "Native race coverage missing",
        )
        require(
            json.loads((OUTPUT / "quality-result.json").read_text())
            == {"sealed": True},
            "Manifest changed",
        )
    finally:
        quality_executor._terminate_untrusted_processes()
        quality_executor._remove_go_temporary_directory(temporary, OUTPUT)
    require(not temporary.exists(), "Go temporaries remained in evidence volume")
    require(
        [path.name for path in OUTPUT.iterdir()] == ["quality-result.json"],
        "Unexpected evidence siblings",
    )
    print(
        "Go isolation smoke: PASS (native race, noexec scratch, sealed manifest, scoped cleanup)"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", nargs="?")
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()
    if args.probe:
        probe()
        return
    if not args.image:
        parser.error("image is required")
    volume = "eng-platform-go-smoke-" + uuid.uuid4().hex
    subprocess.run(
        ["docker", "volume", "create", volume], check=True, capture_output=True
    )
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--init",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "CHOWN",
                "--cap-add",
                "SETUID",
                "--cap-add",
                "SETGID",
                "--cap-add",
                "KILL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "2048",
                "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=6g",
                "--volume",
                f"{volume}:{OUTPUT}:rw",
                "--env",
                "GITHUB_TOKEN=canary-must-not-leak",
                "--env",
                "GOOGLE_APPLICATION_CREDENTIALS=canary-must-not-leak",
                "--entrypoint",
                "python3",
                args.image,
                "/opt/eng-platform/smoke_go_isolation.py",
                "--probe",
            ],
            check=True,
            timeout=360,
        )
    finally:
        subprocess.run(
            ["docker", "volume", "rm", volume], check=True, capture_output=True
        )


if __name__ == "__main__":
    main()
