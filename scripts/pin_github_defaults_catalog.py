"""Prepare the reviewed GitHub defaults catalog offline, without production writes."""

import argparse
import copy
import hashlib
import json
from pathlib import Path

from scripts import pin_artemis_release_catalog as artemis
from scripts import pin_reconnections_catalog as byte_json

EXPECTED_SHA = "fcfe13a685cd607004b6f132d476b66f550cf8b8b72ae22d0825375abb9a1c17"
SERVICE_REPOSITORIES = {
    **dict.fromkeys(artemis.SERVICES, "diegomad14/cgm-artemis-api"),
    "cgm-artemis-web": "diegomad14/cgm-artemis-web",
    "cgm-sanplat-api": "diegomad14/cgm-sanplat-api",
    "cgm-sanplat-web": "diegomad14/cgm-sanplat-web",
    "cgm-bot-api": "diegomad14/cgm-bot-core",
    "communications-ms": "diegomad14/communications-ms",
    "eng-platform-api": "diegomad14/gcp-engineering-platform-api",
    "eng-platform-web": "diegomad14/gcp-engineering-platform-web",
    "cgm-reconnections-api": "diegomad14/cgm-reconnections-api",
}
EXISTING_GITHUB_RELEASES = artemis.SERVICES | {"cgm-reconnections-api"}
EXISTING_GITHUB_DEPLOYMENTS = frozenset(
    {
        "cgm-artemis-api",
        "cgm-artemis-web",
        "eng-platform-api",
        "eng-platform-web",
        "cgm-reconnections-api",
    }
)


def baseline(row, field):
    value = row.get(field, {})
    if not isinstance(value, dict):
        raise ValueError("invalid executor block")
    return value.get("executor", "auto" if field == "deployment" else None)


def executor_edit(text, start, end, field, service_name):
    entry = text[start:end]
    fields, _ = byte_json.object_fields(entry)
    if field in fields:
        child_start, child_end = fields[field]
        edit_start, edit_end, replacement = byte_json.field_edit(
            entry[child_start:child_end], "executor", '"github_actions"'
        )
        edit_start += child_start
        edit_end += child_start
    else:
        edit_start, edit_end, replacement = byte_json.field_edit(
            entry, field, '{"executor": "github_actions"}'
        )
    return {
        "service_name": service_name,
        "field": field + ".executor",
        "offset": len(text[: start + edit_start].encode("utf-8")),
        "old_utf8": entry[edit_start:edit_end],
        "new_utf8": replacement,
    }


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
        or len(names) != len(set(names))
    ):
        raise ValueError("invalid or duplicate service identity")
    indices = {
        i for i, row in enumerate(rows) if row["service_name"] in SERVICE_REPOSITORIES
    }
    if {rows[i]["service_name"] for i in indices} != set(SERVICE_REPOSITORIES):
        raise ValueError("exact twenty managed services required")
    for index, row in enumerate(rows):
        deployment = row.get("deployment", {})
        if not isinstance(deployment, dict):
            raise ValueError("invalid deployment block")
        if index not in indices:
            if (
                row.get("management_mode", "managed") == "managed"
                and deployment.get("enabled", True) is not False
            ):
                raise ValueError("unreviewed enabled managed service")
            continue
        name = row["service_name"]
        if row.get("repository") != SERVICE_REPOSITORIES[name]:
            raise ValueError("managed repository identity mismatch")
        if (
            row.get("management_mode") != "managed"
            or deployment.get("enabled") is not True
        ):
            raise ValueError("reviewed managed service must remain enabled")
        release = (
            "github_actions"
            if name in EXISTING_GITHUB_RELEASES
            else "cloud_build"
            if name == "cgm-artemis-web"
            else None
        )
        deploy = (
            "github_actions"
            if name in EXISTING_GITHUB_DEPLOYMENTS
            else "cloud_build"
            if name in artemis.SERVICES
            else "auto"
        )
        if baseline(row, "release") != release or baseline(row, "deployment") != deploy:
            raise ValueError("reviewed executor baseline changed")
    spans = artemis.entry_spans(text)
    edits = []
    expected = copy.deepcopy(before)
    for index in indices:
        row = rows[index]
        for field in ("release", "deployment"):
            if baseline(row, field) == "github_actions":
                continue
            edits.append(executor_edit(text, *spans[index], field, row["service_name"]))
            expected["services"][index].setdefault(field, {})["executor"] = (
                "github_actions"
            )
    counts = {
        field: sum(edit["field"] == field + ".executor" for edit in edits)
        for field in ("release", "deployment")
    }
    if counts != {"release": 7, "deployment": 15}:
        raise ValueError("exact reviewed twenty two edits required")
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
    after_text = candidate.decode("utf-8")
    if byte_json.DECODER.decode(after_text) != expected:
        raise ValueError("unexpected catalog changes")
    if artemis.outside_entries(text, spans, indices) != artemis.outside_entries(
        after_text, artemis.entry_spans(after_text), indices
    ):
        raise ValueError("bytes outside managed targets changed")
    return candidate, {
        "status": "prepared_not_applied",
        "source_sha256": EXPECTED_SHA,
        "candidate_sha256": hashlib.sha256(candidate).hexdigest(),
        "service_names": sorted(SERVICE_REPOSITORIES),
        "exact_byte_edits": edits,
        "edit_counts": counts,
        "byte_edit_semantics": "Apply in order against intermediate UTF-8 bytes; verify old bytes and final SHA256.",
        "only_policy_changes": ["release.executor", "deployment.executor"],
        "other_entries_semantically_identical": True,
        "bytes_outside_target_entries_identical": True,
        "existing_github_pins_unchanged": True,
        "disabled_and_observation_entries_unchanged": True,
        "network_or_production_action": False,
    }


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
