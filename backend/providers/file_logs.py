import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.providers.results import ProviderResult
from backend.safety.redaction import redact_text

_ERROR_PATTERNS = {
    "ERROR": re.compile(r"\bERROR\b", re.IGNORECASE),
    "Exception": re.compile(r"Exception", re.IGNORECASE),
    "Traceback": re.compile(r"Traceback", re.IGNORECASE),
    "HTTP 500": re.compile(r"HTTP\s+500", re.IGNORECASE),
    "5xx": re.compile(r"\b5\d\d\b|\b5xx\b", re.IGNORECASE),
}

_TIMESTAMP_PREFIX = re.compile(r"^(?P<timestamp>\d{4}-\d{2}-\d{2}T\S+)")
MAX_LOG_BYTES = 5 * 1024 * 1024
MAX_LOG_LINES = 10_000
MAX_LOG_MATCHES = 100


class FileLogProvider:
    provider = EvidenceProvider.LOG

    def __init__(
        self,
        paths: list[Path],
        *,
        max_bytes: int = MAX_LOG_BYTES,
        max_lines: int = MAX_LOG_LINES,
        max_matches: int = MAX_LOG_MATCHES,
    ) -> None:
        self.paths = paths
        self.max_bytes = max_bytes
        self.max_lines = max_lines
        self.max_matches = max_matches

    def collect(self, event: IncidentEvent) -> ProviderResult:
        evidence: list[EvidenceItem] = []
        for path in self.paths:
            matches = self._matches_for_path(path, event)
            if not matches:
                continue

            patterns = sorted({pattern for match in matches for pattern in match.patterns})
            evidence.append(
                EvidenceItem(
                    provider=self.provider,
                    kind=EvidenceKind.LOG_PATTERN,
                    timestamp=matches[0].timestamp or event.started_at,
                    summary=(
                        f"{event.service} log file contains {len(matches)} error "
                        f"pattern matches"
                    ),
                    payload={
                        "source": "configured_log_file",
                        "service": event.service,
                        "environment": event.environment,
                        "error_count": len(matches),
                        "sample_lines": [
                            redact_text(match.line) for match in matches[:5]
                        ],
                        "patterns": patterns,
                    },
                    confidence=1.0,
                )
            )

        return ProviderResult(provider=self.provider, evidence_items=evidence)

    def _matches_for_path(self, path: Path, event: IncidentEvent) -> list["_LogMatch"]:
        if path.stat().st_size > self.max_bytes:
            raise ValueError("Log file exceeds size limit")
        window = timedelta(minutes=event.time_window_minutes)
        matches: list[_LogMatch] = []
        for line in path.read_text(encoding="utf-8").splitlines()[: self.max_lines]:
            parsed_at = _parse_timestamp(line)
            if parsed_at is None or abs(parsed_at - event.started_at) > window:
                continue
            if not _contains_scope(line, event.service) or not _contains_scope(
                line, event.environment
            ):
                continue

            patterns = [
                name for name, pattern in _ERROR_PATTERNS.items() if pattern.search(line)
            ]
            if patterns:
                matches.append(
                    _LogMatch(line=line, timestamp=parsed_at, patterns=patterns)
                )
                if len(matches) >= self.max_matches:
                    break
        return matches


@dataclass(frozen=True)
class _LogMatch:
    line: str
    timestamp: datetime | None
    patterns: list[str]


def _parse_timestamp(line: str) -> datetime | None:
    match = _TIMESTAMP_PREFIX.match(line)
    if match is None:
        return None

    value = match.group("timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _contains_scope(line: str, value: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(value)}(?![\w-])", line) is not None
