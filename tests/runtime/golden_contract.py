from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_GENERATED_KEYS = {"created_at", "updated_at", "completed_at", "duration_ms"}
_NULLABLE_V9_METADATA = {
    "execution_id",
    "idempotency_key",
    "logical_call_id",
    "runtime_run_id",
}
_HEADING = re.compile(r"(?m)^#{1,6}\s+(.+?)\s*$")
_STABLE_ID = re.compile(r"\b[A-Z][A-Z_]+_\d+\b")
_UNORDERED_MARKDOWN_SECTIONS = {"支持该结论的证据"}
_UNORDERED_MARKDOWN_FIELD_PREFIXES = ("- 证据：",)


def canonical_runtime_contract(raw: dict[str, Any]) -> dict[str, Any]:
    """保留业务引用并稳定化随机 ID；只移除生成时间和耗时。"""
    value = json.loads(json.dumps(raw, ensure_ascii=False))
    identifiers: dict[str, str] = {}
    counters: dict[str, int] = {}

    def register(item: Any, category: str) -> None:
        if not isinstance(item, dict):
            return
        identifier = item.get("id")
        if not isinstance(identifier, str) or identifier in identifiers:
            return
        counters[category] = counters.get(category, 0) + 1
        identifiers[identifier] = f"{category}_{counters[category]}"

    record = value.get("record", {})
    register(record, "INVESTIGATION")
    for key, category in (
        ("evidence", "EVIDENCE"),
        ("hypotheses", "HYPOTHESIS"),
        ("actions", "ACTION"),
        ("verification_suggestions", "VERIFICATION"),
    ):
        for item in record.get(key, []):
            register(item, category)
    report = record.get("report")
    if isinstance(report, dict):
        report.setdefault("id", "__baseline_report__")
        register(report, "REPORT")
    for key, category in (
        ("findings", "FINDING"),
        ("tasks", "TASK"),
        ("executions", "EXECUTION"),
        ("tool_calls", "TOOL_CALL"),
    ):
        for item in value.get(key, []):
            register(item, category)
    review = value.get("review")
    register(review, "REVIEW")
    if isinstance(review, dict):
        for item in review.get("candidates", []):
            register(item, "CANDIDATE")

    def register_remaining(item: Any) -> None:
        if isinstance(item, dict):
            register(item, "ENTITY")
            for child in item.values():
                register_remaining(child)
        elif isinstance(item, list):
            for child in item:
                register_remaining(child)

    register_remaining(value)
    replacements = sorted(identifiers.items(), key=lambda item: len(item[0]), reverse=True)

    def normalize(item: Any, path: tuple[str, ...] = ()) -> Any:
        if isinstance(item, dict):
            result = {}
            for key in sorted(item):
                if key in _GENERATED_KEYS:
                    continue
                if key in _NULLABLE_V9_METADATA and item[key] is None:
                    continue
                if key == "started_at" and path != ("record", "event"):
                    continue
                child_path = (*path, key)
                if key == "markdown" and path[-2:] == ("record", "report"):
                    result["markdown_sections"] = _markdown_section_hashes(
                        _replace_text(str(item[key]), replacements)
                    )
                    continue
                result[key] = normalize(item[key], child_path)
            return result
        if isinstance(item, list):
            normalized = [normalize(child, path) for child in item]
            if path and path[-1].endswith("_ids"):
                return sorted(normalized)
            return normalized
        if isinstance(item, str):
            return _replace_text(item, replacements)
        return item

    return normalize(value)


def contract_checksum(contract: dict[str, Any]) -> str:
    encoded = json.dumps(
        contract, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _replace_text(value: str, replacements: list[tuple[str, str]]) -> str:
    for original, stable in replacements:
        value = value.replace(original, stable)
    return value


def _markdown_section_hashes(markdown: str) -> list[dict[str, str]]:
    matches = list(_HEADING.finditer(markdown))
    if not matches:
        normalized = " ".join(markdown.split())
        return [{"heading": "document", "sha256": _text_hash(normalized)}]
    sections = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        lines = [
            _normalize_markdown_line(line)
            for line in markdown[match.end() : end].splitlines()
            if line.strip()
        ]
        heading = " ".join(match.group(1).split())
        body = "\n".join(
            sorted(lines) if heading in _UNORDERED_MARKDOWN_SECTIONS else lines
        )
        sections.append(
            {"heading": heading, "sha256": _text_hash(body)}
        )
    return sections


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_markdown_line(value: str) -> str:
    value = " ".join(value.split())
    if not value.startswith(_UNORDERED_MARKDOWN_FIELD_PREFIXES):
        return value
    identifiers = sorted(_STABLE_ID.findall(value))
    iterator = iter(identifiers)
    return _STABLE_ID.sub(lambda _match: next(iterator), value)
