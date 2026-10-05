"""Bounded XLSX streaming, with literal text and no temporary workbook files.

The caller supplies a private create-only GCS BlobWriter (ignore_flush=True,
8 MiB chunks). It publishes the object only after successful finalization.
SQL NULL is the text \\N; a leading backslash in an actual string is escaped.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
import json
import math
import re
from time import monotonic
from typing import BinaryIO, cast
from xml.sax.saxutils import escape
import zipfile

from .database_console import DatabaseResourceExceeded, MAX_COLUMNS

MAX_DATA_ROWS = 1_048_575
MAX_CELL_CHARACTERS = 32_767
MAX_CELL_LINE_FEEDS = 253
MAX_COMPRESSED_BYTES = 268_435_456
MAX_XML_BYTES = 1_073_741_824
_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_ESCAPE_TOKEN = re.compile(r"_x[0-9a-fA-F]{4}_")
_INTEGER_TYPES = {"int2", "int4", "int8", "oid"}


class _Guard:
    def __init__(self, authorize: Callable[[], None], timeout: float):
        if not 0 < timeout <= 240:
            raise DatabaseResourceExceeded("Invalid export execution budget")
        self.authorize = authorize
        self.deadline = monotonic() + timeout
        self.checked = float("-inf")
        self.xml_bytes = 0

    def check(self, *, force: bool = False) -> None:
        now = monotonic()
        if now >= self.deadline:
            raise DatabaseResourceExceeded("Export execution time budget exceeded")
        if force or now - self.checked >= 1:
            self.authorize()
            self.checked = now

    def xml(self, value: str) -> bytes:
        self.check()
        encoded = value.encode("utf-8")
        self.xml_bytes += len(encoded)
        if self.xml_bytes > MAX_XML_BYTES:
            raise DatabaseResourceExceeded("Export XML size budget exceeded")
        return encoded


class _Output:
    """Deliberately unseekable; ZipFile uses streaming data descriptors."""

    def __init__(self, sink: BinaryIO, guard: _Guard):
        self.sink = sink
        self.guard = guard
        self.bytes = 0

    def write(self, value: bytes) -> int:
        self.guard.check()
        if self.bytes + len(value) > MAX_COMPRESSED_BYTES:
            raise DatabaseResourceExceeded("Export file size budget exceeded")
        written = self.sink.write(value)
        if written is not None and written != len(value):
            raise OSError("Incomplete export upload")
        self.bytes += len(value)
        return len(value)

    def tell(self) -> int:
        return self.bytes

    def flush(self) -> None:
        # BlobWriter is committed/closed by its owner, not by ZipFile.flush().
        self.guard.check()


def _xstring(value: str) -> str:
    if value.count("\n") > MAX_CELL_LINE_FEEDS:
        raise DatabaseResourceExceeded("Export cell line break budget exceeded")
    if (
        len(value.encode("utf-16-le", errors="surrogatepass")) // 2
        > MAX_CELL_CHARACTERS
    ):
        raise DatabaseResourceExceeded("Export cell length budget exceeded")
    value = _ESCAPE_TOKEN.sub(lambda match: "_x005F_" + match.group()[1:], value)
    result = []
    for character in value:
        ordinal = ord(character)
        if (
            ordinal == 13
            or (ordinal < 32 and ordinal not in {9, 10})
            or ordinal in {0xFFFE, 0xFFFF}
        ):
            result.append(f"_x{ordinal:04X}_")
        elif 0xD800 <= ordinal <= 0xDFFF:
            raise DatabaseResourceExceeded("Unsupported export character")
        else:
            result.append(character)
    return escape("".join(result))


def _column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _text_cell(reference: str, value: str, *, header: bool = False) -> str:
    style = ' s="1"' if header else ""
    return f'<c r="{reference}" t="inlineStr"{style}><is><t xml:space="preserve">{_xstring(value)}</t></is></c>'


def _cell(reference: str, value, data_type: str) -> str:
    if value is None:
        return _text_cell(reference, "\\N")
    if isinstance(value, bool) and data_type in {"bool", "boolean"}:
        return f'<c r="{reference}" t="b"><v>{int(value)}</v></c>'
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and data_type in _INTEGER_TYPES
        and len(str(abs(value))) <= 15
    ):
        return f'<c r="{reference}" t="n"><v>{value}</v></c>'
    if isinstance(value, float) and not math.isfinite(value):
        raise DatabaseResourceExceeded("Unsupported export numeric value")
    if (
        data_type in {"json", "jsonb"}
        or data_type.startswith("_")
        or isinstance(value, (dict, list))
    ):
        try:
            if isinstance(value, str):
                # Typed documents arrive as exact PostgreSQL JSON text. Validate
                # without converting numeric lexemes or quoting the document.
                json.loads(value, parse_int=str, parse_float=str)
                text = value
            else:
                text = json.dumps(
                    value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                )
        except (ValueError, TypeError, RecursionError):
            raise DatabaseResourceExceeded("Unsupported export value") from None
    else:
        text = str(value)
    if text.startswith("\\"):
        text = "\\" + text
    return _text_cell(reference, text)


def _write_sheet(archive, index, columns, rows, guard: _Guard) -> int:
    count = 0
    with archive.open(
        f"xl/worksheets/sheet{index}.xml", "w", force_zip64=True
    ) as sheet:

        def write(value: str):
            sheet.write(guard.xml(value))

        write(
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="{_NS}"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><sheetFormatPr defaultRowHeight="15"/><sheetData><row r="1">'
        )
        for number, column in enumerate(columns, 1):
            write(_text_cell(f"{_column_name(number)}1", column["name"], header=True))
        write("</row>")
        for row in rows:
            guard.check()
            if len(row) != len(columns):
                raise DatabaseResourceExceeded("Invalid export result shape")
            count += 1
            write(f'<row r="{count + 1}">')
            for number, (column, value) in enumerate(zip(columns, row, strict=True), 1):
                write(
                    _cell(
                        f"{_column_name(number)}{count + 1}", value, column["data_type"]
                    )
                )
            write("</row>")
        write(
            f'</sheetData><autoFilter ref="A1:{_column_name(len(columns))}{count + 1}"/></worksheet>'
        )
    guard.check(force=True)
    return count


def write_xlsx(
    sink: BinaryIO,
    columns: list[dict],
    rows: Iterable[list],
    authorize: Callable[[], None] = lambda: None,
    *,
    timeout_seconds: float = 240,
) -> dict:
    """Write every row, split worksheets, and return only after ZIP closes."""
    if not 0 < len(columns) <= MAX_COLUMNS or any(
        not isinstance(column.get("name"), str)
        or not isinstance(column.get("data_type"), str)
        for column in columns
    ):
        raise DatabaseResourceExceeded("Invalid export columns")
    guard = _Guard(authorize, timeout_seconds)
    guard.check(force=True)
    output = _Output(sink, guard)
    source = iter(rows)
    sentinel = object()
    first = next(source, sentinel)
    count = 0
    sheets = 0
    # ZipFile probes seek support at runtime and uses write/tell/flush only for
    # this streaming sink; BinaryIO's broader typing also includes read/seek.
    with zipfile.ZipFile(
        cast(BinaryIO, output),
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        allowZip64=True,
    ) as archive:
        while True:
            sheets += 1

            def sheet_rows():
                nonlocal first
                if first is sentinel:
                    return
                yield first
                for _ in range(MAX_DATA_ROWS - 1):
                    value = next(source, sentinel)
                    if value is sentinel:
                        break
                    yield value

            count += _write_sheet(archive, sheets, columns, sheet_rows(), guard)
            first = next(source, sentinel)
            if first is sentinel:
                break

        def file(name: str, value: str):
            archive.writestr(name, guard.xml(value))

        file(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            + "".join(
                f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                for index in range(1, sheets + 1)
            )
            + "</Types>",
        )
        file(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        )
        file(
            "xl/workbook.xml",
            f'<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="{_NS}" xmlns:r="{_REL_NS}"><sheets>'
            + "".join(
                f'<sheet name="Datos {index}" sheetId="{index}" r:id="rId{index}"/>'
                for index in range(1, sheets + 1)
            )
            + "</sheets></workbook>",
        )
        file(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(
                f'<Relationship Id="rId{index}" Type="{_REL_NS}/worksheet" Target="worksheets/sheet{index}.xml"/>'
                for index in range(1, sheets + 1)
            )
            + f'<Relationship Id="styles" Type="{_REL_NS}/styles" Target="styles.xml"/></Relationships>',
        )
        file(
            "xl/styles.xml",
            f'<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="{_NS}"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>',
        )
        guard.check(force=True)
    guard.check(force=True)
    return {"row_count": count, "sheet_count": sheets, "bytes": output.bytes}
