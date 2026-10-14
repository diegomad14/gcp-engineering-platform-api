#!/usr/bin/env python3
"""Offline proposal only: never uploads, overwrites input, or changes Cloud Run."""

import argparse
import copy
import hashlib
import json
from pathlib import Path

SERVICE = "cgm-reconnections-api"
EXPECTED_SHA = "7a0d21c78e34ce05dbcdc75744c66b6c189624f9c3630cdf40ce6255c5b980a7"


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def invalid_constant(_value):
    raise ValueError("nonstandard JSON constant")


DECODER = json.JSONDecoder(object_pairs_hook=unique, parse_constant=invalid_constant)


def skip(text, pos):
    while pos < len(text) and text[pos] in " \t\r\n":
        pos += 1
    return pos


def object_fields(text, start=0):
    pos = skip(text, start)
    if text[pos] != "{":
        raise ValueError("object required")
    pos = skip(text, pos + 1)
    fields = {}
    while text[pos] != "}":
        key, key_end = DECODER.raw_decode(text, pos)
        if not isinstance(key, str) or key in fields:
            raise ValueError("invalid object key")
        pos = skip(text, key_end)
        if text[pos] != ":":
            raise ValueError("colon required")
        value_start = skip(text, pos + 1)
        _value, value_end = DECODER.raw_decode(text, value_start)
        fields[key] = (value_start, value_end)
        pos = skip(text, value_end)
        if text[pos] == "}":
            break
        if text[pos] != ",":
            raise ValueError("comma required")
        pos = skip(text, pos + 1)
    return fields, pos


def field_edit(text, name, literal):
    fields, closing = object_fields(text)
    if name in fields:
        start, end = fields[name]
        return start, end, literal
    insertion = max((end for _start, end in fields.values()), default=closing)
    addition = (", " if fields else "") + json.dumps(name) + ": " + literal
    return insertion, insertion, addition


def set_field(text, name, literal):
    start, end, replacement = field_edit(text, name, literal)
    return text[:start] + replacement + text[end:]


def prepare(raw, expected_sha):
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError("source differs from reviewed catalog SHA256")
    if len(raw) > 32 * 1024 * 1024:
        raise ValueError("catalog too large")
    text = raw.decode("utf-8")
    before = DECODER.decode(text)
    if not isinstance(before, dict) or not isinstance(before.get("services"), list):
        raise ValueError("catalog services array required")
    services = before["services"]
    names = [row.get("service_name") for row in services if isinstance(row, dict)]
    if len(names) != len(services) or len(set(names)) != len(names):
        raise ValueError("invalid or duplicate service identity")
    matches = [
        i for i, row in enumerate(services) if row.get("service_name") == SERVICE
    ]
    if len(matches) != 1:
        raise ValueError("one exact target required")
    index = matches[0]
    target = services[index]
    if (target.get("repository"), target.get("project_id"), target.get("region")) != (
        "diegomad14/cgm-reconnections-api",
        "cgm-reconnections-prod",
        "us-central1",
    ):
        raise ValueError("target identity mismatch")
    release = target.get("release", {})
    deployment = target.get("deployment")
    if not isinstance(release, dict) or release.get("executor") is not None:
        raise ValueError("release baseline changed")
    if not isinstance(deployment, dict) or deployment.get("executor", "auto") != "auto":
        raise ValueError("deployment baseline changed")
    for other_index, row in enumerate(services):
        if (
            other_index != index
            and str(row.get("repository", "")).casefold()
            == target["repository"].casefold()
        ):
            if (
                not isinstance(row.get("release", {}), dict)
                or row.get("release", {}).get("executor") != "github_actions"
            ):
                raise ValueError(
                    "shared repository release policy requires separate review"
                )
    root_fields, _closing = object_fields(text)
    pos = root_fields["services"][0]
    if text[pos] != "[":
        raise ValueError("services array required")
    pos = skip(text, pos + 1)
    spans = []
    while text[pos] != "]":
        start = pos
        _row, end = DECODER.raw_decode(text, start)
        spans.append((start, end))
        pos = skip(text, end)
        if text[pos] == "]":
            break
        if text[pos] != ",":
            raise ValueError("services separator required")
        pos = skip(text, pos + 1)
    start, end = spans[index]
    original_entry = text[start:end]
    byte_edits = []

    def record_edit(stage, child_start, child, name, literal):
        edit_start, edit_end, replacement = field_edit(child, name, literal)
        byte_edits.append(
            {
                "offset": len(text[:start].encode("utf-8"))
                + len(stage[: child_start + edit_start].encode("utf-8")),
                "old_utf8": child[edit_start:edit_end],
                "new_utf8": replacement,
            }
        )

    fields, _ = object_fields(original_entry)
    dep_start, dep_end = fields["deployment"]
    record_edit(
        original_entry,
        dep_start,
        original_entry[dep_start:dep_end],
        "executor",
        '"github_actions"',
    )
    dep = set_field(original_entry[dep_start:dep_end], "executor", '"github_actions"')
    changed = set_field(original_entry, "deployment", dep)
    fields, _ = object_fields(changed)
    if "release" in fields:
        rel_start, rel_end = fields["release"]
        record_edit(
            changed,
            rel_start,
            changed[rel_start:rel_end],
            "executor",
            '"github_actions"',
        )
        rel = set_field(changed[rel_start:rel_end], "executor", '"github_actions"')
    else:
        rel = '{"executor": "github_actions"}'
        record_edit(changed, 0, changed, "release", rel)
    changed = set_field(changed, "release", rel)
    prefix, suffix = text[:start].encode("utf-8"), text[end:].encode("utf-8")
    candidate = prefix + changed.encode("utf-8") + suffix
    expected = copy.deepcopy(before)
    expected["services"][index].setdefault("release", {})["executor"] = "github_actions"
    expected["services"][index]["deployment"]["executor"] = "github_actions"
    if DECODER.decode(candidate.decode("utf-8")) != expected:
        raise ValueError("unexpected catalog changes")
    if candidate[: len(prefix)] != prefix or (
        suffix and candidate[-len(suffix) :] != suffix
    ):
        raise ValueError("bytes outside target entry changed")
    report = {
        "status": "prepared_not_applied",
        "service_name": SERVICE,
        "source_sha256": expected_sha,
        "candidate_sha256": hashlib.sha256(candidate).hexdigest(),
        "exact_byte_edits": byte_edits,
        "byte_edit_semantics": "Apply in order against intermediate UTF-8 bytes; verify old bytes and final SHA256. No private context is included.",
        "only_policy_changes": {
            "release.executor": [None, "github_actions"],
            "deployment.executor": ["auto", "github_actions"],
        },
        "other_entries_semantically_identical": True,
        "bytes_outside_target_entry_identical": True,
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
    candidate, report = prepare(args.source.read_bytes(), EXPECTED_SHA)
    with args.candidate.open("xb") as destination:
        destination.write(candidate)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
