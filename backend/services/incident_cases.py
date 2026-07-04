import json
from pathlib import Path

from backend.domain.events import IncidentEvent

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INCIDENT_CASE_DIR = PROJECT_ROOT / "data" / "incidents"


def list_case_ids() -> list[str]:
    return sorted(path.stem for path in INCIDENT_CASE_DIR.glob("*.json"))


def load_incident_case(case_id: str) -> IncidentEvent:
    if case_id not in list_case_ids():
        raise ValueError(f"Unknown incident case: {case_id}")

    case_path = INCIDENT_CASE_DIR / f"{case_id}.json"
    return IncidentEvent.model_validate(json.loads(case_path.read_text(encoding="utf-8")))
