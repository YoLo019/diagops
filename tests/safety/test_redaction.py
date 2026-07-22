import json

import pytest

from backend.db.models import InvestigationRecord
from backend.db.serialization import record_to_rows
from backend.domain.events import IncidentEvent
from backend.safety.redaction import (
    UnsafePersistenceValue,
    escape_markdown,
    redact_text,
    redact_value,
    safe_failure,
)
from backend.services.incident_cases import load_incident_case


@pytest.mark.parametrize(
    ("value", "marker"),
    [
        ("Authorization: Bearer token-value", "[REDACTED]"),
        ('api_key="quoted-secret"', "[REDACTED]"),
        ("password%3Dencoded-secret", "[REDACTED]"),
        ("postgres://user:pass@db.internal/app", "[REDACTED_URL]"),
        ("https://example.invalid/api?token=query-secret", "[REDACTED_URL]"),
        (r"C:\\private\\incident.log", "[REDACTED_PATH]"),
        ("/var/lib/private/incident.log", "[REDACTED_PATH]"),
        ("owner@example.com", "[REDACTED_EMAIL]"),
        ("sk-proj-abcdefghijklmnopqrstuvwxyz123456", "[REDACTED]"),
        ("sk-abcdefghijklmnopqrstuvwxyz123456", "[REDACTED]"),
    ],
)
def test_redact_text_removes_sensitive_forms(value: str, marker: str) -> None:
    redacted = redact_text(value)

    assert marker in redacted
    assert "secret" not in redacted
    assert "token-value" not in redacted
    assert "owner@example.com" not in redacted


def test_redact_value_handles_nested_and_split_key_values() -> None:
    value = {
        "headers": {"authorization": "Bearer nested-secret"},
        "apiKey": "split-secret",
        "items": [{"path": "/srv/private/data.json"}, "user@example.com"],
    }

    redacted = redact_value(value)

    assert redacted["headers"]["authorization"] == "[REDACTED]"
    assert redacted["apiKey"] == "[REDACTED]"
    assert redacted["items"][0]["path"] == "[REDACTED]"
    assert redacted["items"][1] == "[REDACTED_EMAIL]"
    assert "secret" not in json.dumps(redacted)


def test_escape_markdown_redacts_and_neutralizes_injected_heading() -> None:
    rendered = escape_markdown("## forged\nBearer hidden-token *bold*")

    assert "Bearer hidden-token" not in rendered
    assert "## forged" not in rendered
    assert r"\#\# forged" in rendered
    assert r"\*bold\*" in rendered


def test_safe_failure_uses_only_stable_category() -> None:
    assert safe_failure("timeout") == "provider timeout"
    assert safe_failure("unknown-value Bearer secret") == "operation failed"


def test_record_serialization_refuses_unredacted_dynamic_data() -> None:
    event = load_incident_case("deployment_regression").model_copy(
        update={"description": "Bearer persistence-secret"}
    )
    record = InvestigationRecord(event=event)

    with pytest.raises(UnsafePersistenceValue):
        record_to_rows(record)


def test_redacted_event_can_be_serialized() -> None:
    event = IncidentEvent.model_validate(
        redact_value(
            load_incident_case("deployment_regression")
            .model_copy(update={"description": "Bearer persistence-secret"})
            .model_dump(mode="python")
        )
    )

    rows = record_to_rows(InvestigationRecord(event=event))

    assert "persistence-secret" not in json.dumps(rows)
