"""Immutable snapshots, bounded GCS streams and one-use private SQL staging."""

from contextlib import contextmanager, nullcontext
import hashlib
from io import BytesIO
import json
from unittest.mock import Mock

from fastapi import HTTPException
from google.api_core.exceptions import NotFound, PreconditionFailed
from google.cloud import storage
import pytest

from eng_platform_api.config import config
from eng_platform_api.services import database_result_store as results
from eng_platform_api.services.database_registry import DatabaseUnavailable

EXECUTION = "a" * 32
EXPORT = "b" * 32
PATH = f"results/{EXECUTION}/chunk-000000.ndjson"
COLUMNS = [{"key": "c0", "name": "duplicate", "data_type": "text"}]


class MemoryObjects:
    """Test-only generation-aware storage; never selected from runtime flags."""

    def __init__(self):
        self.objects = {}
        self.next_generation = 1
        self.read_paths = []
        self.deleted = []

    def put(self, path, raw, content_type):
        if path in self.objects:
            raise PreconditionFailed("Object exists")
        generation = self.next_generation
        self.next_generation += 1
        self.objects[path] = (bytes(raw), generation, content_type)
        return generation

    def read(self, reference, maximum):
        self.read_paths.append(reference["object"])
        raw, generation, _ = self.objects[reference["object"]]
        if generation != reference["generation"]:
            raise PreconditionFailed("Generation changed")
        return raw[: maximum + 1]

    def delete(self, reference):
        path = reference["object"]
        if path in self.objects and self.objects[path][1] != reference["generation"]:
            raise PreconditionFailed("Generation changed")
        self.deleted.append(path)
        self.objects.pop(path, None)

    @contextmanager
    def export_sink(self, path):
        sink = BytesIO()
        yield sink
        self.put(
            path,
            sink.getvalue(),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def export_reference(self, path):
        raw, generation, _ = self.objects[path]
        return {"object": path, "generation": generation, "bytes": len(raw)}

    def download(self, reference, authorize):
        raw = self.read(reference, results.EXPORT_BYTES)
        for start in range(0, len(raw), results.CHUNK_BYTES):
            authorize()
            yield raw[start : start + results.CHUNK_BYTES]

    def purge(self, identity):
        for path in list(self.objects):
            if any(
                path.startswith(f"{kind}/{identity}/") for kind in ("inputs", "results")
            ):
                self.objects.pop(path)

    def contains(self, identity):
        return any(
            path.startswith((f"inputs/{identity}/", f"results/{identity}/"))
            for path in self.objects
        )


@pytest.fixture
def objects(monkeypatch):
    monkeypatch.setattr(results, "_client", None)
    monkeypatch.setattr(config.databases, "project_id", "")
    monkeypatch.setattr(config.databases, "result_bucket", "")
    backend = MemoryObjects()
    with results.testing_backend(backend):
        yield backend


@pytest.fixture
def native(monkeypatch):
    monkeypatch.setattr(results, "_test_backend", None)
    monkeypatch.setattr(results, "_client", None)
    monkeypatch.setattr(config.databases, "project_id", "database-test-project")
    monkeypatch.setattr(config.databases, "result_bucket", "private-database-results")
    blob = Mock(generation=7, size=3)
    bucket = Mock()
    bucket.blob.return_value = blob
    bucket.get_blob.return_value = blob
    client = Mock()
    client.bucket.return_value = bucket
    constructor = Mock(return_value=client)
    monkeypatch.setattr(storage, "Client", constructor)
    return blob, bucket, constructor


def reference(raw=b"[1]\n", *, path=PATH, generation=7):
    return {
        "object": path,
        "generation": generation,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


@pytest.mark.parametrize("field", ["project_id", "result_bucket"])
@pytest.mark.parametrize(
    "operation",
    [
        "put",
        "read",
        "delete",
        "export",
        "export_reference",
        "download",
        "purge",
        "purge_export",
    ],
)
def test_missing_production_storage_configuration_has_no_local_fallback(
    native, monkeypatch, field, operation
):
    _, _, constructor = native
    monkeypatch.setattr(config.databases, field, "")
    with pytest.raises(DatabaseUnavailable):
        if operation == "put":
            results.put(PATH, b"private")
        elif operation == "read":
            results.read(reference())
        elif operation == "delete":
            results.delete(reference())
        elif operation == "export":
            with results.export_sink(EXECUTION, EXPORT):
                pytest.fail("An unconfigured export sink must not open")
        elif operation == "download":
            list(results.download(reference(), Mock()))
        elif operation == "export_reference":
            results.export_reference(EXECUTION, EXPORT)
        elif operation == "purge_export":
            results.purge_export(EXECUTION, EXPORT)
        else:
            results.purge(EXECUTION)
    constructor.assert_not_called()


@pytest.mark.parametrize(
    "path",
    [
        "results/../file",
        f"results/{EXECUTION}/../file",
        f"results/{EXECUTION}/other/private",
        "results/" + "A" * 32 + "/chunk",
        f"public/{EXECUTION}/chunk",
    ],
)
def test_object_namespace_and_identity_are_validated_before_sdk(native, path):
    _, _, constructor = native
    with pytest.raises(DatabaseUnavailable):
        results.put(path, b"data")
    constructor.assert_not_called()


def test_native_upload_is_create_only_and_returns_hash_generation_and_bytes(native):
    blob, bucket, constructor = native
    raw = b'["precise-decimal"]\n'
    saved = results.put(PATH, raw, "application/x-ndjson")
    assert saved == reference(raw)
    constructor.assert_called_once_with(project="database-test-project")
    bucket.blob.assert_called_once_with(PATH)
    blob.upload_from_string.assert_called_once_with(
        raw,
        content_type="application/x-ndjson",
        if_generation_match=0,
        timeout=4,
        retry=None,
    )


@pytest.mark.parametrize("failure", [None, "unknown-generation"])
def test_upload_with_missing_or_invalid_generation_fails_closed(native, failure):
    blob, _, _ = native
    blob.generation = failure
    with pytest.raises(DatabaseUnavailable):
        results.put(PATH, b"data")


def test_native_read_pins_generation_and_uses_bounded_range(native):
    blob, bucket, _ = native
    raw = b"[1]\n"
    blob.download_as_bytes.return_value = raw
    assert results.read(reference(raw), maximum=32) == raw
    bucket.blob.assert_called_once_with(PATH, generation=7)
    blob.download_as_bytes.assert_called_once_with(
        start=0, end=32, if_generation_match=7, timeout=4, retry=None
    )


@pytest.mark.parametrize("corruption", ["hash", "length", "replaced"])
def test_corrupted_or_replaced_objects_never_return_rows(native, corruption):
    blob, _, _ = native
    raw = b"[1]\n"
    expected = reference(raw)
    if corruption == "hash":
        blob.download_as_bytes.return_value = b"[2]\n"
    elif corruption == "length":
        blob.download_as_bytes.return_value = raw + b"oversized"
    else:
        blob.download_as_bytes.side_effect = PreconditionFailed(
            "private-provider-value"
        )
    with pytest.raises(DatabaseUnavailable) as denied:
        results.read(expected)
    assert "private-provider" not in str(denied.value)


@pytest.mark.parametrize(
    "change",
    [
        {"bytes": -1},
        {"bytes": results.CHUNK_BYTES + 1},
        {"bytes": "4"},
        {"generation": "7"},
    ],
)
def test_invalid_read_bounds_are_rejected_without_remote_io(native, change):
    blob, _, _ = native
    with pytest.raises(DatabaseUnavailable):
        results.read({**reference(), **change})
    blob.download_as_bytes.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [{"bytes": True}, {"generation": True}, {"generation": 0}, {"generation": -1}],
)
def test_read_metadata_requires_real_integer_bounds_and_positive_generation(
    native, change
):
    blob, _, _ = native
    blob.download_as_bytes.return_value = b"p"
    with pytest.raises(DatabaseUnavailable):
        results.read({**reference(b"p"), **change})
    blob.download_as_bytes.assert_not_called()


@pytest.mark.parametrize("field", ["object", "bytes", "generation", "sha256"])
def test_missing_reference_fields_return_generic_unavailability(native, field):
    blob, _, _ = native
    malformed = reference()
    malformed.pop(field)
    with pytest.raises(DatabaseUnavailable):
        results.read(malformed)
    blob.download_as_bytes.assert_not_called()


def test_native_delete_is_generation_scoped_and_missing_object_is_idempotent(native):
    blob, bucket, _ = native
    blob.delete.side_effect = NotFound("already erased")
    results.delete(reference())
    bucket.blob.assert_called_once_with(PATH, generation=7)
    blob.delete.assert_called_once_with(if_generation_match=7, timeout=4, retry=None)


def test_delete_failure_is_generic_and_does_not_retry(native):
    blob, _, _ = native
    blob.delete.side_effect = RuntimeError("DELETE private_values")
    with pytest.raises(DatabaseUnavailable) as denied:
        results.delete(reference())
    assert str(denied.value) == "Database result cleanup is unavailable"
    assert blob.delete.call_count == 1


def test_staged_sql_is_deleted_before_return_and_is_not_replayable(objects):
    statement = "SELECT 1 AS private_alias"
    staged = results.stage_sql(EXECUTION, statement)
    assert staged["object"] == f"inputs/{EXECUTION}/query.json"
    assert results.take_sql(staged) == statement
    assert staged["object"] not in objects.objects
    assert objects.deleted == [staged["object"]]
    with pytest.raises(DatabaseUnavailable):
        results.take_sql(staged)


def test_staged_sql_erase_failure_prevents_dispatch_and_redacts_diagnostics(
    objects, monkeypatch
):
    staged = results.stage_sql(EXECUTION, "SELECT private_column")
    monkeypatch.setattr(
        objects, "delete", Mock(side_effect=RuntimeError("private_column"))
    )
    with pytest.raises(DatabaseUnavailable) as denied:
        results.take_sql(staged)
    assert staged["object"] in objects.objects
    assert "private_column" not in str(denied.value)


@pytest.mark.parametrize(
    "raw",
    [
        b"not-json",
        b"{}",
        b'{"sql":null}',
        b'{"sql":""}',
        json.dumps({"sql": "x" * 30001}).encode(),
    ],
)
def test_corrupt_staged_input_is_not_returned_as_sql(objects, raw):
    staged = results.put(f"inputs/{EXECUTION}/query.json", raw)
    with pytest.raises(DatabaseUnavailable) as denied:
        results.take_sql(staged)
    assert str(denied.value) == "Database input is unavailable"


def test_unicode_encoding_cannot_exceed_staged_request_byte_limit(objects):
    with pytest.raises(HTTPException) as denied:
        results.stage_sql(EXECUTION, "🛰" * 30000)
    assert denied.value.status_code == 413
    assert objects.objects == {}


def test_result_chunks_keep_whole_rows_order_duplicate_headers_and_exact_values(
    objects, monkeypatch
):
    monkeypatch.setattr(results, "CHUNK_BYTES", 256)
    columns = [COLUMNS[0], {"key": "c1", "name": "duplicate", "data_type": "numeric"}]
    data = [
        ["row-" + str(index) + "-" + "é" * 4, "9007199254740993.123456789"]
        for index in range(9)
    ]
    writer = results.ResultWriter(EXECUTION, Mock())
    writer.on_columns(columns)
    writer.on_rows(data[:4])
    writer.on_rows(data[4:])
    # Permit normal manifest metadata size while retaining small row chunks.
    monkeypatch.setattr(results, "CHUNK_BYTES", 1_048_576)
    saved = results.manifest(writer.finish())
    assert saved["columns"] == columns
    assert saved["row_count"] == len(data)
    assert len(saved["chunks"]) > 1
    assert list(results.rows(saved, Mock())) == data
    starts = []
    for chunk in saved["chunks"]:
        raw = results.read(chunk)
        assert raw.endswith(b"\n") and len(raw) <= 256
        assert len(raw.splitlines()) == chunk["row_count"]
        starts.append(chunk["row_start"])
    assert starts[0] == 0 and starts == sorted(starts)


def test_pagination_reads_only_overlapping_chunks_and_can_cross_boundaries(
    objects, monkeypatch
):
    monkeypatch.setattr(results, "CHUNK_BYTES", 10)
    writer = results.ResultWriter(EXECUTION, Mock())
    writer.on_columns(COLUMNS)
    writer.on_rows([[index] for index in range(7)])
    monkeypatch.setattr(results, "CHUNK_BYTES", 1_048_576)
    saved = results.manifest(writer.finish())
    objects.read_paths.clear()
    assert list(results.rows(saved, Mock(), start=3, limit=3)) == [[3], [4], [5]]
    expected = [
        chunk["object"]
        for chunk in saved["chunks"]
        if chunk["row_start"] < 6 and chunk["row_start"] + chunk["row_count"] > 3
    ]
    assert objects.read_paths == expected
    objects.read_paths.clear()
    assert list(results.rows(saved, Mock(), start=0, limit=0)) == []
    assert objects.read_paths == []
    assert list(results.rows(saved, Mock(), start=6)) == [[6]]


def test_snapshot_limit_rejects_extra_row_instead_of_truncating(objects):
    assert results.SNAPSHOT_BYTES == 256 * 1024 * 1024
    writer = results.ResultWriter(EXECUTION, Mock())
    writer.on_columns(COLUMNS)
    row = ["within-budget"]
    row_bytes = len(results._encode(row)) + 1
    writer.byte_count = results.SNAPSHOT_BYTES - row_bytes
    writer.on_rows([row])
    assert writer.byte_count == results.SNAPSHOT_BYTES
    with pytest.raises(HTTPException) as denied:
        writer.on_rows([["over-budget-private-value"]])
    assert denied.value.status_code == 422
    assert writer.row_count == 1
    assert "over-budget-private-value" not in denied.value.detail
    assert not any(path.endswith("manifest.json") for path in objects.objects)


@pytest.mark.parametrize("reason", ["cell", "row"])
def test_cell_and_row_resource_budgets_abort_without_partial_row(objects, reason):
    writer = results.ResultWriter(EXECUTION, Mock())
    if reason == "cell":
        writer.on_columns(COLUMNS)
        row = ["x" * results.CELL_BYTES]
    else:
        writer.on_columns([{**COLUMNS[0], "key": str(index)} for index in range(5)])
        row = ["x" * (results.CELL_BYTES - 2)] * 5
    with pytest.raises(HTTPException) as denied:
        writer.on_rows([row])
    assert denied.value.status_code == 422
    assert writer.row_count == 0 and writer.buffer == b""
    assert objects.objects == {}


@pytest.mark.parametrize(
    "columns",
    [
        [],
        [COLUMNS[0]] * 101,
        [{"key": "c0", "name": "x"}],
        [{"key": "c0", "name": 1, "data_type": "text"}],
    ],
)
def test_result_column_shape_fails_closed(objects, columns):
    with pytest.raises(DatabaseUnavailable):
        results.ResultWriter(EXECUTION, Mock()).on_columns(columns)


@pytest.mark.parametrize("row", [(1,), [1, 2]])
def test_row_shape_does_not_produce_partial_snapshot(objects, row):
    writer = results.ResultWriter(EXECUTION, Mock())
    writer.on_columns(COLUMNS)
    with pytest.raises(DatabaseUnavailable):
        writer.on_rows([row])
    assert writer.row_count == 0 and objects.objects == {}


def test_revocation_before_manifest_publication_prevents_result_visibility(objects):
    authorize = Mock(
        side_effect=[None, None, HTTPException(403, "Database permission changed")]
    )
    writer = results.ResultWriter(EXECUTION, authorize)
    writer.on_columns(COLUMNS)
    writer.on_rows([[1]])
    with pytest.raises(HTTPException) as denied:
        writer.finish()
    assert denied.value.status_code == 403
    assert any(path.endswith("ndjson") for path in objects.objects)
    assert not any(path.endswith("manifest.json") for path in objects.objects)


def test_manifest_size_limit_prevents_publication(objects, monkeypatch):
    monkeypatch.setattr(results, "CHUNK_BYTES", 16)
    writer = results.ResultWriter(EXECUTION, Mock())
    writer.on_columns(COLUMNS)
    with pytest.raises(DatabaseUnavailable):
        writer.finish()
    assert objects.objects == {}


@pytest.mark.parametrize(
    "change",
    [
        {"bytes": results.SNAPSHOT_BYTES + 1},
        {"row_count": -1},
        {"row_count": 2},
        {"chunks": [{"row_start": 1, "row_count": 1}]},
        {"chunks": [{"row_start": 0, "row_count": 0}]},
        {"chunks": [{"row_start": 0, "row_count": 1}] * 513},
    ],
)
def test_manifest_order_and_totals_cannot_describe_incomplete_result(objects, change):
    value = {
        "execution_id": EXECUTION,
        "columns": COLUMNS,
        "chunks": [],
        "row_count": 0,
        "bytes": len(results._encode(COLUMNS)),
    }
    saved = results.put(
        f"results/{EXECUTION}/manifest.json", results._encode({**value, **change})
    )
    with pytest.raises(DatabaseUnavailable) as denied:
        results.manifest(saved)
    assert str(denied.value) == "Database result manifest is unavailable"


@pytest.mark.parametrize(
    "change",
    [
        {"execution_id": "b" * 32},
        {"execution_id": "../other"},
        {"columns": []},
        {"columns": [{"key": "0", "name": 1, "data_type": "text"}]},
        {"columns": [{"key": "0", "name": "x", "data_type": "text", "sql": "private"}]},
        {"columns": [COLUMNS[0]] * 101},
        {"row_count": False},
        {"row_count": 0.0},
        {"bytes": True},
        {"bytes": 0.5},
    ],
)
def test_manifest_identity_column_schema_and_integer_totals_are_validated(
    objects, change
):
    value = {
        "execution_id": EXECUTION,
        "columns": COLUMNS,
        "chunks": [],
        "row_count": 0,
        "bytes": len(results._encode(COLUMNS)),
    }
    saved = results.put(
        f"results/{EXECUTION}/manifest.json", results._encode({**value, **change})
    )
    with pytest.raises(DatabaseUnavailable) as denied:
        results.manifest(saved)
    assert "private" not in str(denied.value)


@pytest.mark.parametrize(
    "change",
    [
        {"object": f"results/{'b' * 32}/chunk-000000.ndjson"},
        {"object": f"inputs/{EXECUTION}/query.json"},
        {"object": f"results/{EXECUTION}/export-{EXPORT}.xlsx"},
        {"bytes": results.CHUNK_BYTES + 1},
        {"bytes": False},
        {"generation": 0},
        {"generation": True},
        {"sha256": "wrong"},
        {"row_count": True},
        {"row_start": False},
    ],
)
def test_manifest_chunk_references_cannot_cross_snapshot_or_resource_bounds(
    objects, change
):
    chunk = {**reference(), "row_start": 0, "row_count": 1, **change}
    value = {
        "execution_id": EXECUTION,
        "columns": COLUMNS,
        "chunks": [chunk],
        "row_count": 1,
        "bytes": len(results._encode(COLUMNS)) + reference()["bytes"],
    }
    saved = results.put(f"results/{EXECUTION}/manifest.json", results._encode(value))
    with pytest.raises(DatabaseUnavailable):
        results.manifest(saved)


@pytest.mark.parametrize("data", [[], [[1], [2]]])
def test_valid_empty_and_nonempty_manifests_have_exact_byte_and_row_totals(
    objects, data
):
    writer = results.ResultWriter(EXECUTION, Mock())
    writer.on_columns(COLUMNS)
    writer.on_rows(data)
    value = results.manifest(writer.finish())
    assert value["row_count"] == len(data)
    assert value["bytes"] == len(results._encode(COLUMNS)) + sum(
        chunk["bytes"] for chunk in value["chunks"]
    )
    assert list(results.rows(value, Mock())) == data


def test_inconsistent_chunk_row_count_is_rejected_before_any_rows_return(objects):
    saved = results.put(PATH, b"[1]\n")
    saved.update(row_start=0, row_count=2)
    with pytest.raises(DatabaseUnavailable):
        list(results.rows({"row_count": 2, "chunks": [saved]}, Mock()))


def test_native_export_sink_is_generation_create_only_and_uses_bounded_blob_writer(
    native,
):
    blob, bucket, _ = native
    sink = BytesIO()
    blob.open.return_value = nullcontext(sink)
    with results.export_sink(EXECUTION, EXPORT) as target:
        target.write(b"PK-workbook")
    assert sink.getvalue() == b"PK-workbook"
    bucket.blob.assert_called_once_with(f"results/{EXECUTION}/export-{EXPORT}.xlsx")
    blob.open.assert_called_once_with(
        "wb",
        chunk_size=8_388_608,
        ignore_flush=True,
        if_generation_match=0,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        timeout=4,
        retry=None,
    )


def test_export_writer_preserves_application_denial_but_redacts_provider_error(native):
    blob, _, _ = native
    blob.open.return_value = nullcontext(BytesIO())
    with pytest.raises(HTTPException) as denied:
        with results.export_sink(EXECUTION, EXPORT):
            raise HTTPException(403, "Database permission changed")
    assert denied.value.status_code == 403
    blob.open.side_effect = RuntimeError("SELECT private_value")
    with pytest.raises(DatabaseUnavailable) as failed:
        with results.export_sink(EXECUTION, EXPORT):
            pytest.fail("Failed provider must not open sink")
    assert str(failed.value) == "Database export is unavailable"


@pytest.mark.parametrize("size", [None, 0, results.EXPORT_BYTES + 1])
def test_native_export_metadata_cannot_publish_empty_or_oversized_file(native, size):
    blob, _, _ = native
    blob.size = size
    with pytest.raises(DatabaseUnavailable):
        results.export_reference(EXECUTION, EXPORT)


def test_native_export_reference_is_exact_generation_and_size(native):
    _, bucket, _ = native
    assert results.export_reference(EXECUTION, EXPORT) == {
        "object": f"results/{EXECUTION}/export-{EXPORT}.xlsx",
        "generation": 7,
        "bytes": 3,
    }
    bucket.get_blob.assert_called_once_with(
        f"results/{EXECUTION}/export-{EXPORT}.xlsx", timeout=4, retry=None
    )


def test_export_at_exact_256_mib_budget_can_publish_without_materializing_file(native):
    blob, _, _ = native
    assert results.EXPORT_BYTES == 256 * 1024 * 1024
    blob.size = results.EXPORT_BYTES
    assert results.export_reference(EXECUTION, EXPORT)["bytes"] == results.EXPORT_BYTES


def test_native_download_never_requests_more_than_chunk_or_remaining_bytes(native):
    blob, bucket, _ = native
    raw = b"x" * (results.CHUNK_BYTES + 3)
    source = Mock()
    source.read.side_effect = [raw[: results.CHUNK_BYTES], raw[results.CHUNK_BYTES :]]
    blob.open.return_value = nullcontext(source)
    saved = reference(raw, path=f"results/{EXECUTION}/export-{EXPORT}.xlsx")
    authorize = Mock()
    assert b"".join(results.download(saved, authorize)) == raw
    assert [call.args[0] for call in source.read.call_args_list] == [
        results.CHUNK_BYTES,
        3,
    ]
    bucket.blob.assert_called_once_with(saved["object"], generation=7)
    blob.open.assert_called_once_with(
        "rb",
        chunk_size=results.CHUNK_BYTES,
        if_generation_match=7,
        timeout=4,
        retry=None,
    )
    assert authorize.call_count == 6


def test_native_download_incomplete_file_fails_explicitly(native):
    blob, _, _ = native
    source = Mock(read=Mock(side_effect=[b"x", b""]))
    blob.open.return_value = nullcontext(source)
    with pytest.raises(DatabaseUnavailable) as denied:
        list(results.download(reference(b"xxx"), Mock()))
    assert str(denied.value) == "Database export is unavailable"


def test_native_download_revocation_stops_before_next_chunk(native):
    blob, _, _ = native
    source = Mock(read=Mock(return_value=b"data"))
    blob.open.return_value = nullcontext(source)
    authorize = Mock(
        side_effect=[None, HTTPException(403, "Database permission changed")]
    )
    with pytest.raises(HTTPException) as denied:
        list(results.download(reference(b"data"), authorize))
    assert denied.value.status_code == 403
    source.read.assert_not_called()


@pytest.mark.parametrize("size", [0, -1, results.EXPORT_BYTES + 1])
def test_download_budget_is_checked_before_authority_or_sdk(native, size):
    blob, _, _ = native
    authorize = Mock()
    with pytest.raises(DatabaseUnavailable):
        list(results.download({**reference(), "bytes": size}, authorize))
    authorize.assert_not_called()
    blob.open.assert_not_called()


def test_native_purge_lists_only_execution_prefixes_and_deletes_pinned_generations(
    native,
):
    blob, bucket, _ = native
    bucket.list_blobs.side_effect = [[blob], [], [], []]
    results.purge(EXECUTION)
    assert [call.kwargs for call in bucket.list_blobs.call_args_list] == [
        {
            "prefix": f"{prefix}/{EXECUTION}/",
            "max_results": 600,
            "timeout": 4,
            "retry": None,
        }
        for prefix in ("inputs", "results")
    ] + [
        {
            "prefix": f"{prefix}/{EXECUTION}/",
            "max_results": 1,
            "timeout": 4,
            "retry": None,
        }
        for prefix in ("inputs", "results")
    ]
    blob.delete.assert_called_once_with(if_generation_match=7, timeout=4, retry=None)


def test_failed_export_cleanup_targets_only_its_exact_generation(native):
    blob, bucket, _ = native
    results.purge_export(EXECUTION, EXPORT)
    bucket.get_blob.assert_called_once_with(
        f"results/{EXECUTION}/export-{EXPORT}.xlsx", timeout=4, retry=None
    )
    blob.delete.assert_called_once_with(if_generation_match=7, timeout=4, retry=None)
    bucket.list_blobs.assert_not_called()


def test_failed_export_cleanup_is_idempotent_when_object_is_absent(native):
    blob, bucket, _ = native
    bucket.get_blob.return_value = None
    results.purge_export(EXECUTION, EXPORT)
    blob.delete.assert_not_called()


def test_failed_export_cleanup_provider_failure_is_generic(native):
    _, bucket, _ = native
    bucket.get_blob.side_effect = RuntimeError("private-query-export")
    with pytest.raises(DatabaseUnavailable) as failed:
        results.purge_export(EXECUTION, EXPORT)
    assert str(failed.value) == "Database export cleanup is unavailable"


def test_failed_export_cleanup_preserves_result_snapshot_and_other_exports(objects):
    snapshot = results.put(PATH, b"[1]\n")
    for identity in (EXPORT, "c" * 32):
        with results.export_sink(EXECUTION, identity) as sink:
            sink.write(b"PK-test-only")
    results.purge_export(EXECUTION, EXPORT)
    assert results.read(snapshot) == b"[1]\n"
    assert f"results/{EXECUTION}/export-{EXPORT}.xlsx" not in objects.objects
    assert f"results/{EXECUTION}/export-{'c' * 32}.xlsx" in objects.objects
    results.purge_export(EXECUTION, EXPORT)


def test_explicit_object_adapter_can_export_download_purge_and_restore_scope(objects):
    staged = results.stage_sql(EXECUTION, "SELECT 1")
    with results.export_sink(EXECUTION, EXPORT) as sink:
        sink.write(b"PK-test-only")
    saved = results.export_reference(EXECUTION, EXPORT)
    assert b"".join(results.download(saved, Mock())) == b"PK-test-only"
    other = MemoryObjects()
    with results.testing_backend(other):
        with pytest.raises(DatabaseUnavailable):
            results.read(staged)
    assert results.take_sql(staged) == "SELECT 1"
    results.purge(EXECUTION)
    assert objects.objects == {}


def test_native_download_rechecks_revocation_after_provider_read_before_yield(native):
    blob, _, _ = native
    revoked = False

    def read(size):
        nonlocal revoked
        revoked = True
        return b"data"

    def authorize():
        if revoked:
            raise HTTPException(403, "Database permission changed")

    blob.open.return_value = nullcontext(Mock(read=read))
    output = results.download(reference(b"data"), authorize)
    with pytest.raises(HTTPException):
        next(output)


def test_purge_checks_complete_namespace_after_deleting_listed_objects(native):
    blob, bucket, _ = native
    bucket.list_blobs.side_effect = [[], [], [blob]]
    with pytest.raises(DatabaseUnavailable):
        results.purge(EXECUTION)


def test_native_partial_purge_obeys_deadline_and_retry_finishes(native, monkeypatch):
    _, bucket, _ = native
    clock = [0.0]
    remaining = [Mock(generation=index) for index in (1, 2, 3)]

    def remove(blob):
        remaining.remove(blob)
        clock[0] += 120

    for blob in remaining:
        blob.delete.side_effect = (
            lambda *, if_generation_match, timeout, retry, blob=blob: remove(blob)
        )

    def listed(*, prefix, max_results, timeout, retry):
        return list(remaining) if prefix.startswith("results/") else []

    bucket.list_blobs.side_effect = listed
    monkeypatch.setattr(results.time, "monotonic", lambda: clock[0])
    with pytest.raises(DatabaseUnavailable):
        results.purge(EXECUTION, deadline=230)
    assert len(remaining) == 1
    results.purge(EXECUTION, deadline=clock[0] + 230)
    assert not remaining
