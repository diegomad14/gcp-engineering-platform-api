"""Exact comparator and bounded multi-pass sorting over private fake objects."""

from decimal import Decimal
import json
from time import monotonic

from fastapi import HTTPException
import pytest

from eng_platform_api.services import database_sort as sorting
from eng_platform_api.services.database_console import DatabaseResourceExceeded
from eng_platform_api.services.database_registry import DatabaseUnavailable


class Runs:
    def __init__(self, *, fragment=1_048_576):
        self.objects = {}
        self.events = []
        self.fragment = fragment
        self.batches = []
        self.authorizations = 0
        self.open_readers = 0
        self.peak_readers = 0

    def authorize(self):
        self.authorizations += 1

    def write(self, name, lines):
        assert name not in self.objects
        raw = b"".join(lines)
        self.objects[name] = raw
        self.events.append(("write", name))
        return {"object": name, "bytes": len(raw)}

    def read(self, reference):
        self.open_readers += 1
        self.peak_readers = max(self.open_readers, self.peak_readers)
        try:
            raw = self.objects[reference["object"]]
            for offset in range(0, len(raw), self.fragment):
                yield raw[offset : offset + self.fragment]
        finally:
            self.open_readers -= 1

    def delete(self, reference):
        name = reference["object"]
        self.events.append(("delete", name))
        del self.objects[name]

    def emit(self, batch):
        assert 1 <= len(batch) <= 16
        self.batches.append(batch)

    @property
    def output(self):
        return [row for batch in self.batches for row in batch]

    def sort(self, values, data_type="text", direction="asc", **kwargs):
        columns = kwargs.pop(
            "columns",
            [
                {"key": "c0", "name": "value", "data_type": data_type},
                {"key": "c1", "name": "value", "data_type": "int4"},
            ],
        )
        rows = kwargs.pop("rows", iter([[value, i] for i, value in enumerate(values)]))
        arguments = {
            "authorize": self.authorize,
            "write_run": self.write,
            "read_run": self.read,
            "delete_run": self.delete,
            "on_rows": self.emit,
            "deadline": monotonic() + 60,
        }
        arguments.update(kwargs)
        return sorting.sort_rows(columns, rows, "c0", direction, **arguments)


@pytest.mark.parametrize(
    "data_type", ["int2", "int4", "int8", "numeric", "float4", "float8"]
)
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_numeric_order_is_exact_and_null_last(data_type, direction):
    values = [
        "9007199254740993",
        None,
        "9007199254740992",
        "0.1000000000000000000000000000000000000002",
        "0.1000000000000000000000000000000000000001",
        "-2",
        "-10",
        0,
        "1e200",
        "-1e200",
        None,
    ]
    expected = sorted(
        [[value, i] for i, value in enumerate(values) if value is not None],
        key=lambda row: Decimal(row[0]),
        reverse=direction == "desc",
    ) + [[None, 1], [None, 10]]
    runs = Runs(fragment=17)
    result = runs.sort(values, data_type, direction)
    assert runs.output == expected
    assert result["row_count"] == len(values)
    assert result["memory_peak_bytes"] <= 32 * 1_048_576
    assert not runs.objects


@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_numeric_equal_lexemes_and_nulls_preserve_original_ordinal(direction):
    values = ["2.00", "1.0", "2", "1", None, "2e0", None]
    runs = Runs()
    runs.sort(values, "numeric", direction)
    expected_indices = [1, 3, 0, 2, 5, 4, 6]
    if direction == "desc":
        expected_indices = [0, 2, 5, 1, 3, 4, 6]
    assert [row[1] for row in runs.output] == expected_indices


@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_numeric_special_values_are_deterministic(direction):
    values = ["NaN", "Infinity", "-Infinity", "2", "NaN", None]
    runs = Runs()
    runs.sort(values, "float8", direction)
    indices = [2, 3, 1, 0, 4, 5] if direction == "asc" else [0, 4, 1, 3, 2, 5]
    assert [row[1] for row in runs.output] == indices


@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_boolean_and_unicode_order(direction):
    runs = Runs()
    runs.sort([True, False, True, None, False], "bool", direction)
    indices = [1, 4, 0, 2, 3] if direction == "asc" else [0, 2, 1, 4, 3]
    assert [row[1] for row in runs.output] == indices
    values = ["é", "e\u0301", "🙂", "Z", "a", "", None, "a"]
    runs = Runs()
    runs.sort(values, "text", direction)
    expected = sorted(
        [[value, i] for i, value in enumerate(values) if value is not None],
        key=lambda row: row[0],
        reverse=direction == "desc",
    ) + [[None, 6]]
    assert runs.output == expected


@pytest.mark.parametrize(
    "data_type", ["json", "jsonb", "_numeric", "numeric[]", "uuid"]
)
def test_document_array_and_unknown_types_compare_exact_lexical_strings(data_type):
    values = ['{"v": 2}', '{"v": 10}', "null", "[2]", "[10]", None]
    runs = Runs()
    runs.sort(values, data_type)
    assert [row[0] for row in runs.output] == sorted(values[:-1]) + [None]


@pytest.mark.parametrize(
    ("data_type", "values", "indices"),
    [
        ("date", ["2026-01-01", "0001-01-01", "2025-12-31"], [1, 2, 0]),
        (
            "time",
            ["01:00:00.000002", "01:00:00.000001", "00:59:59.999999"],
            [2, 1, 0],
        ),
        (
            "timestamp",
            [
                "9999-12-31 23:59:59.000002",
                "9999-12-31 23:59:59.000001",
                "2026-01-01T00:00:00",
            ],
            [2, 1, 0],
        ),
        (
            "timestamptz",
            [
                "2026-01-01T01:00:00.000002+01:00",
                "2026-01-01T00:00:00.000001Z",
                "2025-12-31T19:00:00.000001-05:00",
                "2026-01-01T00:00:00.000003+00",
            ],
            [1, 2, 0, 3],
        ),
        (
            "timetz",
            ["01:00:00.000002+01:00", "00:00:00.000001Z", "00:00:00.000003Z"],
            [1, 0, 2],
        ),
    ],
)
def test_temporal_microseconds_and_offset_normalization(data_type, values, indices):
    runs = Runs()
    runs.sort(values, data_type)
    assert [row[1] for row in runs.output] == indices


@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_temporal_infinite_values_and_null_order(direction):
    values = ["infinity", "2026-01-01", "-infinity", None]
    runs = Runs()
    runs.sort(values, "date", direction)
    indices = [2, 1, 0, 3] if direction == "asc" else [0, 1, 2, 3]
    assert [row[1] for row in runs.output] == indices


@pytest.mark.parametrize(
    ("data_type", "value"),
    [
        ("date", "invalid"),
        ("date", "0000-01-01"),
        ("date", "2025-02-29"),
        ("date", "0002-02-29 BC"),
        ("time", "24:00:00.000001"),
        ("timestamp", "2026-01-01T00:00:00Z"),
        ("timestamptz", "2026-01-01T00:00:00"),
        ("time", "12:00:00.0000001"),
        ("time", "12:00:00+00:99"),
        ("date", 1),
    ],
)
def test_invalid_temporal_forms_fail_explicitly(data_type, value):
    runs = Runs()
    with pytest.raises(DatabaseResourceExceeded, match="temporal"):
        runs.sort([value], data_type)
    assert not runs.output


def test_extended_postgres_dates_and_bc_have_natural_chronological_order():
    values = [
        "10000-01-01",
        "9999-12-31",
        "0001-01-01",
        "0001-12-31 BC",
        "0001-02-29 BC",
        "0002-12-31 BC",
        "5874897-12-31",
        "4713-01-01 BC",
    ]
    runs = Runs()
    runs.sort(values, "date")
    assert [row[1] for row in runs.output] == [7, 5, 4, 3, 2, 1, 0, 6]


def test_postgres_24_hour_and_extended_timestamp_are_exact():
    runs = Runs()
    runs.sort(["24:00:00", "23:59:59.999999", "00:00:00"], "time")
    assert [row[1] for row in runs.output] == [2, 1, 0]
    values = [
        "10000-01-01T00:00:00.000001",
        "9999-12-31T24:00:00",
        "10000-01-01T00:00:00",
        "0001-12-31T23:59:59.999999 BC",
        "0001-01-01T00:00:00",
    ]
    runs = Runs()
    runs.sort(values, "timestamp")
    assert [row[1] for row in runs.output] == [3, 4, 1, 2, 0]


def test_unknown_numeric_primitive_uses_preserved_textual_lexeme():
    runs = Runs()
    runs.sort([2, 10, 1, None], "oid")
    assert runs.output == [[1, 2], [10, 1], [2, 0], [None, 3]]


@pytest.mark.parametrize(
    ("data_type", "values"),
    [
        ("numeric(30, 4)", ["2", "10"]),
        ("integer", ["2", "10"]),
        ("double precision", ["2", "10"]),
        ("boolean", [False, True]),
        (
            "timestamp(6) with time zone",
            ["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"],
        ),
    ],
)
def test_standard_postgres_type_aliases(data_type, values):
    runs = Runs()
    runs.sort(list(reversed(values)), data_type)
    assert [row[0] for row in runs.output] == values


@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_600_rows_multiple_merge_passes_are_stable_bounded_and_physically_deleted(
    monkeypatch, direction
):
    monkeypatch.setattr(sorting, "RUN_MEMORY_BYTES", 4096)
    values = [None if i % 23 == 0 else str((599 - i) % 71) for i in range(600)]
    runs = Runs(fragment=41)
    result = runs.sort(values, "int8", direction)
    expected = sorted(
        [[value, i] for i, value in enumerate(values) if value is not None],
        key=lambda row: Decimal(row[0]),
        reverse=direction == "desc",
    ) + [[value, i] for i, value in enumerate(values) if value is None]
    assert runs.output == expected
    assert result["row_count"] == 600
    assert result["merge_passes"] >= 2
    assert result["run_count"] > 8
    assert result["memory_peak_bytes"] <= 32 * 1_048_576
    assert runs.peak_readers <= 8
    assert runs.open_readers == 0
    assert not runs.objects
    writes = [name for kind, name in runs.events if kind == "write"]
    deletes = [name for kind, name in runs.events if kind == "delete"]
    assert len(writes) == len(deletes) == len(set(writes))
    # The first initial run is erased only after an eighth-input replacement.
    first_delete = next(
        i for i, event in enumerate(runs.events) if event[0] == "delete"
    )
    assert sum(kind == "write" for kind, _ in runs.events[:first_delete]) == 9


def test_empty_result_authorizes_and_writes_nothing():
    runs = Runs()
    result = runs.sort([])
    assert result["row_count"] == result["run_count"] == 0
    assert runs.authorizations >= 2
    assert not runs.events
    assert not runs.batches


def test_reused_source_list_is_copied_before_next_iteration():
    row = ["", 0]

    def rows():
        for i, value in enumerate(["c", "a", "b"]):
            row[:] = [value, i]
            yield row

    runs = Runs()
    runs.sort([], rows=rows())
    assert runs.output == [["a", 1], ["b", 2], ["c", 0]]


@pytest.mark.parametrize("value", [1.1, {"v": "secret"}, ["secret"]])
def test_unsupported_source_values_are_redacted(value):
    with pytest.raises(DatabaseResourceExceeded, match="Unsupported") as error:
        Runs().sort([value], "numeric")
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    ("data_type", "value"), [("numeric", "secret"), ("bool", "secret")]
)
def test_invalid_selected_value_is_explicitly_unsupported(data_type, value):
    with pytest.raises(DatabaseResourceExceeded) as error:
        Runs().sort([value], data_type)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("budget", ["cell", "row", "columns", "shape", "memory"])
def test_explicit_resource_guards(monkeypatch, budget):
    runs = Runs()
    kwargs = {}
    values = ["a"]
    if budget == "cell":
        values = ["x" * sorting.MAX_CELL_BYTES]
    elif budget == "row":
        kwargs["columns"] = [
            {"key": f"c{i}", "name": "value", "data_type": "text"} for i in range(5)
        ]
        kwargs["rows"] = iter([["x" * (sorting.MAX_CELL_BYTES - 2)] * 5])
    elif budget == "columns":
        kwargs["columns"] = [
            {"key": f"c{i}", "name": "value", "data_type": "text"} for i in range(101)
        ]
    elif budget == "shape":
        kwargs["rows"] = iter([["a"]])
    else:
        monkeypatch.setattr(sorting, "MEMORY_BYTES", 1024)
    with pytest.raises(DatabaseUnavailable):
        runs.sort(values, **kwargs)
    assert not runs.output


def test_worst_allowed_cell_row_and_100_columns_preserve_full_values():
    columns = [
        {"key": f"c{i}", "name": "duplicate", "data_type": "text"} for i in range(100)
    ]
    row = ["x" * (sorting.MAX_CELL_BYTES - 2)] * 3 + [None] * 97
    runs = Runs(fragment=13_000)
    result = runs.sort([], columns=columns, rows=iter([row]))
    assert runs.output == [row]
    assert result["row_count"] == 1


@pytest.mark.parametrize("direction", ["ASC", "invalid", ""])
def test_invalid_direction_is_rejected(direction):
    with pytest.raises(DatabaseResourceExceeded):
        Runs().sort(["a"], direction=direction)


@pytest.mark.parametrize("deadline", [float("inf"), float("nan"), -1])
def test_invalid_or_elapsed_deadline_writes_nothing(deadline):
    runs = Runs()
    with pytest.raises(DatabaseResourceExceeded):
        runs.sort(["a"], deadline=deadline)
    assert not runs.events


def test_deadline_is_checked_during_large_source_drain(monkeypatch):
    now = [0]
    monkeypatch.setattr(sorting, "monotonic", lambda: now[0])

    def rows():
        for i in range(20):
            now[0] = i
            yield [str(i), i]

    runs = Runs()
    with pytest.raises(DatabaseResourceExceeded, match="time budget"):
        runs.sort([], rows=rows(), deadline=5)
    assert not runs.output


def test_cancel_during_run_write_leaves_runs_for_caller_purge(monkeypatch):
    monkeypatch.setattr(sorting, "RUN_MEMORY_BYTES", 4096)
    runs = Runs()

    def authorize():
        if len(runs.objects) >= 2:
            raise HTTPException(409, "Execution cancelled")

    with pytest.raises(HTTPException) as error:
        runs.sort([str(i) for i in range(30)], "int8", authorize=authorize)
    assert error.value.status_code == 409
    assert len(runs.objects) == 2
    assert not runs.output
    assert not any(kind == "delete" for kind, _ in runs.events)


def test_failed_replacement_does_not_delete_consumed_inputs(monkeypatch):
    monkeypatch.setattr(sorting, "RUN_MEMORY_BYTES", 4096)
    runs = Runs()

    def write(name, lines):
        if len(runs.objects) == 8:
            list(lines)
            raise RuntimeError("secret captured provider value")
        return runs.write(name, lines)

    with pytest.raises(DatabaseUnavailable) as error:
        runs.sort([str(i) for i in range(100)], "int8", write_run=write)
    assert "secret" not in str(error.value)
    assert len(runs.objects) == 8
    assert not any(kind == "delete" for kind, _ in runs.events)
    assert runs.open_readers == 0


@pytest.mark.parametrize(
    "corruption", ["partial", "count", "ordinal", "shape", "large_chunk"]
)
def test_private_run_corruption_is_redacted_and_keeps_run_for_cleanup(corruption):
    runs = Runs()

    def read(reference):
        raw = runs.objects[reference["object"]]
        if corruption == "partial":
            yield raw[:-1]
        elif corruption == "count":
            yield raw + raw
        elif corruption == "large_chunk":
            yield b"x" * (sorting.READ_CHUNK_BYTES + 1)
        else:
            value = json.loads(raw)
            if corruption == "ordinal":
                value["ordinal"] = -1
            else:
                value["row"] = ["private secret"]
            yield json.dumps(value).encode() + b"\n"

    with pytest.raises(DatabaseUnavailable) as error:
        runs.sort(["a"], read_run=read)
    assert "private secret" not in str(error.value)
    assert len(runs.objects) == 1


def test_unconsumed_write_callback_is_rejected_without_publishing():
    runs = Runs()
    with pytest.raises(DatabaseUnavailable):
        runs.sort(["a"], write_run=lambda name, lines: {"object": name})
    assert not runs.output


def test_output_failure_is_redacted_and_retains_private_run():
    runs = Runs()

    def emit(batch):
        raise OSError("secret object diagnostic")

    with pytest.raises(DatabaseUnavailable) as error:
        runs.sort(["a"], on_rows=emit)
    assert "secret" not in str(error.value)
    assert len(runs.objects) == 1


def test_ddl_or_database_connections_are_never_used(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("Ordering an immutable capture must not use PostgreSQL")

    monkeypatch.setattr("psycopg.connect", fail)
    runs = Runs()
    runs.sort(["c", "a", "b"])
    assert runs.output == [["a", 1], ["b", 2], ["c", 0]]
