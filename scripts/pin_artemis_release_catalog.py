"""Offline Artemis release-only proposal; run with python -m scripts.pin_artemis_release_catalog."""

import argparse
import copy
import hashlib
import json
from pathlib import Path

from scripts import pin_reconnections_catalog as byte_json

EXPECTED_SHA = "b82cdad9d13a881b25b777d5f9f0cb76f909c17e9cb11e823805b13d88da3c1d"
REPOSITORY = "diegomad14/cgm-artemis-api"
SERVICES = frozenset(
    {
        "cgm-artemis-api",
        "cgm-artemis-job-dispatcher",
        "cgm-artemis-job-worker",
        "cgm-artemis-sync-worker",
        "cgm-artemis-clock-sync-worker",
        "cgm-artemis-data-recovery-worker",
        "cgm-artemis-fnd-ip-sync-worker",
        "cgm-artemis-fnd-observation-worker",
        "cgm-artemis-readings-export-worker",
        "cgm-artemis-smarti-prevention-worker",
        "cgm-artemis-wm-sweep-worker",
        "cgm-artemis-mcp-worker",
    }
)


def entry_spans(text):
    fields, _ = byte_json.object_fields(text)
    pos = byte_json.skip(text, fields["services"][0] + 1)
    spans = []
    while text[pos] != "]":
        start = pos
        _, end = byte_json.DECODER.raw_decode(text, start)
        spans.append((start, end))
        pos = byte_json.skip(text, end)
        if text[pos] == "]":
            break
        if text[pos] != ",":
            raise ValueError("services separator required")
        pos = byte_json.skip(text, pos + 1)
    return spans


def outside_entries(text, spans, indices):
    parts, end = [], 0
    for index in sorted(indices):
        start, following = spans[index]
        parts.append(text[end:start])
        end = following
    parts.append(text[end:])
    return "".join(parts).encode("utf-8")


def prepare(raw):
    if len(raw) > 32 * 1024 * 1024:
        raise ValueError("catalog too large")
    if hashlib.sha256(raw).hexdigest() != EXPECTED_SHA:
        raise ValueError("source differs from reviewed catalog SHA256")
    text = raw.decode("utf-8")
    before = byte_json.DECODER.decode(text)
    if not isinstance(before, dict) or not isinstance(before.get("services"), list):
        raise ValueError("catalog services array required")
    rows = before["services"]
    names = [row.get("service_name") for row in rows if isinstance(row, dict)]
    if (
        len(names) != len(rows)
        or any(not isinstance(name, str) for name in names)
        or len(set(names)) != len(names)
    ):
        raise ValueError("invalid or duplicate service identity")
    indices = {i for i, row in enumerate(rows) if row["service_name"] in SERVICES}
    if {rows[i]["service_name"] for i in indices} != SERVICES:
        raise ValueError("exact twelve Artemis services required")
    for index, row in enumerate(rows):
        if index not in indices:
            if str(row.get("repository", "")).casefold() == REPOSITORY.casefold():
                raise ValueError("unreviewed shared repository service")
            continue
        if row.get("repository") != REPOSITORY:
            raise ValueError("Artemis repository identity mismatch")
        if row.get("management_mode") != "managed":
            raise ValueError("Artemis service must be managed")
        if (
            not isinstance(row.get("quality"), dict)
            or row["quality"].get("enabled") is not True
        ):
            raise ValueError("Artemis quality must be enabled")
        if (
            not isinstance(row.get("release"), dict)
            or row["release"].get("executor") != "cloud_build"
        ):
            raise ValueError("Artemis release baseline changed")
    spans = entry_spans(text)
    edits = []
    for index in indices:
        start, end = spans[index]
        entry = text[start:end]
        fields, _ = byte_json.object_fields(entry)
        release_start, release_end = fields["release"]
        child = entry[release_start:release_end]
        change_start, change_end, literal = byte_json.field_edit(
            child, "executor", '"github_actions"'
        )
        absolute_start, absolute_end = (
            start + release_start + change_start,
            start + release_start + change_end,
        )
        edits.append(
            {
                "service_name": rows[index]["service_name"],
                "offset": len(text[:absolute_start].encode("utf-8")),
                "old_utf8": text[absolute_start:absolute_end],
                "new_utf8": literal,
            }
        )
    edits.sort(key=lambda edit: edit["offset"], reverse=True)
    candidate = raw
    for edit in edits:
        offset, old, new = (
            edit["offset"],
            edit["old_utf8"].encode(),
            edit["new_utf8"].encode(),
        )
        if candidate[offset : offset + len(old)] != old:
            raise ValueError("byte edit precondition failed")
        candidate = candidate[:offset] + new + candidate[offset + len(old) :]
    expected = copy.deepcopy(before)
    for index in indices:
        expected["services"][index]["release"]["executor"] = "github_actions"
    after_text = candidate.decode("utf-8")
    if byte_json.DECODER.decode(after_text) != expected:
        raise ValueError("unexpected catalog changes")
    if outside_entries(text, spans, indices) != outside_entries(
        after_text, entry_spans(after_text), indices
    ):
        raise ValueError("bytes outside Artemis entries changed")
    report = {
        "status": "prepared_not_applied",
        "source_sha256": EXPECTED_SHA,
        "candidate_sha256": hashlib.sha256(candidate).hexdigest(),
        "repository": REPOSITORY,
        "service_names": sorted(SERVICES),
        "exact_byte_edits": edits,
        "byte_edit_semantics": "Apply in order against intermediate UTF-8 bytes; verify old bytes and final SHA256.",
        "only_policy_changes": {"release.executor": ["cloud_build", "github_actions"]},
        "other_entries_semantically_identical": True,
        "bytes_outside_target_entries_identical": True,
        "deployment_and_other_fields_unchanged": True,
        "network_or_production_action": False,
    }
    return candidate, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    args = parser.parse_args()
    if args.source.resolve() == args.candidate.resolve():
        raise ValueError("candidate must be separate from source")
    candidate, report = prepare(args.source.read_bytes())
    with args.candidate.open("xb") as destination:
        destination.write(candidate)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
