import json
from pathlib import Path

from backend.domain.events import IncidentEvent

INCIDENT_CASE_DIR = Path("data/incidents")


def list_case_ids() -> list[str]:
    return sorted(path.stem for path in INCIDENT_CASE_DIR.glob("*.json"))


def load_incident_case(case_id: str) -> IncidentEvent:
    case_path = INCIDENT_CASE_DIR / f"{case_id}.json"
    if not case_path.exists():
        raise ValueError(f"Unknown incident case: {case_id}")

    return IncidentEvent.model_validate(json.loads(case_path.read_text(encoding="utf-8")))
