"""Private immutable result objects, addressed by exact GCS generation/hash."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import re
import time
from typing import Any, Callable, Iterator

from fastapi import HTTPException

from ..config import config
from .database_registry import DatabaseUnavailable

CHUNK_BYTES = 1_048_576
SNAPSHOT_BYTES = 268_435_456
CELL_BYTES = 16_384
ROW_BYTES = 65_536
EXPORT_BYTES = 268_435_456
_test_backend: Any = None
_client: Any = None


@contextmanager
def testing_backend(backend):
    global _test_backend
    previous = _test_backend
    _test_backend = backend
    try:
        yield backend
    finally:
        _test_backend = previous


def _path(path: Any) -> str:
    if not isinstance(path, str) or not re.fullmatch(
        r"(?:inputs|results)/[a-f0-9]{32}/[a-z0-9_.-]{1,100}", path
    ):
        raise DatabaseUnavailable("Invalid database object identity")
    return path


def _reference(reference: dict, maximum: int, *, hashed: bool = True) -> str:
    if not isinstance(reference, dict):
        raise DatabaseUnavailable("Invalid database object metadata")
    path = _path(reference.get("object"))
    size, generation = reference.get("bytes"), reference.get("generation")
    if (
        type(size) is not int
        or not 0 <= size <= maximum
        or type(generation) is not int
        or generation <= 0
        or (
            hashed
            and (
                not isinstance(reference.get("sha256"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", reference["sha256"])
            )
        )
    ):
        raise DatabaseUnavailable("Invalid database object bounds")
    return path


def _columns(columns: list[dict]) -> None:
    if (
        not isinstance(columns, list)
        or not 1 <= len(columns) <= 100
        or any(
            not isinstance(column, dict)
            or set(column) != {"key", "name", "data_type"}
            or any(not isinstance(value, str) for value in column.values())
            or any(len(_encode(value)) > CELL_BYTES for value in column.values())
            for column in columns
        )
        or len({column["key"] for column in columns}) != len(columns)
    ):
        raise DatabaseUnavailable("Invalid database result columns")


def _bucket():
    global _client
    if not config.databases.result_bucket or not config.databases.project_id:
        raise DatabaseUnavailable("Database result store is unavailable")
    if _client is None:
        from google.cloud import storage

        _client = storage.Client(project=config.databases.project_id)
    return _client.bucket(config.databases.result_bucket)


def _encode(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, allow_nan=False, separators=(",", ":")
    ).encode()


def put(path: str, data: bytes, content_type: str = "application/json") -> dict:
    _path(path)
    try:
        if _test_backend is not None:
            generation = _test_backend.put(path, data, content_type)
        else:
            blob = _bucket().blob(path)
            blob.upload_from_string(
                data,
                content_type=content_type,
                if_generation_match=0,
                timeout=4,
                retry=None,
            )
            generation = blob.generation
        if generation is None:
            raise DatabaseUnavailable("Database object generation is unavailable")
        return {
            "object": path,
            "generation": int(generation),
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
        }
    except Exception:
        raise DatabaseUnavailable("Database result store is unavailable") from None


def read(reference: dict, maximum: int = CHUNK_BYTES) -> bytes:
    path = _reference(reference, maximum)
    expected = reference["bytes"]
    try:
        if _test_backend is not None:
            raw = _test_backend.read(reference, maximum)
        else:
            # A bounded range prevents an unexpectedly replaced/oversized object
            # from buffering an unbounded response before the integrity check.
            raw = (
                _bucket()
                .blob(path, generation=reference["generation"])
                .download_as_bytes(
                    start=0,
                    end=maximum,
                    if_generation_match=reference["generation"],
                    timeout=4,
                    retry=None,
                )
            )
        if (
            len(raw) != expected
            or hashlib.sha256(raw).hexdigest() != reference["sha256"]
        ):
            raise DatabaseUnavailable("Database object integrity check failed")
        return raw
    except Exception:
        raise DatabaseUnavailable("Database result store is unavailable") from None


def delete(reference: dict) -> None:
    path = _reference(reference, EXPORT_BYTES, hashed=False)
    try:
        if _test_backend is not None:
            _test_backend.delete(reference)
            return
        from google.api_core.exceptions import NotFound

        try:
            _bucket().blob(path, generation=reference["generation"]).delete(
                if_generation_match=reference["generation"], timeout=4, retry=None
            )
        except NotFound:
            pass
    except Exception:
        raise DatabaseUnavailable("Database result cleanup is unavailable") from None


def stage_sql(execution_id: str, statement: str) -> dict:
    raw = _encode({"sql": statement})
    if len(raw) > 131_072:
        raise HTTPException(413, "Database request exceeds the size limit")
    return put(f"inputs/{execution_id}/query.json", raw)


def take_sql(reference: dict) -> str:
    raw = read(reference, 131_072)
    try:
        value = json.loads(raw)
        statement = value["sql"]
        if not isinstance(statement, str) or not 1 <= len(statement) <= 30_000:
            raise ValueError("Invalid input")
    except Exception:
        raise DatabaseUnavailable("Database input is unavailable") from None
    # Failure to erase the staged SQL prevents dispatch to PostgreSQL.
    delete(reference)
    return statement


class ResultWriter:
    def __init__(self, execution_id: str, authorize: Callable[[], None]):
        self.execution_id = execution_id
        self.authorize = authorize
        self.columns: list[dict] = []
        self.chunks: list[dict] = []
        self.buffer = bytearray()
        self.buffer_count = 0
        self.row_count = 0
        self.byte_count = 0
        self.stored_bytes = 0

    def on_columns(self, columns: list[dict]) -> None:
        _columns(columns)
        self.columns = columns
        self.byte_count = len(_encode(columns))

    def on_rows(self, rows: list[list]) -> None:
        self.authorize()
        for row in rows:
            if not isinstance(row, list) or len(row) != len(self.columns):
                raise DatabaseUnavailable("Invalid database result row")
            encoded = _encode(row) + b"\n"
            if (
                len(encoded) > ROW_BYTES
                or any(len(_encode(cell)) > CELL_BYTES for cell in row)
                or self.byte_count + len(encoded) > SNAPSHOT_BYTES
            ):
                raise HTTPException(422, "Database result exceeds the resource budget")
            if self.buffer and len(self.buffer) + len(encoded) > CHUNK_BYTES:
                self._flush()
            self.buffer.extend(encoded)
            self.buffer_count += 1
            self.row_count += 1
            self.byte_count += len(encoded)

    def _flush(self) -> None:
        self.authorize()
        if not self.buffer:
            return
        reference = put(
            f"results/{self.execution_id}/chunk-{len(self.chunks):06d}.ndjson",
            bytes(self.buffer),
            "application/x-ndjson",
        )
        reference.update(
            row_start=self.row_count - self.buffer_count, row_count=self.buffer_count
        )
        self.chunks.append(reference)
        self.buffer.clear()
        self.buffer_count = 0

    def finish(self) -> dict:
        self._flush()
        self.authorize()
        manifest = {
            "execution_id": self.execution_id,
            "columns": self.columns,
            "chunks": self.chunks,
            "row_count": self.row_count,
            "bytes": self.byte_count,
        }
        raw = _encode(manifest)
        if len(raw) > CHUNK_BYTES:
            raise DatabaseUnavailable("Database result manifest exceeds the budget")
        self.stored_bytes = self.byte_count - len(_encode(self.columns)) + len(raw)
        if self.stored_bytes > SNAPSHOT_BYTES:
            raise HTTPException(422, "Database result exceeds the resource budget")
        return put(f"results/{self.execution_id}/manifest.json", raw)


def manifest(reference: dict) -> dict:
    try:
        value = json.loads(read(reference))
        if (
            not isinstance(value, dict)
            or type(value["bytes"]) is not int
            or not 0 <= value["bytes"] <= SNAPSHOT_BYTES
            or type(value["row_count"]) is not int
            or not 0 <= value["row_count"]
            or not isinstance(value["chunks"], list)
            or len(value["chunks"]) > 512
            or reference["object"] != f"results/{value['execution_id']}/manifest.json"
        ):
            raise ValueError("Invalid manifest")
        _columns(value["columns"])
        start = 0
        byte_count = len(_encode(value["columns"]))
        for index, chunk in enumerate(value["chunks"]):
            path = _reference(chunk, CHUNK_BYTES)
            if (
                type(chunk["row_start"]) is not int
                or chunk["row_start"] != start
                or type(chunk["row_count"]) is not int
                or not 1 <= chunk["row_count"] <= chunk["bytes"]
                or path != f"results/{value['execution_id']}/chunk-{index:06d}.ndjson"
            ):
                raise ValueError("Invalid chunk order")
            start += chunk["row_count"]
            byte_count += chunk["bytes"]
        if start != value["row_count"] or byte_count != value["bytes"]:
            raise ValueError("Invalid manifest count")
        return value
    except Exception:
        raise DatabaseUnavailable("Database result manifest is unavailable") from None


def rows(
    value: dict,
    authorize: Callable[[], None],
    *,
    start: int = 0,
    limit: int | None = None,
) -> Iterator[list]:
    end = value["row_count"] if limit is None else start + limit
    for chunk in value["chunks"]:
        if chunk["row_start"] >= end:
            break
        if chunk["row_start"] + chunk["row_count"] <= start:
            continue
        authorize()
        lines = read(chunk).splitlines()
        if len(lines) != chunk["row_count"]:
            raise DatabaseUnavailable("Database result count is unavailable")
        for offset, line in enumerate(lines):
            position = chunk["row_start"] + offset
            if start <= position < end:
                yield json.loads(line)


@contextmanager
def export_sink(execution_id: str, export_id: str):
    path = _path(f"results/{execution_id}/export-{export_id}.xlsx")
    try:
        if _test_backend is not None:
            with _test_backend.export_sink(path) as sink:
                yield sink
            return
        blob = _bucket().blob(path)
        with blob.open(
            "wb",
            chunk_size=8_388_608,
            ignore_flush=True,
            if_generation_match=0,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            timeout=4,
            retry=None,
        ) as sink:
            yield sink
    except (HTTPException, DatabaseUnavailable):
        raise
    except Exception:
        raise DatabaseUnavailable("Database export is unavailable") from None


def export_reference(execution_id: str, export_id: str) -> dict:
    path = _path(f"results/{execution_id}/export-{export_id}.xlsx")
    try:
        if _test_backend is not None:
            return _test_backend.export_reference(path)
        blob = _bucket().get_blob(path, timeout=4, retry=None)
        if blob is None or blob.size is None or not 0 < blob.size <= EXPORT_BYTES:
            raise DatabaseUnavailable("Database export exceeds the budget")
        return {"object": path, "generation": int(blob.generation), "bytes": blob.size}
    except Exception:
        raise DatabaseUnavailable("Database export is unavailable") from None


def download(reference: dict, authorize: Callable[[], None]) -> Iterator[bytes]:
    path = _reference(reference, EXPORT_BYTES, hashed=False)
    if not 0 < reference["bytes"] <= EXPORT_BYTES:
        raise DatabaseUnavailable("Invalid database export bounds")
    authorize()
    try:
        if _test_backend is not None:
            for piece in _test_backend.download(reference, authorize):
                authorize()
                yield piece
            return
        blob = _bucket().blob(path, generation=reference["generation"])
        with blob.open(
            "rb",
            chunk_size=CHUNK_BYTES,
            if_generation_match=reference["generation"],
            timeout=4,
            retry=None,
        ) as source:
            remaining = reference["bytes"]
            while remaining:
                authorize()
                piece = source.read(min(CHUNK_BYTES, remaining))
                if not piece or len(piece) > min(CHUNK_BYTES, remaining):
                    raise DatabaseUnavailable("Database export is incomplete")
                remaining -= len(piece)
                authorize()
                yield piece
            authorize()
    except HTTPException:
        raise
    except Exception:
        raise DatabaseUnavailable("Database export is unavailable") from None


def purge(execution_id: str, *, deadline: float | None = None) -> None:
    _path(f"results/{execution_id}/manifest.json")
    deadline = deadline if deadline is not None else time.monotonic() + 230

    def check():
        if time.monotonic() >= deadline:
            raise DatabaseUnavailable("Database result cleanup is pending")

    try:
        check()
        if _test_backend is not None:
            _test_backend.purge(execution_id)
            check()
            if _test_backend.contains(execution_id):
                raise DatabaseUnavailable("Database result cleanup is pending")
            return
        bucket = _bucket()
        for prefix in (f"inputs/{execution_id}/", f"results/{execution_id}/"):
            check()
            objects = bucket.list_blobs(
                prefix=prefix, max_results=600, timeout=4, retry=None
            )
            for blob in objects:
                check()
                blob.delete(if_generation_match=blob.generation, timeout=4, retry=None)
        # A bounded re-list confirms the complete namespace is empty, including
        # the case where an earlier invocation only deleted part of the prefix.
        for prefix in (f"inputs/{execution_id}/", f"results/{execution_id}/"):
            check()
            if (
                next(
                    iter(
                        bucket.list_blobs(
                            prefix=prefix, max_results=1, timeout=4, retry=None
                        )
                    ),
                    None,
                )
                is not None
            ):
                raise DatabaseUnavailable("Database result cleanup is pending")
    except Exception:
        raise DatabaseUnavailable("Database result cleanup is unavailable") from None


def purge_export(execution_id: str, export_id: str) -> None:
    """Erase a failed export without touching its reusable source snapshot."""
    path = _path(f"results/{execution_id}/export-{export_id}.xlsx")
    if _test_backend is not None:
        try:
            reference = _test_backend.export_reference(path)
        except KeyError:
            return
        delete(reference)
        return
    try:
        blob = _bucket().get_blob(path, timeout=4, retry=None)
        if blob is not None:
            blob.delete(if_generation_match=blob.generation, timeout=4, retry=None)
    except Exception:
        raise DatabaseUnavailable("Database export cleanup is unavailable") from None
