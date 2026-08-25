from __future__ import annotations

import html
import re
import traceback
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

_ModelT = TypeVar("_ModelT", bound=BaseModel)

_BEARER = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_ASSIGNMENT = re.compile(
    r'''(?i)\b([a-z][a-z0-9_-]*)(\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s,;{}]+)'''
)
_JSON_ASSIGNMENT = re.compile(
    r'''(?i)(["']?([a-z][a-z0-9_-]*)["']?\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s,;{}]+)'''
)
_IDENTIFIER_ASSIGNMENT = re.compile(
    r'''(?i)\b((?:(?:account|customer|session|user)\s+id|user)["']?\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s,;{}]+)'''
)
_ENCODED_ASSIGNMENT = re.compile(
    r"(?i)\b([a-z][a-z0-9_-]*)(?:%3a|%3d)(?:%22|%27)?[^\s,;&]+"
)
_CREDENTIAL_URL = re.compile(
    r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@[^\s]+"
)
_QUERY_URL = re.compile(r"(?i)\bhttps?://[^\s?]+\?[^\s]+")
_PROVIDER_CREDENTIAL = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"sk-(?:proj-|ant-(?:api\d{2}-)?)?[A-Za-z0-9_-]{20,}"
    r"|gsk_[A-Za-z0-9_-]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|AIza[A-Za-z0-9_-]{20,}"
    r"|AKIA[A-Z0-9]{16}"
    r")(?![A-Za-z0-9])"
)
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_WINDOWS_PATH = re.compile(r"(?i)(?<![\w])\b[A-Z]:\\(?:[^\s\\]+\\)*[^\s\\]*")
_POSIX_PATH = re.compile(r"(?<![:/\w])/(?:[^/\s]+/)+[^\s,;]+")
_MARKDOWN = re.compile(r"([\\`*_{}\[\]()#+!|>])")
_CAMEL_LOWER_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_CAMEL_ACRONYM_BOUNDARY = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_SENSITIVE_SEGMENTS = {
    "ccv",
    "cvv",
    "authorization",
    "expires",
    "cookie",
    "credential",
    "credentials",
    "firstname",
    "lastname",
    "passwd",
    "password",
    "path",
    "phone",
    "pwd",
    "secret",
    "token",
    "uri",
    "username",
    "url",
}
_SENSITIVE_COMPOUNDS = {
    "accesskey",
    "accountid",
    "apikey",
    "baseurl",
    "cardnumber",
    "customerid",
    "longnum",
    "privatekey",
    "sessionid",
    "userid",
}

_FAILURE_LABELS = {
    "authentication": "provider authentication failed",
    "cancelled": "operation cancelled",
    "contract_integrity": "execution contract integrity failed",
    "invalid_output": "provider output invalid",
    "not_configured": "provider not configured",
    "provider_failure": "provider collection failed",
    "quota": "provider quota exceeded",
    "rate_limit": "provider rate limited",
    "skipped": "provider not configured",
    "timeout": "provider timeout",
    "transport": "provider transport failed",
}


class UnsafePersistenceValue(ValueError):
    """表示写入 payload 仍含可被安全边界识别的敏感值。"""


def safe_exception_diagnostic(
    exc: BaseException, repository_root: Path
) -> dict[str, str]:
    """返回可写入日志的异常类型、脱敏摘要和仓库相对位置。"""
    text = " ".join(redact_text(str(exc)).split())
    if len(text) > 256:
        marker = text.find(" validation error")
        if marker != -1:
            text = f"{text[:64]} ... {text[marker:marker + 186]}"
        else:
            text = f"{text[:187]} ... {text[-64:]}"
    location = "unknown"
    for frame in reversed(traceback.extract_tb(exc.__traceback__)):
        try:
            relative = Path(frame.filename).resolve().relative_to(repository_root.resolve())
        except ValueError:
            continue
        location = f"{relative.as_posix()}:{frame.lineno}"
        break
    return {
        "exception_type": type(exc).__name__[:128],
        "message": text,
        "location": location,
    }


def redact_text(value: str) -> str:
    """移除文本中的凭据、个人标识和本机路径。"""
    value = _CREDENTIAL_URL.sub("[REDACTED_URL]", value)
    value = _QUERY_URL.sub("[REDACTED_URL]", value)
    value = _PROVIDER_CREDENTIAL.sub("[REDACTED]", value)
    value = _BEARER.sub("[REDACTED]", value)
    value = _ENCODED_ASSIGNMENT.sub(_redact_encoded_assignment, value)
    value = _IDENTIFIER_ASSIGNMENT.sub(_redact_identifier_assignment, value)
    value = _JSON_ASSIGNMENT.sub(_redact_json_assignment, value)
    value = _ASSIGNMENT.sub(_redact_assignment, value)
    value = _EMAIL.sub("[REDACTED_EMAIL]", value)
    value = _WINDOWS_PATH.sub("[REDACTED_PATH]", value)
    return _POSIX_PATH.sub("[REDACTED_PATH]", value)


def redact_value(value: Any) -> Any:
    """递归脱敏 JSON-compatible 数据，同时保留原有结构。"""
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _is_sensitive_key(str(key)) else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_model(model: _ModelT) -> _ModelT:
    """创建脱敏后的同类型 Pydantic model，不修改 repository 中的历史对象。"""
    return type(model).model_validate(redact_value(model.model_dump(mode="python")))


def escape_markdown(value: object) -> str:
    """将动态值压成单行并转义 HTML 与 Markdown 控制字符。"""
    text = redact_text(str(value))
    text = html.escape(" ".join(text.split()), quote=True)
    return _MARKDOWN.sub(r"\\\1", text)


def escape_markdown_code(value: object) -> str:
    """转义 code span 边界，同时保留 ID 和 enum 的可读形式。"""
    text = html.escape(" ".join(redact_text(str(value)).split()), quote=True)
    return text.replace("\\", "\\\\").replace("`", "\\`")


def safe_failure(category: str) -> str:
    """只根据稳定分类生成外部失败说明，不解析异常文本。"""
    return _FAILURE_LABELS.get(category, "operation failed")


def assert_safe_value(value: Any) -> None:
    """拒绝仍需脱敏的持久化 payload，避免只依赖最终展示层。"""
    if redact_value(value) != value:
        raise UnsafePersistenceValue("payload contains unsafe dynamic data")


def assert_safe_label(value: str) -> None:
    """拒绝凭据形态的外部标签，避免 secret/token/key 被当成元数据。"""
    assert_safe_value(value)
    segments = {
        item.lower() for item in re.split(r"[^a-zA-Z0-9]+", value) if item
    }
    if _is_sensitive_key(value) or "key" in segments:
        raise UnsafePersistenceValue("payload contains unsafe dynamic data")


def _redact_assignment(match: re.Match[str]) -> str:
    key = match.group(1)
    if not _is_sensitive_key(key):
        return match.group(0)
    return f"{key}{match.group(2)}[REDACTED]"


def _redact_identifier_assignment(match: re.Match[str]) -> str:
    return f"{match.group(1)}[REDACTED]"


def _redact_json_assignment(match: re.Match[str]) -> str:
    if not _is_sensitive_key(match.group(2)):
        return match.group(0)
    return f"{match.group(1)}[REDACTED]"


def _redact_encoded_assignment(match: re.Match[str]) -> str:
    return "[REDACTED]" if _is_sensitive_key(match.group(1)) else match.group(0)


def _is_sensitive_key(key: str) -> bool:
    normalized = _CAMEL_ACRONYM_BOUNDARY.sub("_", key)
    normalized = _CAMEL_LOWER_BOUNDARY.sub("_", normalized)
    segments = [
        segment.lower()
        for segment in re.split(r"[^a-zA-Z0-9]+", normalized)
        if segment
    ]
    if set(segments) & _SENSITIVE_SEGMENTS:
        return True
    compact = "".join(segments)
    return any(
        compact == marker or compact.endswith(marker)
        for marker in _SENSITIVE_COMPOUNDS
    )
