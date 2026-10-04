"""Presentation redaction must be bounded, JSON-safe and never expose secrets."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from eng_platform_api.services.log_redaction import (
    MAX_COLLECTION_ITEMS,
    MAX_DEPTH,
    MAX_NODES,
    MAX_PAYLOAD_BYTES,
    MAX_TEXT_BYTES,
    REDACTED,
    TRUNCATED,
    UNSUPPORTED,
    sanitize_payload,
    sanitize_text,
)


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "PASSWORD",
        "dbPassword",
        "password_hash",
        "passwd",
        "DB_PWD",
        "api_key",
        "apiKey",
        "x-api-key",
        "X-Goog-Api-Key",
        "accessToken",
        "refresh_token",
        "token",
        "authorization",
        "Proxy-Authorization",
        "auth",
        "cookie",
        "Set-Cookie",
        "privateKey",
        "private-key",
        "credentials",
        "clientSecret",
        "AWS_SECRET_ACCESS_KEY",
        "sessionId",
        "signing_key",
        "ｐａｓｓｗｏｒｄ",
        "pass\u200bword",
        "authori\u202ezation",
    ],
)
def test_sensitive_keys_are_redacted_at_every_level(key):
    source = {"message": "healthy", "nested": [{key: {"raw": "DO_NOT_SHOW"}}]}
    before = deepcopy(source)
    result, truncated = sanitize_payload(source)
    assert "DO_NOT_SHOW" not in json.dumps(result)
    assert result["message"] == "healthy"
    assert REDACTED in json.dumps(result)
    assert truncated is False
    assert source == before


@pytest.mark.parametrize(
    "text, secrets",
    [
        ("Authorization: Bearer TOPSECRET", ["TOPSECRET"]),
        ("received bearer TOPSECRET", ["TOPSECRET"]),
        ("Basic dXNlcjpwYXNzd29yZA==", ["dXNlcjpwYXNzd29yZA=="]),
        ('{"password":"hunter two", "okay":true}', ["hunter two"]),
        ("password='hunter\\' two'", ["hunter", "two"]),
        ("password=hello world", ["hello", "world"]),
        ("DB_PASSWORD=hunter; action=read", ["hunter"]),
        ("clientSecret: hunter, action: read", ["hunter"]),
        ('credentials={"user":"someone", "password":"hunter"}', ["someone", "hunter"]),
        ("credentials=['hunter', {'secret': 'sensitive'}]", ["hunter", "sensitive"]),
        ("Cookie: sid=hunter; flavor=chocolate\nstatus=ok", ["hunter", "chocolate"]),
        ('password="unterminated hunter', ["hunter"]),
        ('credentials={"password":"unterminated hunter"', ["hunter"]),
        ("credentials=[hunter}", ["hunter"]),
        ("credentials={hunter]", ["hunter"]),
        ("api-key=VERYSECRET", ["VERYSECRET"]),
        ("ｐａｓｓｗｏｒｄ=VERYSECRET", ["VERYSECRET"]),
        ("pass\u202eword=VERYSECRET", ["VERYSECRET"]),
        ("ghp_abcdefgh12345678", ["ghp_abcdefgh12345678"]),
        ("github_pat_abcdefgh12345678", ["github_pat_abcdefgh12345678"]),
        ("sk-proj-abcdefgh12345678", ["sk-proj-abcdefgh12345678"]),
        ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.signature", ["eyJ", "signature"]),
        (
            "-----BEGIN PRIVATE KEY-----\nPRIVATE_MATERIAL\n-----END PRIVATE KEY-----",
            ["PRIVATE_MATERIAL"],
        ),
        ("-----BEGIN RSA PRIVATE KEY-----\nPRIVATE_MATERIAL", ["PRIVATE_MATERIAL"]),
        (
            "-----BEGIN ENCRYPTED PRIVATE KEY-----\\nPRIVATE_MATERIAL",
            ["PRIVATE_MATERIAL"],
        ),
    ],
)
def test_secret_values_are_removed_from_text(text, secrets):
    result, truncated = sanitize_text(text)
    assert REDACTED in result
    assert all(secret not in result for secret in secrets)
    assert truncated is False


@pytest.mark.parametrize(
    "url",
    [
        "https://user:hunter@example.test/path?token=VERYSECRET&safe=yes",
        "postgresql://user:hunter@example.test/db?password=VERYSECRET",
        "https://example.test/?api%5Fkey=VERYSECRET&safe=yes",
        "https://example.test/?api%255Fkey=VERYSECRET&safe=yes",
        "https://example.test/#access_token=VERYSECRET&safe=yes",
        "https://user:hunter@[malformed/path?token=VERYSECRET&safe=yes",
        "https://example.test/?safe=yes;clientSecret=VERYSECRET",
    ],
)
def test_urls_remove_userinfo_and_encoded_sensitive_query_values(url):
    result, truncated = sanitize_text("request " + url)
    assert "hunter" not in result
    assert "VERYSECRET" not in result
    assert REDACTED in result
    assert "example.test" in result or "malformed" in result
    assert truncated is False


def test_redaction_removes_control_sequences_but_preserves_readable_unicode():
    text = "\x1b[31mHola José 東京 😃\x1b[0m\x1b]0;hidden title\x07\x00\x7f\x85\u202e\u2066\u200b\r\n\tfin"
    result, truncated = sanitize_text(text)
    assert result == "Hola José 東京 😃\n\tfin"
    assert truncated is False


def test_unsupported_objects_never_invoke_conversion_or_container_hooks():
    class Dangerous:
        def __str__(self):
            raise AssertionError("must not stringify untrusted objects")

        def __repr__(self):
            raise AssertionError("must not repr untrusted objects")

    class DangerousDict(dict):
        def items(self):
            raise AssertionError("must not invoke custom container methods")

    class DangerousStr(str):
        def __getitem__(self, key):
            raise AssertionError("must not invoke custom text methods")

    obj = Dangerous()
    value = [obj, b"password=DO_NOT_SHOW", DangerousDict(), DangerousStr("secret")]
    assert sanitize_payload(value) == ([UNSUPPORTED] * 4, False)
    assert sanitize_text(obj) == (UNSUPPORTED, False)
    assert sanitize_payload({obj: "DO_NOT_SHOW", "safe": "ok"}) == (
        {"safe": "ok"},
        False,
    )


def test_payload_normalizes_non_finite_floats_and_tuples_to_json():
    result, truncated = sanitize_payload(
        (None, True, 10, 2.5, float("nan"), float("inf"))
    )
    assert result == [None, True, 10, 2.5, None, None]
    assert json.loads(json.dumps(result, allow_nan=False)) == result
    assert truncated is False


@pytest.mark.parametrize("character", ["a", "é", "😃"])
def test_text_limit_is_utf8_bytes_and_safely_handles_giant_sources(character):
    result, truncated = sanitize_text(character * (MAX_TEXT_BYTES * 10))
    assert len(result.encode("utf-8")) <= MAX_TEXT_BYTES
    assert result and set(result) == {character}
    assert truncated is True


def test_exact_text_limit_is_not_truncation():
    assert sanitize_text("a" * MAX_TEXT_BYTES) == ("a" * MAX_TEXT_BYTES, False)


def test_secret_is_redacted_even_when_cutoff_splits_its_value():
    result, truncated = sanitize_text("password=" + "hunter" * MAX_TEXT_BYTES)
    assert result == "password=" + REDACTED
    assert truncated is True


def test_redaction_growth_still_respects_output_limit():
    result, truncated = sanitize_text("token=x;" * 1000)
    assert len(result.encode("utf-8")) <= MAX_TEXT_BYTES
    assert "=x" not in result
    assert truncated is True


@pytest.mark.parametrize(
    "container", [list(range(1000)), {str(i): i for i in range(1000)}]
)
def test_collection_limit(container):
    result, truncated = sanitize_payload(container)
    assert len(result) == MAX_COLLECTION_ITEMS
    assert truncated is True


def test_exact_collection_limit_is_not_truncation():
    value = list(range(MAX_COLLECTION_ITEMS))
    assert sanitize_payload(value) == (value, False)


def test_deep_payload_and_cycles_are_bounded():
    nested = "END"
    for _ in range(1000):
        nested = [nested]
    result, truncated = sanitize_payload(nested)
    assert truncated is True
    for _ in range(MAX_DEPTH):
        result = result[0]
    assert result == TRUNCATED
    cycle = []
    cycle.append(cycle)
    assert sanitize_payload(cycle) == ([TRUNCATED], True)
    cycle_dict = {}
    cycle_dict["self"] = cycle_dict
    assert sanitize_payload(cycle_dict) == ({"self": TRUNCATED}, True)


def test_shared_but_acyclic_objects_are_not_misclassified():
    child = {"okay": 1}
    assert sanitize_payload([child, child]) == ([{"okay": 1}, {"okay": 1}], False)


def test_node_limit_is_global_across_collections():
    value = [[i] * MAX_COLLECTION_ITEMS for i in range(MAX_COLLECTION_ITEMS)]
    result, truncated = sanitize_payload(value)

    def node_count(item):
        return (
            1 + sum(node_count(child) for child in item)
            if isinstance(item, list)
            else 1
        )

    assert node_count(result) <= MAX_NODES
    assert truncated is True


def test_node_limit_also_counts_redacted_values():
    value = [{f"token{i}": "DO_NOT_SHOW" for i in range(64)} for _ in range(64)]
    result, truncated = sanitize_payload(value)
    assert "DO_NOT_SHOW" not in json.dumps(result)
    assert 1 + len(result) + sum(len(child) for child in result) <= MAX_NODES
    assert truncated is True


@pytest.mark.parametrize("character", ["x", "é", "😃", "\n", '"'])
def test_aggregate_payload_budget_includes_json_escaping(character):
    source = {str(index): character * MAX_TEXT_BYTES for index in range(64)}
    result, truncated = sanitize_payload(source)
    assert len(json.dumps(result).encode("utf-8")) <= MAX_PAYLOAD_BYTES
    assert (
        len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= MAX_PAYLOAD_BYTES
    )
    assert truncated is True


def test_scanning_budget_also_bounds_heavily_redacted_text(monkeypatch):
    import eng_platform_api.services.log_redaction as module

    calls = 0
    original = module.sanitize_text

    def counted(text):
        nonlocal calls
        calls += 1
        return original(text)

    monkeypatch.setattr(module, "sanitize_text", counted)
    result, truncated = module.sanitize_payload(
        ["password=" + "s" * MAX_TEXT_BYTES] * 64
    )
    assert calls <= MAX_PAYLOAD_BYTES // MAX_TEXT_BYTES
    assert truncated is True
    assert "ssss" not in json.dumps(result)


def test_aggregate_budget_includes_unicode_dictionary_keys():
    source = [{str(i) + "😃" * 200: "a" for i in range(64)} for _ in range(64)]
    result, truncated = sanitize_payload(source)
    assert len(json.dumps(result).encode("utf-8")) <= MAX_PAYLOAD_BYTES
    assert truncated is True


def test_huge_keys_and_integers_do_not_escape_resource_limits():
    result, truncated = sanitize_payload(
        {"x" * 10000: "DO_NOT_SHOW", "number": 1 << 10000}
    )
    assert "DO_NOT_SHOW" not in json.dumps(result)
    assert len(next(iter(result))) == 256
    assert result["number"] == TRUNCATED
    assert truncated is True


def test_empty_values_and_benign_assignments_are_safe():
    assert sanitize_payload({}) == ({}, False)
    assert sanitize_payload([]) == ([], False)
    assert sanitize_text("") == ("", False)
    assert sanitize_text("status=okay; code: 200") == ("status=okay; code: 200", False)
    assert sanitize_text("password=") == ("password=" + REDACTED, False)


def test_isolated_surrogates_cannot_break_utf8_sanitization():
    value, truncated = sanitize_text("hello\ud800world")
    assert value == "helloworld"
    value.encode("utf-8")
    assert truncated is False
    assert "synth-sentinel" not in sanitize_text("pass\ud800word=synth-sentinel")[0]


def test_sanitization_does_not_persist_data_or_import_logging():
    # Module exposes pure functions rather than a writer or configured logger.
    import eng_platform_api.services.log_redaction as module

    assert "logging" not in vars(module)
    assert "os" not in vars(module)


@pytest.mark.parametrize(
    "text",
    [
        "password:\nsecret-value",
        "password: |\n  secret-value\n  more-secret",
        "password: >\n  secret-value",
        '{"api key":"secret-value"}',
        "'private key': 'secret-value'",
    ],
)
def test_multiline_assignments_and_quoted_spaced_keys(text):
    result, truncated = sanitize_text(text)
    assert "secret-value" not in result
    assert "more-secret" not in result
    assert REDACTED in result
    assert truncated is False


@pytest.mark.parametrize(
    "headers",
    [
        [{"name": "Authorization", "value": "secret-value"}],
        [{"Name": "X-Api-Key", "Value": "secret-value"}],
        [{"header": "Cookie", "values": ["secret-value"]}],
        [{"key": "DB_PASSWORD", "val": "secret-value"}],
        [["Authorization", "secret-value"]],
        [("Authorization", "secret-value")],
    ],
)
def test_header_name_value_records_and_pair_arrays(headers):
    result, truncated = sanitize_payload({"headers": headers})
    assert "secret-value" not in json.dumps(result)
    assert REDACTED in json.dumps(result)
    assert truncated is False


@pytest.mark.parametrize("key", ["password", "authorization", "api key"])
@pytest.mark.parametrize("depth", [1, 2, 3, 5])
def test_serialized_json_strings_hide_secret_values(key, depth):
    text = {key: "synth-sentinel"}
    for _ in range(depth):
        text = json.dumps(text)
    result, truncated = sanitize_text(text)
    assert "synth-sentinel" not in result
    assert REDACTED in result
    assert truncated is False


@pytest.mark.parametrize(
    "text",
    [
        'request body="{\\"password\\":\\"synth-sentinel\\"}"',
        '{"body":"{\\"password\\":\\"synth-sentinel\\"}"}',
        '"\\u0070assword=\\u0073ynth-sentinel"',
        '{\\"password\\":\\"synth-sentinel',
        '"password": "secret\\"synth-sentinel"',
        "Bearer\nsynth-sentinel",
    ],
)
def test_escaped_embedded_and_malformed_secret_text(text):
    result, truncated = sanitize_text(text)
    assert "synth-sentinel" not in result
    assert REDACTED in result
    assert truncated is False


def test_benign_escaped_quotes_do_not_change_or_leak_following_secret_suffixes():
    value = '{"message":"say \\"hello\\""}'
    result, _ = sanitize_text(value)
    assert json.loads(result) == json.loads(value)


def test_encoded_string_inspection_catches_non_json_literals_without_raising():
    result, truncated = sanitize_text('"bad\\q"')
    assert result == '"bad\\q"'
    assert truncated is False


@pytest.mark.parametrize(
    "key",
    [
        "sig",
        "signature",
        "X-Amz-Signature",
        "X-Goog-Signature",
        "jwt",
        "session",
        "code",
    ],
)
def test_signed_url_credentials_and_oauth_codes_are_hidden(key):
    result, truncated = sanitize_text(
        f"https://example.test/path?{key}=synth-sentinel&safe=yes"
    )
    assert "synth-sentinel" not in result
    assert "safe=yes" in result
    assert truncated is False
    assert sanitize_payload({"code": 200}) == ({"code": 200}, False)


@pytest.mark.parametrize(
    "text",
    [
        r'{"\u0070assword":"synth-sentinel"}',
        r'{"api\u0020key":"synth-sentinel"}',
        r'prefix {"pass\u0077ord":"synth-sentinel"}',
        '{"headers":[{"name":"Authorization","value":"synth-sentinel"}]}',
        '{"headers":[["Authorization","synth-sentinel"]]}',
    ],
)
def test_json_text_uses_structural_redaction_and_decodes_escaped_keys(text):
    result, truncated = sanitize_text(text)
    assert "synth-sentinel" not in result
    assert REDACTED in result
    assert truncated is False


def test_deep_serialized_json_is_bounded_without_recursion_errors():
    result, truncated = sanitize_text("[" * 2000 + '"okay"' + "]" * 2000)
    assert len(result.encode("utf-8")) <= MAX_TEXT_BYTES
    # Malformed/deeper-than-Python-parser JSON still goes through bounded text.
    assert isinstance(truncated, bool)


@pytest.mark.parametrize(
    "key", ["password" + "a" * 250, "a" * 250 + "password", "a" * 258]
)
def test_long_assignment_keys_fail_closed(key):
    for text in (
        f"{key}=synth-sentinel",
        f'prefix "{key}":"synth-sentinel"',
        f"https://example.test/?{key}=synth-sentinel",
    ):
        result, truncated = sanitize_text(text)
        assert "synth-sentinel" not in result
        assert REDACTED in result
        assert truncated is False


@pytest.mark.parametrize(
    "userinfo", ["user:synth-sentinel", "user:123456789", "synth-sentinel"]
)
def test_truncated_url_authority_never_reveals_partial_userinfo(userinfo):
    result, truncated = sanitize_text(
        "https://" + userinfo * MAX_TEXT_BYTES + "@example.test/"
    )
    assert result == "https://" + REDACTED
    assert truncated is True


def test_truncated_header_record_without_visible_name_fails_closed():
    record = {
        "value": "synth-sentinel",
        **{f"field{i}": i for i in range(64)},
        "name": "Authorization",
    }
    result, truncated = sanitize_payload(record)
    assert "synth-sentinel" not in json.dumps(result)
    assert truncated is True


def test_long_escape_runs_are_not_rescanned_at_each_backslash():
    import eng_platform_api.services.log_redaction as module

    # Only the first backslash in a run can start the escaped-assignment regex.
    # This makes failed matches linear rather than testing all quadratic suffixes.
    assert module._ESCAPED_ASSIGNMENT.pattern.startswith(r"(?<!\\)")
    value = "\\" * MAX_TEXT_BYTES
    assert sanitize_text(value) == (value, False)
    assert (
        "synth-sentinel" not in sanitize_text(r'{\\"password\\":\\"synth-sentinel')[0]
    )
