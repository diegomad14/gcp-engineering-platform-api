"""Stable external ordering of an immutable capture, without a database connection.

Run callbacks own private, generation-pinned storage. They must finish a write
before returning, stream reads in chunks of at most one MiB, and purge their
namespace if this operation fails. Consumed runs are erased only after their
replacement is durable. Numeric lexemes remain strings throughout transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import heapq
import json
import math
import re
from time import monotonic
from typing import Any, Callable, Generator, Iterator

from fastapi import HTTPException

from .database_console import DatabaseResourceExceeded
from .database_registry import DatabaseUnavailable

MAX_COLUMNS = 100
MAX_CELL_BYTES = 16_384
MAX_ROW_BYTES = 65_536
MAX_RECORD_BYTES = MAX_ROW_BYTES + 128
READ_CHUNK_BYTES = 1_048_576
MEMORY_BYTES = 32 * 1_048_576
RUN_MEMORY_BYTES = 8 * 1_048_576
MERGE_FAN_IN = 8
BATCH_ROWS = 16
_NUMERIC = {"int2", "int4", "int8", "numeric", "float4", "float8"}
_TEMPORAL = {"date", "time", "timetz", "timestamp", "timestamptz"}
_TYPE_ALIASES = {
    "smallint": "int2",
    "integer": "int4",
    "bigint": "int8",
    "decimal": "numeric",
    "real": "float4",
    "double precision": "float8",
    "boolean": "bool",
    "timestamp without time zone": "timestamp",
    "timestamp with time zone": "timestamptz",
    "time without time zone": "time",
    "time with time zone": "timetz",
}
_DATE = re.compile(r"([0-9]{4,9})-([0-9]{2})-([0-9]{2})\Z")
_TIME = re.compile(
    r"([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,6}))?"
    r"(Z|([+-])([0-9]{2})(?::([0-9]{2}))?(?::([0-9]{2}))?)?\Z"
)


def _encode(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, allow_nan=False, separators=(",", ":")
    ).encode()


def _type_name(data_type: str) -> str:
    value = data_type.strip().lower()
    # Typmods do not change the comparison domain; array types stay lexical.
    if "[" not in value and not value.startswith("_"):
        value = re.sub(r"\([0-9]+(?:\s*,\s*[0-9]+)?\)", "", value).strip()
    return _TYPE_ALIASES.get(value, value)


def _civil_day(value: str, bce: bool) -> int:
    match = _DATE.fullmatch(value)
    if match is None:
        raise ValueError
    year, month, day = (int(part) for part in match.groups())
    if year == 0 or not 1 <= month <= 12:
        raise ValueError
    year = 1 - year if bce else year
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    months = (31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    if not 1 <= day <= months[month - 1]:
        raise ValueError
    previous = year - 1
    return (
        previous * 365
        + previous // 4
        - previous // 100
        + previous // 400
        + sum(months[: month - 1])
        + day
    )


def _clock_ticks(value: str) -> tuple[int, int | None]:
    match = _TIME.fullmatch(value)
    if match is None:
        raise ValueError
    hour, minute, second = (int(part) for part in match.groups()[:3])
    microsecond = int((match[4] or "").ljust(6, "0"))
    if (
        hour > 24
        or minute > 59
        or second > 59
        or (hour == 24 and (minute or second or microsecond))
    ):
        raise ValueError
    ticks = (hour * 3600 + minute * 60 + second) * 1_000_000 + microsecond
    offset = None
    if match[5]:
        offset_hour, offset_minute, offset_second = (
            int(match[index] or "0") for index in (7, 8, 9)
        )
        if offset_hour > 23 or offset_minute > 59 or offset_second > 59:
            raise ValueError
        offset = (offset_hour * 3600 + offset_minute * 60 + offset_second) * 1_000_000
        if match[6] == "-":
            offset = -offset
    return ticks, offset


def _temporal_key(value: str, data_type: str) -> tuple:
    lowered = value.lower()
    if lowered in {"-infinity", "infinity", "+infinity"}:
        return (0 if lowered.startswith("-") else 2, 0)
    try:
        bce = value.endswith(" BC")
        value = value[:-3] if bce else value
        if data_type == "date":
            return (1, _civil_day(value, bce))
        if data_type in {"time", "timetz"}:
            if bce:
                raise ValueError
            ticks, offset = _clock_ticks(value)
        else:
            pieces = re.split("[T ]", value)
            if len(pieces) != 2:
                raise ValueError
            ticks, offset = _clock_ticks(pieces[1])
            ticks += _civil_day(pieces[0], bce) * 86_400_000_000
        if data_type in {"timestamptz", "timetz"} and offset is None:
            raise ValueError
        if data_type == "timestamp" and offset is not None:
            raise ValueError
        if offset is not None:
            ticks -= offset
        return (1, ticks)
    except (ValueError, OverflowError):
        raise DatabaseResourceExceeded(
            "Unsupported database sort temporal value"
        ) from None


def _value_key(value: Any, data_type: str) -> tuple | None:
    if value is None:
        return None
    if data_type in _NUMERIC:
        if type(value) not in {str, int}:
            raise DatabaseResourceExceeded("Unsupported database sort numeric value")
        try:
            number = Decimal(value)
        except InvalidOperation:
            raise DatabaseResourceExceeded(
                "Unsupported database sort numeric value"
            ) from None
        if number.is_nan():
            return (3, 0)
        if number.is_infinite():
            return (0 if number.is_signed() else 2, 0)
        return (1, number)
    if data_type == "bool":
        if type(value) is bool:
            return (1, int(value))
        if value in {"false", "true"}:
            return (1, int(value == "true"))
        raise DatabaseResourceExceeded("Unsupported database sort boolean value")
    if data_type in _TEMPORAL:
        if not isinstance(value, str):
            raise DatabaseResourceExceeded("Unsupported database sort temporal value")
        return _temporal_key(value, data_type)
    # Unicode code points, with no locale/collation or normalization. JSON,
    # jsonb, arrays, bytea and unknown types compare their exact stored strings.
    return (1, value if isinstance(value, str) else _encode(value).decode())


class _Order:
    def __init__(self, descending: bool, check: Callable[[], None]):
        self.descending = descending
        self.check = check
        self.comparisons = 0

    def less(self, left: _Record, right: _Record) -> bool:
        self.comparisons += 1
        if self.comparisons % 1024 == 0:
            self.check()
        if left.key is None or right.key is None:
            if left.key is None and right.key is None:
                return left.ordinal < right.ordinal
            return left.key is not None
        if left.key == right.key:
            return left.ordinal < right.ordinal
        return left.key > right.key if self.descending else left.key < right.key


@dataclass(slots=True)
class _Record:
    ordinal: int
    row: list
    raw: bytes
    key: tuple | None
    order: _Order
    cost: int

    def __lt__(self, other: _Record) -> bool:
        return self.order.less(self, other)


@dataclass(slots=True)
class _Run:
    reference: dict
    row_count: int
    level: int


def _row_record(
    row: Any, ordinal: int, width: int, selected: int, data_type: str, order: _Order
) -> _Record:
    if not isinstance(row, list) or len(row) != width:
        raise DatabaseUnavailable("Invalid database sort row")
    if any(type(cell) not in {str, int, bool, type(None)} for cell in row):
        raise DatabaseResourceExceeded("Unsupported database sort value")
    row = list(row)
    if any(len(_encode(cell)) > MAX_CELL_BYTES for cell in row):
        raise DatabaseResourceExceeded("Database sort cell size budget exceeded")
    encoded = _encode(row)
    if len(encoded) + 1 > MAX_ROW_BYTES:
        raise DatabaseResourceExceeded("Database sort row size budget exceeded")
    key = _value_key(row[selected], data_type)
    raw = _encode({"ordinal": ordinal, "row": row}) + b"\n"
    if len(raw) > MAX_RECORD_BYTES:
        raise DatabaseResourceExceeded("Database sort record size budget exceeded")
    # Includes decoded Unicode (up to four bytes/code point), the encoded row,
    # decimal key, both row lists during copying, list-sort scratch and objects.
    cost = len(raw) * 8 + width * 128 + 1024
    return _Record(ordinal, row, raw, key, order, cost)


def sort_rows(
    columns: list[dict],
    rows: Iterator[list],
    column_key: str,
    direction: str,
    *,
    authorize: Callable[[], None],
    write_run: Callable[[str, Iterator[bytes]], dict],
    read_run: Callable[[dict], Iterator[bytes]],
    delete_run: Callable[[dict], None],
    on_rows: Callable[[list[list]], None],
    deadline: float,
) -> dict:
    """Emit every captured row in stable order, NULLS LAST in both directions.

    The engine keeps at most eight readers and eight MiB of initial records.
    Storage adapters reserve their own buffers within the total 64 MiB budget;
    this engine's conservative working-set estimate is capped at 32 MiB.
    Online compaction bounds live run descriptors instead of limiting rows.
    """
    try:
        return _sort_rows(
            columns,
            rows,
            column_key,
            direction,
            authorize=authorize,
            write_run=write_run,
            read_run=read_run,
            delete_run=delete_run,
            on_rows=on_rows,
            deadline=deadline,
        )
    except (HTTPException, DatabaseResourceExceeded):
        raise
    except Exception:
        # Storage and decoder diagnostics can contain captured values/paths.
        raise DatabaseUnavailable("Database sort is unavailable") from None


def _sort_rows(
    columns: list[dict],
    rows: Iterator[list],
    column_key: str,
    direction: str,
    *,
    authorize: Callable[[], None],
    write_run: Callable[[str, Iterator[bytes]], dict],
    read_run: Callable[[dict], Iterator[bytes]],
    delete_run: Callable[[dict], None],
    on_rows: Callable[[list[list]], None],
    deadline: float,
) -> dict:
    if (
        not isinstance(columns, list)
        or not 1 <= len(columns) <= MAX_COLUMNS
        or any(
            not isinstance(column, dict)
            or set(column) != {"key", "name", "data_type"}
            or any(not isinstance(value, str) for value in column.values())
            or any(len(_encode(value)) > MAX_CELL_BYTES for value in column.values())
            for column in columns
        )
        or len({column["key"] for column in columns}) != len(columns)
        or direction not in {"asc", "desc"}
        or not isinstance(column_key, str)
        or column_key not in {column["key"] for column in columns}
        or not isinstance(deadline, (int, float))
        or not math.isfinite(deadline)
    ):
        raise DatabaseResourceExceeded("Invalid database sort specification")
    selected = next(
        i for i, column in enumerate(columns) if column["key"] == column_key
    )
    data_type = _type_name(columns[selected]["data_type"])

    def check() -> None:
        authorize()
        if monotonic() >= deadline:
            raise DatabaseResourceExceeded("Database sort time budget exceeded")

    check()
    order = _Order(direction == "desc", check)
    levels: list[list[_Run]] = []
    run_count = 0
    merge_passes = 0
    memory_peak = 0
    row_count = 0
    byte_count = 0

    def reserve(size: int) -> None:
        nonlocal memory_peak
        # Run references may contain per-object chunk metadata. Their serialized
        # size times eight also covers the dict/list/string object overhead.
        metadata = sum(
            len(_encode(run.reference)) * 8 + 1024 for level in levels for run in level
        )
        size += metadata
        if size > MEMORY_BYTES:
            raise DatabaseResourceExceeded("Database sort memory budget exceeded")
        memory_peak = max(memory_peak, size)

    def write(records: Iterator[_Record], count: int, level: int) -> _Run:
        nonlocal run_count
        check()
        emitted = 0

        def lines() -> Iterator[bytes]:
            nonlocal emitted
            for record in records:
                check()
                emitted += 1
                yield record.raw

        reference = write_run(f"run-{run_count:06d}.ndjson", lines())
        run_count += 1
        check()
        if emitted != count or not isinstance(reference, dict):
            raise DatabaseUnavailable("Invalid database sort run")
        return _Run(reference, count, level)

    def records(run: _Run) -> Generator[_Record, None, None]:
        buffer = bytearray()
        count = 0
        try:
            for chunk in read_run(run.reference):
                check()
                if not isinstance(chunk, bytes) or len(chunk) > READ_CHUNK_BYTES:
                    raise DatabaseResourceExceeded("Database sort read budget exceeded")
                buffer.extend(chunk)
                while True:
                    end = buffer.find(b"\n")
                    if end < 0:
                        if len(buffer) >= MAX_RECORD_BYTES:
                            raise DatabaseResourceExceeded(
                                "Database sort record size budget exceeded"
                            )
                        break
                    if end + 1 > MAX_RECORD_BYTES:
                        raise DatabaseResourceExceeded(
                            "Database sort record size budget exceeded"
                        )
                    line = bytes(buffer[:end])
                    del buffer[: end + 1]
                    value = json.loads(line)
                    if (
                        not isinstance(value, dict)
                        or set(value) != {"ordinal", "row"}
                        or type(value["ordinal"]) is not int
                        or not 0 <= value["ordinal"] < row_count
                    ):
                        raise DatabaseUnavailable("Invalid database sort record")
                    record = _row_record(
                        value["row"],
                        value["ordinal"],
                        len(columns),
                        selected,
                        data_type,
                        order,
                    )
                    count += 1
                    if count > run.row_count:
                        raise DatabaseUnavailable("Invalid database sort run count")
                    check()
                    yield record
            if buffer or count != run.row_count:
                raise DatabaseUnavailable("Invalid database sort run count")
        finally:
            buffer.clear()

    def merge(group: list[_Run]) -> Iterator[_Record]:
        if not 1 <= len(group) <= MERGE_FAN_IN:
            raise DatabaseResourceExceeded("Database sort merge budget exceeded")
        # One read chunk/copy, a bounded pending line and decoded heap record per
        # reader, plus a full 16-row output batch and one transient record.
        reserve(
            len(group) * (READ_CHUNK_BYTES * 2 + MAX_RECORD_BYTES * 10)
            + (BATCH_ROWS + 1) * (MAX_RECORD_BYTES * 8 + MAX_COLUMNS * 128 + 1024)
        )
        iterators = [records(run) for run in group]
        heap: list[tuple[_Record, int]] = []
        try:
            for index, iterator in enumerate(iterators):
                record = next(iterator, None)
                if record is not None:
                    heapq.heappush(heap, (record, index))
            while heap:
                check()
                record, index = heapq.heappop(heap)
                yield record
                record = next(iterators[index], None)
                if record is not None:
                    heapq.heappush(heap, (record, index))
        finally:
            for iterator in iterators:
                iterator.close()

    def compact(group: list[_Run]) -> _Run:
        nonlocal merge_passes
        level = max(run.level for run in group) + 1
        replacement = write(merge(group), sum(run.row_count for run in group), level)
        merge_passes = max(merge_passes, level)
        for run in group:
            check()
            delete_run(run.reference)
            check()
        return replacement

    def add(run: _Run) -> None:
        while len(levels) <= run.level:
            levels.append([])
        levels[run.level].append(run)
        reserve(0)
        if len(levels[run.level]) == MERGE_FAN_IN:
            group = levels[run.level]
            replacement = compact(group)
            levels[run.level] = []
            add(replacement)

    pending: list[_Record] = []
    pending_cost = 0
    for row in rows:
        check()
        record = _row_record(row, row_count, len(columns), selected, data_type, order)
        # Make this ordinal visible while an online merge reads previous runs.
        row_count += 1
        byte_count += len(_encode(record.row)) + 1
        if pending and pending_cost + record.cost > RUN_MEMORY_BYTES:
            check()
            pending.sort()
            check()
            run = write(iter(pending), len(pending), 0)
            pending.clear()
            pending_cost = 0
            add(run)
        reserve(pending_cost + record.cost)
        pending.append(record)
        pending_cost += record.cost
    if pending:
        check()
        pending.sort()
        check()
        run = write(iter(pending), len(pending), 0)
        pending.clear()
        add(run)
    group = [run for level in levels for run in level]
    while len(group) > MERGE_FAN_IN:
        replacement = compact(group[:MERGE_FAN_IN])
        group = [replacement, *group[MERGE_FAN_IN:]]
        levels = [group]
    emitted = 0
    batch: list[list] = []
    if group:
        for record in merge(group):
            batch.append(record.row)
            emitted += 1
            if len(batch) == BATCH_ROWS:
                check()
                on_rows(batch)
                batch = []
                check()
        if batch:
            check()
            on_rows(batch)
            check()
    if emitted != row_count:
        raise DatabaseUnavailable("Invalid database sort output count")
    for run in group:
        check()
        delete_run(run.reference)
        check()
    check()
    return {
        "row_count": row_count,
        "bytes": byte_count,
        "run_count": run_count,
        "merge_passes": merge_passes,
        "memory_peak_bytes": memory_peak,
    }
