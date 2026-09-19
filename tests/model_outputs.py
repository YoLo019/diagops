"""测试模型的显式 V12 响应；生产代码不得使用这些固定决策。"""


def critic_response(value):
    if isinstance(value, tuple):
        return (critic_response(value[0]), *value[1:])
    if not isinstance(value, dict) or "assessments" not in value or "final_decision" in value:
        return value
    assessments = value["assessments"]
    if any(item.get("verdict") == "needs_evidence" for item in assessments):
        decision = None
    else:
        accepted = [item for item in assessments if item.get("verdict") == "accept"]
        refs = list(
            dict.fromkeys(
                ref
                for item in accepted
                for check in item.get("checks", [])
                for ref in check.get("evidence_ids", [])
            )
        )
        decision = {
            "action": "conclude" if accepted else "inconclusive",
            "candidate_refs": [item["candidate_ref"] for item in accepted],
            "evidence_ids": refs,
            "summary": "Test model selected these candidates",
            "stop_reason": None if accepted else "test evidence is insufficient",
        }
    return {**value, "final_decision": decision}


def single_response(value):
    if isinstance(value, tuple):
        return (single_response(value[0]), *value[1:])
    if not isinstance(value, dict) or "candidates" not in value or "action" in value:
        return value
    candidates = value["candidates"]
    return {
        **value,
        "action": "conclude" if candidates else "inconclusive",
        "summary": "Test single Agent decision",
        "evidence_ids": list(
            dict.fromkeys(
                ref
                for candidate in candidates
                for ref in candidate.get("supporting_evidence_ids", [])
            )
        ),
        "stop_reason": None if candidates else "test evidence is insufficient",
    }
