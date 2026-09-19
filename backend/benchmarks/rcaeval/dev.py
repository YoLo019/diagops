"""OB30 开发对照入口；预测不接收标签，评分在单独命令中执行。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from statistics import mean, median
from time import perf_counter
from uuid import uuid4

from backend.benchmarks.rcaeval.models import (
    CasePrediction,
    EndpointCapabilityIdentity,
    EvaluationBudget,
    RcaEvalConfiguration,
)
from backend.domain.multi_agent import ExecutionActor

DIAGNOSIS_TOKEN_BUDGET = 200000
DIAGNOSIS_REQUEST_BUDGET = 48
DIAGNOSIS_TOOL_BUDGET = 24
DIAGNOSIS_TIMEOUT_SECONDS = 300
LEAD_TOKEN_BUDGET = 60000
LEAD_REQUEST_BUDGET = 12
LEAD_TIMEOUT_SECONDS = 180


def write_json(path: Path, value) -> None:
    """只写新产物，重新调用模型必须另建输出目录。"""
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def evidence_audit(review, evidence, actions=(), verifications=()) -> list[dict]:
    """遍历支持、反证、检查和建议引用，角色分开供人工核对。"""
    available = {
        item.id: item
        for item in evidence
        if item.runtime_run_id == review.runtime_run_id
        and item.status.value in {"success", "partial"}
    }
    rows = []

    def add(owner, role, ids, claim=""):
        for evidence_id in ids:
            rows.append(
                {
                    "owner": owner,
                    "role": role,
                    "evidence_id": evidence_id,
                    "valid": evidence_id in available,
                    "claim": claim,
                    "evidence_summary": available[evidence_id].summary
                    if evidence_id in available
                    else None,
                    "checked_by": None,
                    "judgment": None,
                    "reason": None,
                }
            )

    for candidate in review.candidates:
        add(
            candidate.id,
            "support",
            candidate.supporting_evidence_ids,
            candidate.failure_mechanism or candidate.summary,
        )
        add(
            candidate.id, "counterevidence", candidate.contradicting_evidence_ids, candidate.summary
        )
    for assessment in review.critic_assessments:
        add(assessment.id, "support", assessment.supporting_evidence_ids)
        add(assessment.id, "counterevidence", assessment.contradicting_evidence_ids)
        for check in assessment.checks:
            add(f"{assessment.id}:{check.name.value}", "check", check.evidence_ids, check.summary)
    if review.final_decision:
        add(
            "final_decision",
            "context",
            review.final_decision.evidence_ids,
            review.final_decision.summary,
        )
    for item in actions:
        add(item.id, "recommendation", item.supporting_evidence_ids)
    for item in verifications:
        add(item.id, "verification", item.result_evidence_ids)
    return rows


def adjudication_snapshot(review, findings, evidence) -> dict:
    """隐藏最终选择及展示 rank，保留同一批事实和逐候选因果审查。"""
    from backend.diagnosis.v11_runtime import _evidence_projection, _usable_critic_evidence

    if review.final_decision is None or review.final_decision.actor != "critic":
        raise ValueError("valid Critic snapshot is unavailable")
    findings = [item for item in findings if item.runtime_run_id == review.runtime_run_id]
    selected_evidence = _usable_critic_evidence(
        review,
        findings,
        evidence,
        runtime_run_id=review.runtime_run_id,
    )
    return {
        "candidates": [
            candidate.model_dump(mode="json", exclude={"rank"}) for candidate in review.candidates
        ],
        "assessments": [
            item.model_dump(mode="json", exclude={"id", "runtime_run_id", "supplemental_task_ids"})
            for item in review.critic_assessments
        ],
        "findings": [item.model_dump(mode="json") for item in findings],
        "evidence": [_evidence_projection(item) for item in selected_evidence],
    }


async def adjudicate_lead(snapshot: dict, model, capability, directory: Path) -> dict:
    """仅用保存的快照追加一次裁决操作，独立保存模型事件和增量成本。"""
    from backend.diagnosis.adaptive_tools import ClassifiedRetryableError
    from backend.diagnosis.v11_runtime import FinalDecisionDraft, V11Runtime
    from backend.domain.multi_agent import FailureCategory

    runtime = V11Runtime(
        model=model,
        model_provider=capability.provider,
        model_name=capability.model,
        max_turns=LEAD_REQUEST_BUDGET,
        token_budget=LEAD_TOKEN_BUDGET,
        timeout_seconds=LEAD_TIMEOUT_SECONDS,
    )
    runtime.runtime_run_id = f"lead-ablation-{uuid4().hex}"
    runtime._remaining_model_turns = LEAD_REQUEST_BUDGET
    runtime._model_turn_budget_enabled = True

    async def reserve():
        remaining = runtime._remaining_model_turns
        if remaining <= 0:
            raise ValueError("snapshot request budget exhausted")
        return remaining - 1

    async def record_event(execution_id, status, input_tokens, output_tokens, actor, safe_payload):
        events.append({"status": status, "safe_payload": safe_payload or {}})
        with (directory / "lead-events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "execution_id": execution_id,
                        "status": status,
                        "actor": actor,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "safe_payload": safe_payload,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    runtime._reserve_model_turn_callback = reserve
    runtime._persist_model_event = record_event
    events = []
    started = perf_counter()
    result = {"decision": None, "failure": None}
    accepted = {
        item["candidate_id"] for item in snapshot["assessments"]
        if item["verdict"] == "accept"
        and not any(check["status"] == "fail" for check in item.get("checks", []))
    } & {item["id"] for item in snapshot["candidates"]}
    evidence_ids = {item["id"] for item in snapshot["evidence"]}

    def validate_decision(draft):
        decision = draft.to_domain("lead")
        if (not set(decision.candidate_ids) <= accepted
                or not set(decision.evidence_ids) <= evidence_ids):
            raise ClassifiedRetryableError(
                FailureCategory.INVALID_OUTPUT, audit_code="invalid_snapshot_reference"
            )

    try:
        turn = await asyncio.wait_for(
            runtime._call_model(
                actor=ExecutionActor.LEAD.value,
                prompt="Select, order or abstain from Critic-accepted candidates "
                "using this evidence. "
                "Return final decision candidate_refs copied from candidate IDs. "
                "Do not create candidates or call tools. "
                "Cite committed evidence and explain uncertainty. If none can be selected, "
                "return action=inconclusive, candidate_refs=[], and a concrete stop_reason. "
                "For conclude, both candidate_refs and evidence_ids must be nonempty.",
                output_type=FinalDecisionDraft,
                context=snapshot,
                tools=[],
                remaining_token_budget=LEAD_TOKEN_BUDGET,
                remaining_tool_budget=0,
                output_validator=validate_decision,
            ),
            timeout=LEAD_TIMEOUT_SECONDS,
        )
        decision = runtime._parse_output(turn.output, FinalDecisionDraft).to_domain("lead")
        result["decision"] = decision.model_dump(mode="json")
    except Exception as exc:
        result["failure"] = type(exc).__name__
    result.update(
        duration_ms=round((perf_counter() - started) * 1000),
        input_tokens=runtime._input_tokens,
        output_tokens=runtime._output_tokens,
        model_requests=LEAD_REQUEST_BUDGET - runtime._remaining_model_turns,
        unknown_usage_requests=sum(
            item["safe_payload"].get("usage_known") is False for item in events
        ),
        accounted_tokens=LEAD_TOKEN_BUDGET - runtime.remaining_token_budget,
    )
    return result


def select_ob30_cases(manifest, requested_ids=()):
    """只从既有 OB30 选择定向回归案例，不改变数据包或读取标签。"""
    cases = [item for item in manifest.cases if item.partition.value == "ob30"]
    if len(cases) != 30 or len({item.case_id for item in cases}) != 30:
        raise ValueError("OB30 requires the existing 30-case selection")
    if not requested_ids:
        return cases
    selected = set(requested_ids)
    if len(selected) != len(requested_ids) or not selected <= {item.case_id for item in cases}:
        raise ValueError("Requested case IDs must be unique members of OB30")
    return [item for item in cases if item.case_id in selected]


def predict(arguments) -> None:
    from backend.benchmarks.rcaeval.isolation import verify_runtime_package
    from backend.benchmarks.rcaeval.runner import RcaEvalCaseRunner
    from backend.config.settings import canonicalize_endpoint, endpoint_id
    from backend.db.session import create_db_engine, initialize_database
    from backend.db.sqlite_repository import SQLiteInvestigationRepository
    from backend.diagnosis.openai_compatible_model import (
        compatible_extra_body,
        create_openai_compatible_model,
    )
    from backend.diagnosis.v11_runtime import (
        _CRITIC_OUTPUT_TOKEN_LIMIT,
        _MODEL_OUTPUT_TOKEN_LIMIT,
    )
    from backend.domain.runtime import V11_DEFAULT_MAX_MODEL_CORRECTIONS
    from backend.runtime.sqlite_store import SQLiteRuntimeStore
    from backend.services.model_capability import (
        read_capability_artifact,
        validate_capability_for_prediction,
    )

    manifest = verify_runtime_package(arguments.runtime)
    cases = select_ob30_cases(manifest, getattr(arguments, "case_ids", []))
    artifact = read_capability_artifact(arguments.capability_artifact)
    endpoint = canonicalize_endpoint(arguments.base_url)
    validate_capability_for_prediction(
        artifact,
        provider="openai_compatible",
        model=arguments.model,
        endpoint_id_value=endpoint_id(endpoint),
        expected_parallelism=3,
        repository_root=Path.cwd(),
        require_clean_source=False,
    )
    model = create_openai_compatible_model(
        arguments.model,
        os.environ["DIAGOPS_AGENTS_API_KEY"],
        endpoint,
        timeout_seconds=120,
        max_retries=0,
        structured_output_transport=artifact.structured_output_transport,
    )
    capability = EndpointCapabilityIdentity(
        **{
            key: getattr(artifact, key)
            for key in (
                "provider",
                "model",
                "api_mode",
                "structured_output_transport",
                "endpoint_id",
                "artifact_hash",
            )
        }
    )
    arguments.output.mkdir(parents=True, exist_ok=False)
    configurations = [
        RcaEvalConfiguration.SINGLE_EQUAL_TOKEN,
        RcaEvalConfiguration.MULTI_EQUAL_TOKEN,
    ]
    if getattr(arguments, "multi_only", False):
        configurations = [RcaEvalConfiguration.MULTI_EQUAL_TOKEN]
    budgets = {
        config: EvaluationBudget(
            configuration=config,
            token_budget=DIAGNOSIS_TOKEN_BUDGET,
            max_turns=DIAGNOSIS_REQUEST_BUDGET,
            tool_budget=DIAGNOSIS_TOOL_BUDGET,
            timeout_seconds=DIAGNOSIS_TIMEOUT_SECONDS,
            max_investigators=3 if config.is_multi else 1,
            max_rounds=2 if config.is_multi else 1,
        )
        for config in configurations
    }
    write_json(
        arguments.output / "config.json",
        {
            "dataset": "OB30 targeted regression" if len(cases) != 30 else "OB30 development cases",
            "case_ids": [case.case_id for case in cases],
            "runtime_manifest": manifest.model_dump(mode="json"),
            "model": arguments.model,
            "request_extra_body": compatible_extra_body(endpoint),
            "endpoint_id": capability.endpoint_id,
            "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "budgets": {key.value: value.model_dump(mode="json") for key, value in budgets.items()},
            "budget_purpose": "high-budget reliability validation; tune after observed usage",
            "model_request_timeout_seconds": 120,
            "max_structured_corrections": V11_DEFAULT_MAX_MODEL_CORRECTIONS,
            "model_output_token_limits": {
                "default": _MODEL_OUTPUT_TOKEN_LIMIT,
                "critic": _CRITIC_OUTPUT_TOKEN_LIMIT,
            },
            "investigator_tool_budget": DIAGNOSIS_TOOL_BUDGET // 3,
            "lead_budget": {
                "tokens": LEAD_TOKEN_BUDGET, "requests": LEAD_REQUEST_BUDGET,
                "timeout_seconds": LEAD_TIMEOUT_SECONDS,
            },
        },
    )
    (arguments.output / "changes.patch").write_bytes(
        subprocess.check_output(
            ["git", "diff", "HEAD", "--", "backend", "config", "pyproject.toml", "uv.lock"]
        )
    )
    # 保存相关源码（包括未提交与未跟踪文件），避免 patch 漏掉新入口或提示词。
    source_dir = arguments.output / "source"
    paths = subprocess.check_output(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "backend",
            "config",
            "pyproject.toml",
            "uv.lock",
        ],
        text=True,
        encoding="utf-8",
    ).splitlines()
    for name in dict.fromkeys(paths):
        source = Path(name)
        if source.is_file():
            target = source_dir / source
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    engine = create_db_engine(f"sqlite:///{(arguments.output / 'runtime.db').resolve()}")
    initialize_database(engine)
    repository = SQLiteInvestigationRepository(engine)
    store = SQLiteRuntimeStore(engine, repository)
    runner = RcaEvalCaseRunner(
        runtime_package=arguments.runtime,
        model=model,
        capability=capability,
        repository=repository,
        runtime_store=store,
    )
    try:
        for index, case in enumerate(cases):
            case_dir = arguments.output / case.case_id
            case_dir.mkdir()
            for config in configurations[:: 1 if index % 2 == 0 else -1]:
                prediction = runner.run_case(case, budgets[config])
                run = store.get_run(prediction.runtime_run_id)
                record = repository.get(run.investigation_id)
                review = repository.get_coordination_review(record.id)
                side = "multi" if config.is_multi else "single"
                write_json(case_dir / f"{side}.json", prediction.model_dump(mode="json"))
                write_json(
                    case_dir / f"{side}-review.json",
                    review.model_dump(mode="json") if review else None,
                )
                write_json(
                    case_dir / f"{side}-audit.json",
                    evidence_audit(
                        review,
                        record.evidence,
                        record.actions,
                        record.verification_suggestions,
                    )
                    if review
                    else [],
                )
                shutil.copyfile(
                    case_dir / f"{side}-audit.json", case_dir / f"{side}-audit-source.json"
                )
                events = store.list_events(run.id)
                write_json(
                    case_dir / f"{side}-events.json",
                    [event.model_dump(mode="json") for event in events],
                )
                if config.is_multi:
                    if (
                        prediction.completed
                        and review
                        and review.final_decision
                        and review.final_decision.actor == "critic"
                    ):
                        snapshot = adjudication_snapshot(
                            review, repository.list_agent_findings(record.id), record.evidence
                        )
                        write_json(case_dir / "snapshot.json", snapshot)
                        lead = asyncio.run(adjudicate_lead(snapshot, model, capability, case_dir))
                    else:
                        lead = {"decision": None, "failure": "snapshot_unavailable"}
                    write_json(case_dir / "lead.json", lead)
    finally:
        engine.dispose()


def score(arguments) -> None:
    # 此命令只读保存的预测，不导入模型端点或重新调用 Agent。
    from backend.benchmarks.rcaeval.evaluator import (
        evaluate_predictions,
        normalize_fault_label,
        normalize_label,
        scorer_dependency_hash,
    )
    from backend.benchmarks.rcaeval.models import LabelManifest

    config = json.loads((arguments.predictions / "config.json").read_text(encoding="utf-8"))
    case_ids = config["case_ids"]
    if len(case_ids) != 30 or len(set(case_ids)) != 30:
        raise ValueError("All 30 planned cases must be unique")
    audits = []
    manual_fields = {"checked_by", "judgment", "reason"}
    for case in case_ids:
        for side in ("single", "multi"):
            directory = arguments.predictions / case
            rows = json.loads((directory / f"{side}-audit.json").read_text(encoding="utf-8"))
            source = json.loads(
                (directory / f"{side}-audit-source.json").read_text(encoding="utf-8")
            )
            if [
                {key: value for key, value in row.items() if key not in manual_fields}
                for row in rows
            ] != [
                {key: value for key, value in row.items() if key not in manual_fields}
                for row in source
            ]:
                raise ValueError("Evidence audit references must match the saved source")
            audits.extend(rows)
    if any(
        not row["checked_by"]
        or row["judgment"] not in ("supported", "unsupported", "uncertain", "context")
        or not row["reason"]
        for row in audits
    ):
        raise ValueError("Complete the saved evidence audit before opening labels")
    labels = LabelManifest.model_validate_json(arguments.labels_ob30.read_text(encoding="utf-8"))
    if labels.runtime_manifest_hash != config["runtime_manifest"]["manifest_hash"]:
        raise ValueError("Labels belong to another runtime package")
    if any(item.partition.value != "ob30" for item in labels.entries):
        raise ValueError("Only an OB30-only label package is accepted")
    by_id = {item.case_id: item for item in labels.entries}
    if set(by_id) != set(case_ids) or len(labels.entries) != 30:
        raise ValueError("Labels and all 30 planned cases must match")
    report = {
        "dataset": "OB30 development set; not production accuracy", "sample_count": 30,
        "scorer_dependency_hash": scorer_dependency_hash(),
    }
    outcomes = {}
    lead_outcomes = []
    lead_costs = []
    lead_available = 0
    lead_order_changes = 0
    lead_abstentions = 0
    for side in ("single", "multi"):
        predictions = [
            CasePrediction.model_validate_json(
                (arguments.predictions / case / f"{side}.json").read_text(encoding="utf-8")
            )
            for case in case_ids
        ]
        summary = evaluate_predictions(predictions, list(by_id.values())).model_dump(mode="json")
        summary["joint_top1_correct_count"] = round(summary["exact_top1"] * 30)
        summary["service_top1_correct_count"] = round(summary["component_top1"] * 30)
        summary["denominator"] = 30
        summary["status_counts"] = dict(
            Counter(str(item.diagnostic_status) for item in predictions)
        )
        summary["mean_duration_ms"] = mean(item.duration_ms for item in predictions)
        summary["median_duration_ms"] = median(item.duration_ms for item in predictions)
        failures = Counter()
        outcomes[side] = []
        for prediction in predictions:
            label = by_id[prediction.case_id]
            review = (
                json.loads(
                    (arguments.predictions / prediction.case_id / f"{side}-review.json").read_text(
                        encoding="utf-8"
                    )
                )
                or {}
            )

            def matches(candidate, label=label):
                return normalize_label(candidate.get("affected_entity") or "") == normalize_label(
                    label.service
                ) and normalize_fault_label(
                    candidate.get("failure_class") or ""
                ) == normalize_fault_label(label.fault)

            matching = {item["id"] for item in review.get("candidates", []) if matches(item)}
            accepted = {
                item["candidate_id"]
                for item in review.get("critic_assessments", [])
                if item["verdict"] == "accept"
            }
            final = review.get("final_decision") or {}
            correct = bool(
                prediction.completed
                and final.get("candidate_ids", [])
                and final["candidate_ids"][0] in matching
            )
            outcomes[side].append(correct)
            failures["candidate_covered"] += bool(matching)
            if not correct:
                failures[
                    "no_candidate_snapshot"
                    if not review
                    else "candidate_missed"
                    if not matching
                    else "critic_excluded"
                    if side == "multi" and not matching & accepted
                    else "final_selection_or_execution_failed"
                ] += 1
            if side == "multi":
                lead = json.loads(
                    (arguments.predictions / prediction.case_id / "lead.json").read_text(
                        encoding="utf-8"
                    )
                )
                decision = lead.get("decision") or {}
                lead_available += (
                    arguments.predictions / prediction.case_id / "snapshot.json"
                ).exists()
                lead_outcomes.append(
                    bool(decision.get("candidate_ids") and decision["candidate_ids"][0] in matching)
                )
                lead_costs.append(lead)
                lead_order_changes += bool(
                    decision and decision.get("candidate_ids") != final.get("candidate_ids")
                )
                lead_abstentions += decision.get("action") == "inconclusive"
        events = [
            event
            for case in case_ids
            for event in json.loads(
                (arguments.predictions / case / f"{side}-events.json").read_text(encoding="utf-8")
            )
        ]
        summary["model_requests"] = sum(event["event_type"] == "model.started" for event in events)
        summary["unknown_usage_requests"] = sum(
            event["safe_payload"].get("usage_known") is False for event in events
        )
        summary["conservative_unknown_tokens"] = sum(
            event["safe_payload"].get("accounted_tokens", 0)
            for event in events
            if event["safe_payload"].get("usage_known") is False
        )
        summary["investigation_limits"] = dict(
            Counter(item.failure_category for item in predictions if item.failure_category)
        )
        summary["candidate_analysis"] = dict(failures)
        report[side] = summary
    report["paired"] = dict(
        Counter(
            f"single_{a}_multi_{b}"
            for a, b in zip(outcomes["single"], outcomes["multi"], strict=True)
        )
    )
    report["lead"] = {
        "correct_count": sum(lead_outcomes),
        "denominator": 30,
        "valid_snapshot_count": lead_available,
        "conditional_accuracy": sum(lead_outcomes) / lead_available if lead_available else None,
        "corrected": sum(
            not a and b for a, b in zip(outcomes["multi"], lead_outcomes, strict=True)
        ),
        "regressed": sum(
            a and not b for a, b in zip(outcomes["multi"], lead_outcomes, strict=True)
        ),
        "both_correct": sum(a and b for a, b in zip(outcomes["multi"], lead_outcomes, strict=True)),
        "both_wrong": sum(
            not a and not b for a, b in zip(outcomes["multi"], lead_outcomes, strict=True)
        ),
        "selection_order_changes": lead_order_changes,
        "abstentions": lead_abstentions,
        "unknown_usage_requests": sum(item.get("unknown_usage_requests", 0) for item in lead_costs),
        "failure_counts": dict(
            Counter(item["failure"] for item in lead_costs if item.get("failure"))
        ),
        "incremental_duration_ms": sum(item.get("duration_ms", 0) for item in lead_costs),
        "incremental_input_tokens": sum(item.get("input_tokens", 0) for item in lead_costs),
        "incremental_output_tokens": sum(item.get("output_tokens", 0) for item in lead_costs),
        "incremental_requests": sum(item.get("model_requests", 0) for item in lead_costs),
        "limitations": "Extra budget; snapshot only; not end-to-end Multi-Lead latency",
    }
    report["evidence_integrity"] = {
        "valid": sum(row["valid"] for row in audits),
        "total": len(audits),
    }
    report["monetary_cost"] = None
    report["cost_note"] = (
        "Token/request usage only; endpoint prices were not supplied. "
        "Unknown usage is reported separately."
    )
    write_json(arguments.output, report)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("predict")
    run.add_argument("--runtime", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--base-url", required=True)
    run.add_argument("--capability-artifact", type=Path, required=True)
    run.add_argument("--model", default="deepseek-flash")
    run.add_argument("--case-id", dest="case_ids", action="append", default=[])
    run.add_argument("--multi-only", action="store_true")
    evaluate = commands.add_parser("score")
    evaluate.add_argument("--predictions", type=Path, required=True)
    evaluate.add_argument("--labels-ob30", type=Path, required=True)
    evaluate.add_argument("--output", type=Path, required=True)
    semantic = commands.add_parser("score-semantic")
    semantic.add_argument("--predictions", type=Path, required=True)
    semantic.add_argument("--labels-ob30", type=Path, required=True)
    semantic.add_argument("--output", type=Path, required=True)
    semantic.add_argument("--base-url", required=True)
    semantic.add_argument("--model", default="deepseek-flash")
    semantic.add_argument("--side", choices=("single", "multi"), default="multi")
    arguments = parser.parse_args()
    if arguments.command == "score-semantic":
        from backend.benchmarks.rcaeval.semantic import score_semantic

        score_semantic(arguments)
    else:
        (predict if arguments.command == "predict" else score)(arguments)


if __name__ == "__main__":
    main()
