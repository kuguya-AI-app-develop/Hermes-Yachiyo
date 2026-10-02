import io
import logging
import sys

import packages.security as security
from apps.desktop_backend import app as desktop_app
from scripts.verify_secret_redaction import verify_secret_redaction


def test_logging_secret_redaction_filters_formatted_messages():
    previous_factory = logging.getLogRecordFactory()
    previous_installed = security._LOG_RECORD_FACTORY_INSTALLED
    previous_original = security._ORIGINAL_LOG_RECORD_FACTORY
    stream = io.StringIO()
    logger = logging.getLogger("tests.security.logging")
    previous_handlers = list(logger.handlers)
    previous_propagate = logger.propagate
    previous_level = logger.level
    try:
        security._LOG_RECORD_FACTORY_INSTALLED = False
        security._ORIGINAL_LOG_RECORD_FACTORY = None
        logging.setLogRecordFactory(previous_factory)
        security.install_logging_secret_redaction()

        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)

        logger.error("OPENAI_API_KEY=%s", "sk-log-secret123456")
        output = stream.getvalue()

        assert "sk-log-secret123456" not in output
        assert "OPENAI_API_KEY=[redacted]" in output
    finally:
        logger.handlers = previous_handlers
        logger.propagate = previous_propagate
        logger.setLevel(previous_level)
        logging.setLogRecordFactory(previous_factory)
        security._LOG_RECORD_FACTORY_INSTALLED = previous_installed
        security._ORIGINAL_LOG_RECORD_FACTORY = previous_original


def test_redact_log_record_filters_exception_traceback():
    try:
        raise RuntimeError("token=traceback-token-123456")
    except RuntimeError:
        record = logging.LogRecord(
            "tests.security.exception",
            logging.ERROR,
            __file__,
            1,
            "failed password:%s",
            ("secret123456",),
            sys.exc_info(),
        )

    security.redact_log_record(record)
    message = record.getMessage()
    exc_text = record.exc_text or ""

    assert "secret123456" not in message
    assert "traceback-token-123456" not in exc_text
    assert "password=[redacted]" in message
    assert "token=[redacted]" in exc_text


def test_secret_excepthook_redacts_uncaught_exception_output():
    previous_hook = sys.excepthook
    previous_installed = security._EXCEPTHOOK_INSTALLED
    previous_original = security._ORIGINAL_EXCEPTHOOK
    stream = io.StringIO()
    try:
        security._EXCEPTHOOK_INSTALLED = False
        security._ORIGINAL_EXCEPTHOOK = None
        security.install_secret_excepthook(stream=stream, force=True)
        sys.excepthook(RuntimeError, RuntimeError("api_key=sk-crash-secret123456"), None)

        output = stream.getvalue()

        assert "sk-crash-secret123456" not in output
        assert "api_key=[redacted]" in output
    finally:
        sys.excepthook = previous_hook
        security._EXCEPTHOOK_INSTALLED = previous_installed
        security._ORIGINAL_EXCEPTHOOK = previous_original


def test_secret_excepthook_crash_file_passes_runtime_secret_scan(tmp_path):
    previous_hook = sys.excepthook
    previous_installed = security._EXCEPTHOOK_INSTALLED
    previous_original = security._ORIGINAL_EXCEPTHOOK
    crash_path = tmp_path / "backend.crash"
    leaked_secret = "sk-crash-file-secret123456"
    try:
        security._EXCEPTHOOK_INSTALLED = False
        security._ORIGINAL_EXCEPTHOOK = None
        with crash_path.open("w", encoding="utf-8") as stream:
            security.install_secret_excepthook(stream=stream, force=True)
            sys.excepthook(RuntimeError, RuntimeError(f"password={leaked_secret}"), None)

        crash_text = crash_path.read_text(encoding="utf-8")

        assert leaked_secret not in crash_text
        assert "password=[redacted]" in crash_text
        assert verify_secret_redaction(paths=[crash_path]) == []
    finally:
        sys.excepthook = previous_hook
        security._EXCEPTHOOK_INSTALLED = previous_installed
        security._ORIGINAL_EXCEPTHOOK = previous_original


def test_desktop_backend_setup_logging_installs_secret_redaction(monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr(security, "install_logging_secret_redaction", lambda: calls.append("logging"))
    monkeypatch.setattr(security, "install_secret_excepthook", lambda: calls.append("excepthook"))
    monkeypatch.setattr(desktop_app.logging, "basicConfig", lambda **_kwargs: calls.append("basicConfig"))

    desktop_app._setup_logging()

    assert calls == ["logging", "excepthook", "basicConfig"]


def test_api_error_text_redacts_secrets():
    output = security.redact_api_error_text("provider failed api_key=sk-api-error-secret123456")

    assert "sk-api-error-secret123456" not in output
    assert "api_key=[redacted]" in output


def test_api_error_detail_redacts_nested_payloads():
    output = security.redact_api_error_detail(
        {
            "error": {
                "message": "bad token=route-secret-123456",
                "api_key": "sk-nested-secret123456",
            }
        }
    )

    assert output["error"]["message"] == "bad token=[redacted]"
    assert output["error"]["api_key"] == "[redacted]"


def test_sanitize_sensitive_value_keeps_numeric_token_usage_counts():
    output = security.sanitize_sensitive_value(
        {
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
                "token": "provider-secret-123456",
            }
        }
    )

    assert output["usage"]["prompt_tokens"] == 11
    assert output["usage"]["completion_tokens"] == 7
    assert output["usage"]["total_tokens"] == 18
    assert output["usage"]["token"] == "[redacted]"


def test_redact_log_text_scrubs_quoted_json_secrets_without_breaking_json():
    import json

    payload = {
        "api_key": "demo-provider-123456",
        "password": 'pass with spaces and "quotes"',
        "authorization": "Bearer demo-auth-123456",
        "message": "ordinary text",
    }
    original = json.dumps(payload)

    assert security.contains_sensitive_text(original)
    redacted = security.redact_log_text(original)
    assert json.loads(redacted) == {
        "api_key": security.REDACTED,
        "password": security.REDACTED,
        "authorization": security.REDACTED,
        "message": "ordinary text",
    }
    assert not security.contains_sensitive_text(redacted)


def test_redact_log_text_scrubs_python_repr_and_quoted_environment_values():
    for original in (
        "{'password': 'demo-password-123456'}",
        "OPENAI_API_KEY='demo provider secret 123456'",
        'CUSTOM_ACCESS_TOKEN="demo-access-123456"',
        'OPENAI_API_KEY=demo-provider-123456',
        'custom_access_token=demo-provider-123456',
    ):
        assert security.contains_sensitive_text(original), original
        redacted = security.redact_log_text(original)
        assert "demo" not in redacted
        assert security.REDACTED in redacted
        assert not security.contains_sensitive_text(redacted)


def test_secret_scan_rejects_unredacted_quoted_provider_response(tmp_path):
    output = tmp_path / "provider-error.log"
    output.write_text('{"api_key": "demo-provider-123456"}\n', encoding="utf-8")

    assert verify_secret_redaction(paths=[output])
    output.write_text(security.redact_log_text(output.read_text()), encoding="utf-8")
    assert verify_secret_redaction(paths=[output]) == []


def test_redact_log_text_preserves_empty_secrets_and_token_usage():
    original = '{"api_key": "", "prompt_tokens": 12, "completion_tokens": 8}'

    assert security.redact_log_text(original) == original
    assert not security.contains_sensitive_text(original)
