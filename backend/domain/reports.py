from pydantic import BaseModel, Field

from backend.domain.hypotheses import Hypothesis


class IncidentReport(BaseModel):
    investigation_id: str
    summary: str
    timeline: list[dict[str, str]] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    markdown: str
