import json
import shlex
import sys

import pytest

import packages.security as security
from apps.shell.agent.runtime.events import redact_json_value
from apps.shell.agent.runtime.tool_approvals import approval_request_fingerprint
from scripts.verify_secret_redaction import verify_secret_redaction


@pytest.mark.parametrize(
    "source",
    [
        'print("API_KEY=" + "sk-" + "part")',
        "print('API_KEY=' + 'sk-' + 'part')",
        'print("OPENAI_API_KEY=" + "sk-" + "part")',
        'print("api-key=" + "part")',
        'print("password=" + "part")',
        'print("authorization: " + "Bearer " + "part")',
        'print("API_KEY="+"part")',
        'print("API_KEY=" + "escaped \\\"quote\\\" and \\n")',
        'print("API_KEY=" + "")',
    ],
)
def test_source_key_label_string_addition_preserves_nonsecret_source(source):
    assert security.redact_sensitive_text(
        source, limit=0, collapse_whitespace=False, trim=False
    ) == source
    assert not security.contains_sensitive_text(source)


@pytest.mark.parametrize(
    "original",
    [
        'API_KEY=" + "',
        "API_KEY=' + '",
        'OPENAI_API_KEY=" + "',
        '{"api_key": " + "}',
        "{'password': ' + '}",
        '{"authorization": " + "}',
        'https://example.test/?api_key=" + "',
        "https://example.test/?password=' + '",
        'API_KEY=" + " + "source-tail"',
        "'API_KEY=\" + \"'",
        "\"API_KEY=' + '\"",
        'print("API_KEY=" + "unterminated)',
        'print("API_KEY=not-a-pure-label=" + "part")',
        'print(\\"API_KEY=" + "part")',
    ],
)
def test_real_quoted_assignments_and_nonliteral_contexts_still_redact(original):
    assert security.contains_sensitive_text(original)
    redacted = security.redact_sensitive_text(original, limit=0)
    assert security.REDACTED in redacted
    assert not security.contains_sensitive_text(redacted)


@pytest.mark.parametrize(
    "secret",
    [
        "sk-contiguous-secret123456789",
        "ghp_contiguoussecret123456789",
        "xoxb-contiguous-secret123456789",
        "Bearer contiguous-secret123456789",
    ],
)
def test_label_addition_does_not_exempt_real_tokens_in_source(secret):
    source = f'print("API_KEY=" + "{secret}")'
    assert security.contains_sensitive_text(source)
    redacted = security.redact_sensitive_text(source, limit=0)
    assert secret not in redacted
    assert security.REDACTED in redacted
    assert not security.contains_sensitive_text(redacted)


@pytest.mark.parametrize("key", ["api_key", "password", "authorization", "token"])
def test_sensitive_dictionary_keys_still_redact_label_looking_values(key):
    value = '"API_KEY=" + "part"'
    assert security.sanitize_sensitive_value({key: value}) == {key: security.REDACTED}


def test_source_concatenation_survives_approval_persistence_and_secret_scan(tmp_path):
    code = (
        'import sys; '
        'print("OPENAI_" + "API_KEY=" + "sk-" + "stdout-" + "secret123456789"); '
        'print("Author" + "ization: Bearer " + "stderr-token-" + "secret123456789", '
        'file=sys.stderr); '
        'sys.exit(7)'
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
    request = {"tool_name": "terminal_run", "input": {"command": command}}
    stored = redact_json_value(request)
    assert stored == request
    assert approval_request_fingerprint(stored) == approval_request_fingerprint(request)
    path = tmp_path / "pending.json"
    path.write_text(json.dumps(stored), encoding="utf-8")
    assert verify_secret_redaction(paths=[path]) == []


@pytest.mark.parametrize(
    "output",
    [
        "OPENAI_API_KEY=sk-stdout-secret123456789\n",
        "Authorization: Bearer stderr-token-secret123456789\n",
        'OPENAI_API_KEY=" + "\n',
    ],
)
def test_actual_sensitive_process_output_still_redacts(output):
    assert security.contains_sensitive_text(output)
    redacted = security.redact_sensitive_text(output, limit=0)
    assert security.REDACTED in redacted
    assert not security.contains_sensitive_text(redacted)
