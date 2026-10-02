"""Oha-Yachiyo security helpers.

This package intentionally keeps the shared secret redaction rules small and
dependency-free so every persistence boundary can use the same scrubber.
"""

from __future__ import annotations

import ast
import logging
import os
import re
import sys
import traceback
from typing import Any

REDACTED = "[redacted]"
_LOG_RECORD_FACTORY_INSTALLED = False
_ORIGINAL_LOG_RECORD_FACTORY: Any = None
_EXCEPTHOOK_INSTALLED = False
_ORIGINAL_EXCEPTHOOK: Any = None

_SECRET_FIELD = (
    r"(?:[a-z0-9]+[_-])*(?:api[_-]?key|token|password|passwd|secret|authorization)"
)
# Provider errors and log messages often contain JSON, Python repr, or quoted
# environment assignments. Keep their quotes/delimiters intact and consume the
# entire escaped value, rather than leaking the tail after its first space.
_QUOTED_SECRET_PATTERNS = tuple(
    re.compile(
        rf"(?i)(?P<prefix>\b{_SECRET_FIELD}\b[\"']?\s*[:=]\s*)"
        rf"(?P<quote>{quote})(?P<secret>(?:\\.|[^{quote}\\])*)(?P=quote)"
    )
    for quote in ('"', "'")
)
_SOURCE_SECRET_LABEL_RE = re.compile(rf"(?i){_SECRET_FIELD}\s*[:=]\s*")
_SOURCE_STRING_OPERAND_PATTERNS = {
    quote: re.compile(rf"{quote}(?:\\.|[^{quote}\\\r\n])*{quote}")
    for quote in ('"', "'")
}
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = _QUOTED_SECRET_PATTERNS + (
    re.compile(
        r"(?i)\b(authorization)\b\s*[:=]\s*(?:bearer\s+)?([^\s,;\"']{6,})"
    ),
    re.compile(
        r"(?i)\b(api[_-]?key|token|password|passwd|secret)\b"
        r"\s*[:=]\s*([^\s,;\"']{6,})"
    ),
    re.compile(
        r"(?i)\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*_(?:API_KEY|TOKEN|SECRET|PASSWORD|PASSWD))"
        r"\s*=\s*([^\s,;\"']{6,})"
    ),
    re.compile(r"(?i)\b(bearer)\s+([A-Za-z0-9._\-]{8,})\b"),
    re.compile(r"\b(sk-[A-Za-z0-9._\-]{8,})\b"),
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9_]{12,})\b"),
    re.compile(r"\b(xox[baprs]-[A-Za-z0-9\-]{12,})\b"),
)
_SENSITIVE_ENV_KEY_RE = re.compile(
    r"(?i)(^SSH_AUTH_SOCK$|^GITHUB_TOKEN$|^(AWS|GOOGLE|AZURE)_|(_API_KEY|_TOKEN|_SECRET|_PASSWORD)$)"
)
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(api[_-]?key|token|password|passwd|secret|authorization|bearer)"
)
_SAFE_TOKEN_COUNT_KEYS = {
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "input_tokens",
    "output_tokens",
    "cached_tokens",
}
_TOOL_CALL_SNIPPET_RE = re.compile(r"<tool_call\b.*?</tool_call>", re.IGNORECASE | re.DOTALL)
_TOOL_CALL_TAIL_RE = re.compile(r"<tool_call\b.*", re.IGNORECASE | re.DOTALL)


def scrubbed_subprocess_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return process env without credentials that should not reach child commands."""

    env = {
        key: value
        for key, value in os.environ.items()
        if not _SENSITIVE_ENV_KEY_RE.search(key)
    }
    for key, value in (extra or {}).items():
        clean_key = str(key)
        if not clean_key or _SENSITIVE_ENV_KEY_RE.search(clean_key):
            continue
        env[clean_key] = str(value)
    return env


def redact_sensitive_text(
    value: Any,
    *,
    limit: int = 600,
    collapse_whitespace: bool = True,
    trim: bool = True,
    hide_tool_calls: bool = True,
) -> str:
    """Return text with common API keys/tokens/passwords removed.

    `limit <= 0` disables truncation. Chat persistence uses that mode to avoid
    changing normal transcript content while still preventing obvious secrets
    from entering storage.
    """

    text = str(value if value is not None else "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if trim:
        text = text.strip()
    if hide_tool_calls:
        text = redact_tool_call_markup(text)
    if collapse_whitespace:
        text = re.sub(r"\s+", " ", text)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_secret_replacement, text)
    if limit and limit > 0 and len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def contains_sensitive_text(value: Any, *, hide_tool_calls: bool = True) -> bool:
    """Return True when text still contains an obvious unredacted secret."""

    text = str(value if value is not None else "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if hide_tool_calls and "<tool_call" in text.lower():
        return True
    return any(
        _sensitive_match_is_unredacted(match)
        for pattern in _SECRET_PATTERNS
        for match in pattern.finditer(text)
    )


def sanitize_sensitive_value(
    value: Any,
    *,
    depth: int = 0,
    max_depth: int = 3,
    text_limit: int = 1800,
    key_limit: int = 80,
    max_items: int = 40,
    collapse_whitespace: bool = True,
    trim: bool = True,
) -> Any:
    """Recursively redact secrets in JSON-like payloads."""

    if depth > max_depth:
        return redact_sensitive_text(
            value,
            limit=min(text_limit, 160) if text_limit and text_limit > 0 else text_limit,
            collapse_whitespace=collapse_whitespace,
            trim=trim,
        )
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:max_items]:
            raw_key = str(key)
            key_text = redact_sensitive_text(
                raw_key,
                limit=key_limit,
                collapse_whitespace=collapse_whitespace,
                trim=trim,
            )
            if _SENSITIVE_KEY_RE.search(raw_key):
                if _is_safe_token_count_field(raw_key, item):
                    result[key_text] = item
                else:
                    result[key_text] = REDACTED
            else:
                result[key_text] = sanitize_sensitive_value(
                    item,
                    depth=depth + 1,
                    max_depth=max_depth,
                    text_limit=text_limit,
                    key_limit=key_limit,
                    max_items=max_items,
                    collapse_whitespace=collapse_whitespace,
                    trim=trim,
                )
        return result
    if isinstance(value, (list, tuple)):
        return [
            sanitize_sensitive_value(
                item,
                depth=depth + 1,
                max_depth=max_depth,
                text_limit=text_limit,
                key_limit=key_limit,
                max_items=max_items,
                collapse_whitespace=collapse_whitespace,
                trim=trim,
            )
            for item in list(value)[:max_items]
        ]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_sensitive_text(
        value,
        limit=text_limit,
        collapse_whitespace=collapse_whitespace,
        trim=trim,
    )


def _is_safe_token_count_field(key: str, value: Any) -> bool:
    if key.lower() not in _SAFE_TOKEN_COUNT_KEYS:
        return False
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def redact_tool_call_markup(text: str) -> str:
    if "<tool_call" not in text.lower():
        return text
    text = _TOOL_CALL_SNIPPET_RE.sub("[工具调用草稿已隐藏]", text)
    return _TOOL_CALL_TAIL_RE.sub("[工具调用草稿已隐藏]", text)


def redact_log_text(value: Any) -> str:
    return redact_sensitive_text(
        value,
        limit=0,
        collapse_whitespace=False,
        trim=False,
    )


def redact_api_error_text(value: Any, *, fallback: str = "") -> str:
    text = redact_sensitive_text(value, limit=1200)
    return text or fallback


def redact_api_error_detail(value: Any) -> Any:
    """Redact secrets before an error payload crosses the local HTTP bridge."""

    if isinstance(value, (dict, list, tuple)):
        return sanitize_sensitive_value(
            value,
            max_depth=4,
            text_limit=1200,
            max_items=80,
        )
    return redact_api_error_text(value)


def redact_log_record(record: logging.LogRecord) -> logging.LogRecord:
    try:
        message = record.getMessage()
    except Exception:
        message = str(record.msg)
    record.msg = redact_log_text(message)
    record.args = ()
    if record.exc_info:
        record.exc_text = format_redacted_exception(*record.exc_info)
    elif record.exc_text:
        record.exc_text = redact_log_text(record.exc_text)
    if record.stack_info:
        record.stack_info = redact_log_text(record.stack_info)
    return record


def install_logging_secret_redaction() -> None:
    """Install process-wide redaction for standard-library logging records."""

    global _LOG_RECORD_FACTORY_INSTALLED, _ORIGINAL_LOG_RECORD_FACTORY
    if _LOG_RECORD_FACTORY_INSTALLED:
        return
    original_factory = logging.getLogRecordFactory()
    _ORIGINAL_LOG_RECORD_FACTORY = original_factory

    def _factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        return redact_log_record(original_factory(*args, **kwargs))

    setattr(_factory, "_oha_yachiyo_secret_redaction", True)
    logging.setLogRecordFactory(_factory)
    _LOG_RECORD_FACTORY_INSTALLED = True


def format_redacted_exception(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_traceback: Any,
) -> str:
    return redact_log_text("".join(traceback.format_exception(exc_type, exc_value, exc_traceback)))


def install_secret_excepthook(*, stream: Any | None = None, force: bool = False) -> None:
    """Install a redacting excepthook for uncaught Python exceptions."""

    global _EXCEPTHOOK_INSTALLED, _ORIGINAL_EXCEPTHOOK
    if _EXCEPTHOOK_INSTALLED and not force:
        return
    if not _EXCEPTHOOK_INSTALLED:
        _ORIGINAL_EXCEPTHOOK = sys.excepthook

    def _hook(exc_type: type[BaseException], exc_value: BaseException, exc_traceback: Any) -> None:
        target = stream if stream is not None else sys.stderr
        text = format_redacted_exception(exc_type, exc_value, exc_traceback)
        target.write(text)
        if not text.endswith("\n"):
            target.write("\n")

    setattr(_hook, "_oha_yachiyo_secret_excepthook", True)
    sys.excepthook = _hook
    _EXCEPTHOOK_INSTALLED = True


def _is_source_secret_label_concatenation(match: re.Match[str]) -> bool:
    """Recognize a closing label quote followed by a source string addition.

    In ``"API_KEY=" + "part"``, the assignment regex mistakes the label's
    closing quote for a value's opening quote and captures `` + ``. Require
    two complete string literals and a pure key label before preserving it;
    an actual assignment such as ``API_KEY=" + "`` still gets redacted.
    """

    if match.group("secret").strip() != "+":
        return False
    prefix = match.group("prefix")
    if not _SOURCE_SECRET_LABEL_RE.fullmatch(prefix):
        return False
    quote = match.group("quote")
    label_start = match.start("prefix") - 1
    if label_start < 0 or match.string[label_start] != quote:
        return False
    if label_start > 0 and match.string[label_start - 1] == "\\":
        return False
    operand_start = match.end() - 1
    if operand_start - label_start > 256:
        return False
    operand = _SOURCE_STRING_OPERAND_PATTERNS[quote].match(
        match.string[operand_start : operand_start + 4096]
    )
    if operand is None:
        return False
    expression = match.string[label_start : operand_start + operand.end()]
    try:
        node = ast.parse(expression, mode="eval").body
    except (SyntaxError, ValueError, RecursionError):
        return False
    return (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Add)
        and isinstance(node.left, ast.Constant)
        and node.left.value == prefix
        and isinstance(node.right, ast.Constant)
        and isinstance(node.right.value, str)
    )


def _secret_replacement(match: re.Match[str]) -> str:
    if "secret" in match.re.groupindex:
        if not match.group("secret") or _is_source_secret_label_concatenation(match):
            return match.group(0)
        return f'{match.group("prefix")}{match.group("quote")}{REDACTED}{match.group("quote")}'
    if match.lastindex and match.lastindex > 1:
        return f"{match.group(1)}={REDACTED}"
    return REDACTED


def _sensitive_match_is_unredacted(match: re.Match[str]) -> bool:
    if "secret" in match.re.groupindex:
        secret = match.group("secret")
        return (
            bool(secret)
            and secret != REDACTED
            and not _is_source_secret_label_concatenation(match)
        )
    if match.lastindex and match.lastindex > 1:
        return not str(match.group(2) or "").strip().startswith(REDACTED)
    return str(match.group(0) or "").strip() != REDACTED
