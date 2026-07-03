from backend.domain.evidence import EvidenceItem


def contains_any(evidence: list[EvidenceItem], keywords: list[str]) -> list[EvidenceItem]:
    matches: list[EvidenceItem] = []
    lowered_keywords = [keyword.lower() for keyword in keywords]

    for item in evidence:
        text = f"{item.summary} {item.payload}".lower()
        if any(keyword in text for keyword in lowered_keywords):
            matches.append(item)

    return matches


def confidence_from_score(score: int) -> float:
    return min(0.95, round(score / 10, 2))
