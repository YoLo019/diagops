import json

import pytest

from backend.db.models import InvestigationRecord
from backend.db.serialization import record_to_rows
from backend.domain.events import IncidentEvent
from backend.domain.reports import IncidentReport
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


def test_redact_text_masks_log_identifiers_and_payment_fields() -> None:
    value = (
        'Posting Customer: {"username":"Alice","longNum":"4111111111111111",'
        '"expires":"1232","ccv":"123","customer id":"cust-42"} '
        "session id: sess-42 user=usr-42"
    )

    redacted = redact_text(value)

    assert "Alice" not in redacted
    assert "4111111111111111" not in redacted
    assert "1232" not in redacted
    assert "123" not in redacted
    assert "cust-42" not in redacted
    assert "sess-42" not in redacted
    assert "usr-42" not in redacted


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


def test_report_serialization_redacts_model_generated_sensitive_text() -> None:
    record = InvestigationRecord(
        event=load_incident_case("deployment_regression"),
        report=IncidentReport(
            investigation_id="investigation-1",
            summary="resource pressure",
            markdown='username=Alice longNum="4111111111111111" ccv=123',
        ),
    )

    rows = record_to_rows(record)

    serialized = json.dumps(rows, ensure_ascii=False)
    assert "Alice" not in serialized
    assert "4111111111111111" not in serialized
    assert "ccv=123" not in serialized
    assert "[REDACTED]" in rows["report"]["markdown"]


def test_redaction_keeps_non_secret_token_counters_readable() -> None:
    record = InvestigationRecord(
        event=load_incident_case("deployment_regression"),
        report=IncidentReport(
            investigation_id="investigation-1",
            summary="resource pressure",
            markdown="summary",
            total_input_tokens=5,
            total_output_tokens=2,
        ),
    )

    restored = IncidentReport(**record_to_rows(record)["report"])

    assert restored.total_input_tokens == 5
    assert restored.total_output_tokens == 2
