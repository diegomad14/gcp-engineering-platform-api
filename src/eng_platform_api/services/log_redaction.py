"""Bounded, best-effort redaction of untrusted log data before presentation.

Only plain JSON-like builtins are traversed: arbitrary objects are never coerced
to strings or queried for attributes. Redaction and removal of unsafe controls
do not set the returned flag; the flag reports data omitted by resource limits.
This is a presentation safety net, not permission to log credentials at source.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from typing import cast
from urllib.parse import unquote_plus

MAX_TEXT_BYTES = 8 * 1024
MAX_PAYLOAD_BYTES = 32 * 1024
MAX_DEPTH = 8
MAX_NODES = 256
MAX_COLLECTION_ITEMS = 64
MAX_KEY_CHARS = 256
REDACTED = "[REDACTED]"
TRUNCATED = "[TRUNCATED]"
UNSUPPORTED = "[UNSUPPORTED]"

_SENSITIVE_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "authorization",
    "authentication",
    "cookie",
    "privatekey",
    "credential",
    "accesskey",
    "signingkey",
    "signature",
    "sessionid",
    "sessionkey",
)
_ANSI = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*(?:[@-~]|$)|\][^\x07\x1b]*(?:\x07|\x1b\\|$)|[@-_])"
)
_PEM = re.compile(
    r"-----BEGIN(?: [A-Z0-9]+){0,3} PRIVATE KEY-*(?:[\s\S]*?)"
    r"(?:-----END(?: [A-Z0-9]+){0,3} PRIVATE KEY-----|$)",
    re.IGNORECASE,
)
_URL = re.compile(r"\b[a-z][a-z0-9+.-]{0,31}://[^\s<>\"']+", re.IGNORECASE)
_QUERY = re.compile(r"([?&#;])([^?&#;=]{1,8192})=([^&#;]*)")
_ASSIGNMENT = re.compile(
    r"(?<![\w./-])(?:(?P<quote>[\"'])(?P<quoted_key>[\w./ \t-]{1,8192})"
    r"(?P=quote)|(?P<key>[\w./-]{1,8192}))[ \t]*[:=]\s*"
)
_AUTH = re.compile(r"\b(Bearer|Basic)\s+[^\s,;\"'<>]+", re.IGNORECASE)
_JSON_STRING = re.compile(r'"(?:[^"\\]|\\[\s\S])*"')
_ESCAPED_ASSIGNMENT = re.compile(
    r"(?<!\\)\\+[\"'](?P<key>[\w./ -]{1,8192})\\+[\"']\s*[:=]"
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]*){0,4}")
_API_TOKEN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{8,}|"
    r"glpat-[A-Za-z0-9_-]{8,}|sk-(?:proj-)?[A-Za-z0-9_-]{12,}|AKIA[A-Z0-9]{16})\b"
)


def _clip(value: str, limit: int = MAX_TEXT_BYTES) -> tuple[str, bool]:
    # Slice *before* encoding, so a gigantic source never causes a gigantic copy.
    prefix = value[:limit]
    encoded = prefix.encode("utf-8", errors="ignore")
    clipped = len(value) > limit or len(encoded) > limit
    return encoded[:limit].decode("utf-8", errors="ignore"), clipped


def _strip_controls(value: str) -> str:
    value = _ANSI.sub("", value)
    # Retain LF and tab for readable stack traces; strip CR, C1, bidi overrides,
    # zero-width format controls and isolated surrogates.
    return "".join(
        char
        for char in value
        if char in "\n\t" or unicodedata.category(char) not in {"Cc", "Cf", "Cs"}
    )


def _normalized_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", _strip_controls(value))
    return re.sub(r"[^a-z0-9]", "", normalized.lower())


def _sensitive_key(value: str) -> bool:
    normalized = _normalized_key(value)
    return (
        normalized in {"auth", "pass", "pwd", "key", "sig", "jwt", "session"}
        or normalized.endswith("pwd")
        or any(part in normalized for part in _SENSITIVE_PARTS)
    )


def _redact_url(match: re.Match[str], truncated_tail: bool = False) -> str:
    value = match.group(0)
    scheme, _, remainder = value.partition("://")
    boundary = re.search(r"[/?#]", remainder)
    end = boundary.start() if boundary else len(remainder)
    authority = remainder[:end]
    if "@" in authority:
        authority = REDACTED + "@" + authority.rsplit("@", 1)[1]
    elif truncated_tail and boundary is None:
        # The missing '@host' may be beyond the input cutoff; never return a
        # partially read userinfo component as though it were a hostname.
        authority = REDACTED
    value = scheme + "://" + authority + remainder[end:]

    def redact_query(item: re.Match[str]) -> str:
        key = item.group(2)
        if len(key) > MAX_KEY_CHARS:
            return item.group(1) + key + "=" + REDACTED
        # Decode a bounded number of times, including encoded separators in keys.
        for _ in range(3):
            key = unquote_plus(key, errors="replace")
        if _sensitive_key(key) or _normalized_key(key) == "code":
            return item.group(1) + item.group(2) + "=" + REDACTED
        return item.group(0)

    return _QUERY.sub(redact_query, value)


def _value_end(value: str, start: int, header: bool) -> int:
    """Find an assignment's end without parsing or evaluating its contents."""
    if start == len(value):
        return start
    first = value[start]
    if first in "|>":
        # Multiline YAML/block-style assignments: fail closed through the tail.
        return len(value)
    if first in "\"'[{":
        quote = first if first in "\"'" else ""
        stack = [first] if first in "[{" else []
        escaped = False
        for index in range(start + 1, len(value)):
            char = value[index]
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = ""
                    if not stack:
                        return index + 1
            elif char in "\"'":
                quote = char
            elif char in "[{":
                stack.append(char)
            elif char in "]}":
                if (char == "]" and stack[-1] != "[") or (
                    char == "}" and stack[-1] != "{"
                ):
                    return len(value)
                stack.pop()
                if not stack:
                    return index + 1
        # An unterminated quote/container is unsafe to partially display.
        return len(value)
    delimiters = "\n" if header else "\n,;&}]"
    for index in range(start, len(value)):
        if value[index] in delimiters:
            return index
    return len(value)


def _redact_assignments(value: str) -> str:
    parts: list[str] = []
    end = 0
    for match in _ASSIGNMENT.finditer(value):
        key = match.group("quoted_key") or match.group("key")
        long_key = len(key) > MAX_KEY_CHARS
        if match.start() < end or (not long_key and not _sensitive_key(key)):
            continue
        key = "" if long_key else _normalized_key(key)
        stop = _value_end(value, match.end(), "cookie" in key or "authorization" in key)
        parts.extend((value[end : match.end()], REDACTED))
        end = stop
    parts.append(value[end:])
    return "".join(parts)


def sanitize_text(value: str) -> tuple[str, bool]:
    """Return secret-redacted text capped at 8 KiB of UTF-8 and a truncation flag."""
    if type(value) is not str:
        return UNSUPPORTED, False
    return _sanitize_text(value, 0)


def _sanitize_text(value: str, encoding_depth: int) -> tuple[str, bool]:
    value, truncated = _clip(value)
    value = _strip_controls(value)
    if value.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            pass
        else:
            clean_payload, payload_truncated = sanitize_payload(parsed)
            clean_text, output_truncated = _clip(
                json.dumps(clean_payload, ensure_ascii=False)
            )
            return clean_text, truncated or payload_truncated or output_truncated

    def redact_encoded_string(match: re.Match[str]) -> str:
        nonlocal truncated
        encoded = match.group(0)
        if "\\" not in encoded:
            return encoded
        if encoding_depth >= MAX_DEPTH:
            truncated = True
            return json.dumps(REDACTED)
        try:
            decoded = json.loads(encoded)
        except (ValueError, RecursionError):
            return encoded
        clean, clipped = _sanitize_text(decoded, encoding_depth + 1)
        truncated |= clipped
        # Decode obfuscated object keys even when the key itself is benign text.
        # The enclosing assignment pass can then recognize e.g. "pass\\u0077ord".
        following = match.end()
        while following < len(value) and value[following].isspace():
            following += 1
        if (
            following < len(value)
            and value[following] == ":"
            and (len(decoded) > MAX_KEY_CHARS or _sensitive_key(decoded))
        ):
            return json.dumps(decoded, ensure_ascii=False)
        return json.dumps(clean, ensure_ascii=False) if clean != decoded else encoded

    # Decode only complete JSON string literals, preserving quote escapes in
    # ordinary values. A blind backslash replacement can expose secret suffixes.
    value = _JSON_STRING.sub(redact_encoded_string, value)
    # Fail closed when truncation/malformed serialization left an escaped secret
    # assignment that could not be inspected as a complete JSON string.
    for match in _ESCAPED_ASSIGNMENT.finditer(value):
        if len(match.group("key")) > MAX_KEY_CHARS or _sensitive_key(
            match.group("key")
        ):
            value = value[: match.end()] + REDACTED
            break
    value = _PEM.sub(REDACTED, value)
    value = _URL.sub(
        lambda match: _redact_url(match, truncated and match.end() == len(value)), value
    )
    value = _redact_assignments(value)
    value = _AUTH.sub(lambda match: match.group(1) + " " + REDACTED, value)
    value = _JWT.sub(REDACTED, value)
    value = _API_TOKEN.sub(REDACTED, value)
    value, output_truncated = _clip(value)
    return value, truncated or output_truncated


def sanitize_payload(value: object) -> tuple[object, bool]:
    """Return a bounded JSON-safe copy; never mutate or stringify the input.

    Collections have at most 64 members, paths at most 8 containers, and the walk
    visits at most 256 values. The aggregate JSON output and scanned text are
    limited to 32 KiB. Oversized keys redact their values as a precaution. Cycles
    and limit-exceeding values are omitted/replaced and report truncation.
    """
    remaining = MAX_NODES
    remaining_bytes = MAX_PAYLOAD_BYTES
    remaining_scan = MAX_PAYLOAD_BYTES
    exhausted = False
    truncated = False
    active: set[int] = set()

    def secret_name(item: object) -> bool:
        return type(item) is str and (
            len(cast(str, item)) > MAX_KEY_CHARS or _sensitive_key(cast(str, item))
        )

    def scalar(item: object) -> object:
        nonlocal remaining_bytes, exhausted, truncated
        size = len(json.dumps(item, ensure_ascii=True))
        if size > remaining_bytes:
            truncated = exhausted = True
            item = TRUNCATED
            size = len(TRUNCATED) + 2
        remaining_bytes -= size
        return item

    def visit(item: object, depth: int) -> object:
        nonlocal remaining, remaining_bytes, remaining_scan, exhausted, truncated
        if remaining == 0:
            truncated = True
            return scalar(TRUNCATED)
        remaining -= 1
        item_type = type(item)
        if item_type is str:
            text = cast(str, item)
            scan_size = min(len(text), MAX_TEXT_BYTES)
            if scan_size > remaining_scan:
                truncated = exhausted = True
                return scalar(TRUNCATED)
            remaining_scan -= scan_size
            clean, clipped = sanitize_text(text)
            truncated |= clipped
            return scalar(clean)
        if item is None or item_type is bool:
            return scalar(item)
        if item_type is int:
            if cast(int, item).bit_length() > 1024:
                truncated = True
                return scalar(TRUNCATED)
            return scalar(item)
        if item_type is float:
            return scalar(item if math.isfinite(cast(float, item)) else None)
        if item_type not in (dict, list, tuple):
            return scalar(UNSUPPORTED)
        if depth >= MAX_DEPTH or id(item) in active:
            truncated = True
            return scalar(TRUNCATED)
        active.add(id(item))
        remaining_bytes -= 2  # Reserve the opening and closing delimiters.
        try:
            if item_type is dict:
                result: dict[str, object] = {}
                # Common HTTP header/environment record shapes carry the name
                # and the actual secret in separate fields.
                named_secret = len(cast(dict, item)) > MAX_COLLECTION_ITEMS
                for index, (key, child) in enumerate(cast(dict, item).items()):
                    if index >= MAX_COLLECTION_ITEMS:
                        break
                    if (
                        type(key) is str
                        and len(key) <= MAX_KEY_CHARS
                        and _normalized_key(key) in {"name", "key", "header"}
                        and secret_name(child)
                    ):
                        named_secret = True
                        break
                for index, (key, child) in enumerate(cast(dict, item).items()):
                    if index >= MAX_COLLECTION_ITEMS or remaining == 0 or exhausted:
                        truncated = True
                        break
                    if type(key) is not str:
                        continue
                    scan_size = min(len(key), MAX_KEY_CHARS)
                    if scan_size > remaining_scan:
                        truncated = exhausted = True
                        break
                    remaining_scan -= scan_size
                    long_key = len(key) > MAX_KEY_CHARS
                    clean_key, clipped = sanitize_text(key[:MAX_KEY_CHARS])
                    truncated |= long_key or clipped
                    # Include ': ' and ', ', conservatively even for first keys.
                    key_size = len(json.dumps(clean_key, ensure_ascii=True)) + 4
                    if remaining_bytes < key_size + len(TRUNCATED) + 2:
                        truncated = exhausted = True
                        break
                    remaining_bytes -= key_size
                    if (
                        long_key
                        or _sensitive_key(key)
                        or (
                            named_secret
                            and _normalized_key(key) in {"value", "values", "val"}
                        )
                    ):
                        remaining -= 1
                        result[clean_key] = scalar(REDACTED)
                    else:
                        result[clean_key] = visit(child, depth + 1)
                return result
            result_list: list[object] = []
            sequence = cast(list | tuple, item)
            named_secret = len(sequence) == 2 and secret_name(sequence[0])
            for index, child in enumerate(sequence):
                if (
                    index >= MAX_COLLECTION_ITEMS
                    or remaining == 0
                    or exhausted
                    or remaining_bytes < len(TRUNCATED) + 4
                ):
                    truncated = True
                    break
                remaining_bytes -= 2  # Reserve ', ', including first values.
                if named_secret and index == 1:
                    remaining -= 1
                    result_list.append(scalar(REDACTED))
                else:
                    result_list.append(visit(child, depth + 1))
            return result_list
        finally:
            active.remove(id(item))

    result = visit(value, 0)
    return result, truncated
