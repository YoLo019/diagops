"""开发对照只评分已保存的预测，合成数据不能充当真实效果数据。"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from agents import Model, ModelResponse, Usage
from openai.types.responses import ResponseOutputMessage, ResponseOutputText

from backend.benchmarks.rcaeval.dev import adjudicate_lead, evidence_audit, score, write_json
from backend.benchmarks.rcaeval.models import CasePrediction, LabelManifest


def test_audit_preserves_reference_roles_and_rejects_foreign_evidence():
    evidence = [
        SimpleNamespace(
            id="ev",
            runtime_run_id="run",
            status=SimpleNamespace(value="success"),
            summary="observation",
        ),
        SimpleNamespace(
            id="foreign",
            runtime_run_id="other",
            status=SimpleNamespace(value="success"),
            summary="not visible",
        ),
    ]
    review = SimpleNamespace(
        runtime_run_id="run",
        candidates=[
            SimpleNamespace(
                id="a",
                summary="claim",
                failure_mechanism="mechanism",
                supporting_evidence_ids=["ev"],
                contradicting_evidence_ids=["foreign"],
            )
        ],
        critic_assessments=[],
        final_decision=SimpleNamespace(evidence_ids=["ev"], summary="decision"),
    )
    rows = evidence_audit(review, evidence)
    assert [row["role"] for row in rows] == ["support", "counterevidence", "context"]
    assert [row["valid"] for row in rows] == [True, False, True]
    assert rows[1]["evidence_summary"] is None
    assert all(row["judgment"] is None for row in rows)


def test_targeted_selection_is_a_unique_subset_of_existing_ob30():
    from backend.benchmarks.rcaeval.dev import select_ob30_cases

    cases = [SimpleNamespace(case_id=f"case-{i}", partition=SimpleNamespace(value="ob30"))
             for i in range(30)]
    manifest = SimpleNamespace(cases=cases)
    assert select_ob30_cases(manifest) == cases
    assert select_ob30_cases(manifest, ["case-3", "case-1"]) == [cases[1], cases[3]]
    for ids in (["case-1", "case-1"], ["unknown"]):
        with pytest.raises(ValueError, match="unique members"):
            select_ob30_cases(manifest, ids)
    with pytest.raises(ValueError, match="30-case selection"):
        select_ob30_cases(SimpleNamespace(cases=cases[:4]), ["case-1"])


def _saved_cases(tmp_path):
    case_ids = [f"re2-{number:016x}" for number in range(30)]
    write_json(
        tmp_path / "config.json",
        {"case_ids": case_ids, "runtime_manifest": {"manifest_hash": "a" * 64}},
    )
    labels = []
    for index, case_id in enumerate(case_ids):
        directory = tmp_path / case_id
        directory.mkdir()
        labels.append(
            {
                "case_id": case_id,
                "partition": "ob30",
                "source_case_id": "synthetic",
                "system": "online_boutique",
                "service": "checkout",
                "fault": "cpu",
                "repetition": 1,
            }
        )
        for side in ("single", "multi"):
            completed = index != 0
            selected = ["b", "a"] if side == "multi" else ["a"]
            prediction = CasePrediction(
                case_id=case_id,
                configuration=f"{side}_equal_token",
                completed=completed,
                runtime_run_id=f"{side}-{index}",
                execution_contract_hash="b" * 64,
                candidates=[
                    {
                        "affected_service": "checkout" if side == "multi" else "wrong",
                        "failure_class": "cpu",
                        "failure_mechanism": "cpu",
                        "evidence_ids": ["ev"],
                    }
                ]
                if completed
                else [],
                available_evidence_ids=["ev"],
                duration_ms=10,
                input_tokens=12,
                output_tokens=8,
            )
            write_json(directory / f"{side}.json", prediction.model_dump(mode="json"))
            write_json(
                directory / f"{side}-review.json",
                {
                    "candidates": [
                        {"id": "a", "affected_entity": "wrong", "failure_class": "cpu", "rank": 1},
                        {
                            "id": "b",
                            "affected_entity": "checkout",
                            "failure_class": "cpu",
                            "rank": 2,
                        },
                    ],
                    "critic_assessments": [{"candidate_id": "b", "verdict": "accept"}],
                    "final_decision": {"candidate_ids": selected, "action": "conclude"},
                },
            )
            row = {
                "owner": "b",
                "role": "support",
                "evidence_id": "ev",
                "valid": True,
                "judgment": "supported",
                "checked_by": "tester",
                "reason": "synthetic observation",
            }
            write_json(directory / f"{side}-audit.json", [row])
            write_json(
                directory / f"{side}-audit-source.json",
                [{**row, "checked_by": None, "judgment": None, "reason": None}],
            )
            write_json(
                directory / f"{side}-events.json",
                [
                    {"event_type": "model.started", "safe_payload": {}},
                    {
                        "event_type": "model.failed",
                        "safe_payload": {"usage_known": False, "accounted_tokens": 100},
                    },
                ],
            )
        write_json(
            directory / "lead.json",
            {
                "decision": {"candidate_ids": ["a"], "action": "conclude"} if index else None,
                "failure": None if index else "snapshot_unavailable",
                "input_tokens": 20,
                "output_tokens": 10,
                "model_requests": 1,
            },
        )
        if index:
            write_json(directory / "snapshot.json", {})
    manifest = LabelManifest(runtime_manifest_hash="a" * 64, entries=labels)
    write_json(tmp_path / "labels.json", manifest.model_dump(mode="json"))
    return SimpleNamespace(
        predictions=tmp_path, labels_ob30=tmp_path / "labels.json", output=tmp_path / "score.json"
    )


def test_scoring_uses_all_thirty_cases_and_records_extra_lead_regressions(tmp_path):
    arguments = _saved_cases(tmp_path)
    score(arguments)
    report = json.loads(arguments.output.read_text(encoding="utf-8"))
    assert report["multi"]["joint_top1_correct_count"] == 29
    assert report["multi"]["denominator"] == 30
    assert report["multi"]["failed_cases"] == 1
    assert report["single"]["joint_top1_correct_count"] == 0
    assert report["single"]["candidate_analysis"]["candidate_covered"] == 30
    assert report["lead"]["regressed"] == 29
    assert report["lead"]["valid_snapshot_count"] == 29
    assert report["lead"]["denominator"] == 30
    assert report["lead"]["incremental_requests"] == 30
    assert report["multi"]["unknown_usage_requests"] == 30
    assert report["evidence_integrity"] == {"valid": 60, "total": 60}
    with pytest.raises(FileExistsError):
        score(arguments)


@pytest.mark.parametrize("edit", ["missing_review", "removed_reference"])
def test_prelabel_audit_is_complete_before_labels_are_opened(tmp_path, edit):
    arguments = _saved_cases(tmp_path)
    path = tmp_path / "re2-0000000000000000" / "multi-audit.json"
    rows = json.loads(path.read_text(encoding="utf-8"))
    if edit == "missing_review":
        rows[0]["checked_by"] = None
    else:
        rows = []
    path.write_text(json.dumps(rows), encoding="utf-8")
    arguments.labels_ob30 = tmp_path / "does-not-exist.json"
    with pytest.raises(ValueError, match="audit"):
        score(arguments)


@pytest.mark.parametrize("failed_check", [False, True])
def test_snapshot_adjudication_uses_real_sdk_without_tools_and_records_usage(
    tmp_path, failed_check
):
    class LeadModel(Model):
        calls = 0

        async def get_response(self, **kwargs):
            self.calls += 1
            assert kwargs["tools"] == []
            assert "rank" not in json.dumps(kwargs["input"])
            decision = {
                "action": "conclude",
                "candidate_refs": ["b"],
                "evidence_ids": ["ev"],
                "summary": "supported",
                "stop_reason": None,
            }
            if failed_check and self.calls > 1:
                decision.update(action="inconclusive", candidate_refs=[],
                                stop_reason="candidate has failed causal checks")
            return ModelResponse(
                output=[
                    ResponseOutputMessage(
                        id="message",
                        type="message",
                        role="assistant",
                        status="completed",
                        content=[
                            ResponseOutputText(
                                type="output_text", text=json.dumps(decision), annotations=[]
                            )
                        ],
                    )
                ],
                usage=Usage(requests=1, input_tokens=30, output_tokens=20),
                response_id=None,
            )

        async def stream_response(self, **kwargs):
            raise AssertionError("not used")
            yield

    model = LeadModel()
    snapshot = {
        "candidates": [{"id": "b"}],
        "assessments": [
            {
                "candidate_id": "b",
                "verdict": "accept",
                "checks": [{"status": "fail" if failed_check else "pass"}],
            }
        ],
        "evidence": [{"id": "ev"}],
    }
    before = json.dumps(snapshot)
    result = asyncio.run(
        adjudicate_lead(snapshot, model, SimpleNamespace(provider="openai", model="fake"), tmp_path)
    )
    if failed_check:
        assert result["failure"] is None
        assert result["decision"]["action"] == "inconclusive"
        assert result["decision"]["candidate_ids"] == []
    else:
        assert result["failure"] is None
        assert result["decision"]["candidate_ids"] == ["b"]
    expected_calls = 2 if failed_check else 1
    assert result["model_requests"] == model.calls == expected_calls
    assert (result["input_tokens"], result["output_tokens"]) == (
        30 * expected_calls, 20 * expected_calls
    )
    events = [
        json.loads(line)
        for line in (tmp_path / "lead-events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [event["status"] for event in events] == ["started", "completed"] * expected_calls
    assert events[-1]["input_tokens"] == 30
    assert events[-1]["actor"] == "LeadAgent"
    assert json.dumps(snapshot) == before


@pytest.mark.parametrize("narrow_query", [False, True])
@pytest.mark.parametrize("high_budget", [False, True])
def test_real_sdk_three_investigators_two_rounds_use_fifteen_requests(
    tmp_path, narrow_query, high_budget,
):
    from openai.types.responses import ResponseFunctionToolCall

    from backend.benchmarks.rcaeval.models import EndpointCapabilityIdentity, EvaluationBudget
    from backend.benchmarks.rcaeval.runner import RcaEvalCaseRunner
    from backend.config.settings import endpoint_id
    from backend.db.session import create_db_engine, initialize_database
    from backend.db.sqlite_repository import SQLiteInvestigationRepository
    from backend.domain.multi_agent import CausalCheckName
    from backend.runtime.sqlite_store import SQLiteRuntimeStore
    from tests.benchmarks.test_rcaeval_runner import _write_runtime_package

    class TwoRoundModel(Model):
        calls = 0
        active = 0
        parallel_peak = 0
        investigator_started = 0
        ready = None
        rejected_candidate = None

        async def get_response(self, **kwargs):
            self.calls += 1
            call_number = self.calls
            self.active += 1
            self.parallel_peak = max(self.parallel_peak, self.active)
            prompt, _ = json.JSONDecoder().raw_decode(kwargs["system_instructions"])
            correcting = "candidate_evidence_reference" in kwargs["system_instructions"]
            role = prompt.get("role", "lead")
            if role == "general investigator" and self.investigator_started < 3:
                if self.ready is None:
                    self.ready = asyncio.Event()
                self.investigator_started += 1
                if self.investigator_started == 3:
                    self.ready.set()
                await asyncio.wait_for(self.ready.wait(), timeout=5)
            self.active -= 1
            if role == "lead":
                payload = {
                    "decision": {
                        "action": "investigate",
                        "summary": "three independent gaps",
                        "selected_skills": [],
                        "stop_reason": None,
                    },
                    "tasks": [
                        {
                            "title": f"task-{index}",
                            "description": "inspect checkout logs",
                            "information_gap": f"signal-{index}",
                            "expected_discriminator": None,
                            "evidence_scope": None,
                        }
                        for index in range(3)
                    ],
                }
            elif role == "critic":
                from backend.diagnosis.v11_runtime import CriticCompactOutput

                assert kwargs["output_schema"].output_type is CriticCompactOutput
                refs = prompt["allowed_evidence_ids"]
                first = prompt["round"] == 1
                candidates = prompt["candidates"]
                payload = {
                    "assessments": [
                        {
                            "candidate_ref": item["candidate_ref"],
                            "verdict": "needs_evidence" if first else "accept",
                            "checks": [
                                {
                                    "name": name.value,
                                    "status": "unknown" if first else "pass",
                                    "gap": "additional observation" if first else None,
                                    "evidence_ids": [] if first else refs[:1],
                                    **({} if first else {"summary": "supported"}),
                                }
                                for name in CausalCheckName
                            ],
                            "gap": "additional observation" if first else None,
                            "supplemental_task_ids": [f"extra-{index}"] if first else [],
                        }
                        for index, item in enumerate(candidates)
                    ],
                    "tasks": [
                        {
                            "id": f"extra-{index}",
                            "title": f"extra-{index}",
                            "description": "check logs again",
                            "information_gap": "additional observation",
                            "expected_discriminator": None,
                            "evidence_scope": None,
                        }
                        for index in range(3)
                    ]
                    if first
                    else [],
                    "final_decision": None
                    if first
                    else {
                        "action": "conclude",
                        "candidate_refs": [item["candidate_ref"] for item in reversed(candidates)],
                        "evidence_ids": refs[:2],
                        "summary": "Final selection after second round",
                        "stop_reason": None,
                    },
                }
            else:
                results = [
                    item
                    for item in kwargs["input"]
                    if isinstance(item, dict) and item.get("type") == "function_call_output"
                ]
                if not results:
                    # 收尾把工具历史转为只读观测；替身仍须用已返回证据形成结论。
                    for item in kwargs["input"]:
                        if isinstance(item, dict) and item.get("role") == "user":
                            content = item.get("content")
                            if isinstance(content, str):
                                try:
                                    closed = json.loads(content)
                                except ValueError:
                                    continue
                                if isinstance(closed, dict):
                                    results.extend({"output": json.dumps(observation)}
                                                   for observation in
                                                   closed.get("collected_observations", []))
                if correcting:
                    assert not kwargs["tools"]
                    assert "own_tool_evidence" in str(kwargs["input"])
                    response = {"evidence": self.rejected_candidate}
                elif not results:
                    output = [
                        ResponseFunctionToolCall(
                            type="function_call",
                            name="read_logs",
                            arguments='{"limit":1}',
                            call_id=f"tool-{call_number}",
                        )
                    ]
                    return ModelResponse(
                        output=output,
                        usage=Usage(requests=1, input_tokens=100, output_tokens=40),
                        response_id=None,
                    )
                else:
                    response = json.loads(results[-1]["output"])
                if (
                    not correcting and narrow_query
                    and prompt["task"]["title"] == "task-0" and len(results) == 1
                ):
                    assert response["truncated"] is True
                    return ModelResponse(
                        output=[
                            ResponseFunctionToolCall(
                                type="function_call",
                                name="read_logs",
                                arguments='{"limit":1,"keywords":["unique"]}',
                                call_id=f"narrow-{call_number}",
                            )
                        ],
                        usage=Usage(requests=1, input_tokens=100, output_tokens=40),
                        response_id=None,
                    )
                if not correcting and narrow_query and prompt["task"]["title"] == "task-0":
                    assert response["truncated"] is False
                    assert any("unique" in item["summary"] for item in response["evidence"])
                refs = [
                    item["id"]
                    for item in response["evidence"]
                    if item.get("status", "success") in ("success", "partial")
                ]
                assert refs
                seed = [
                    item["id"]
                    for item in prompt["evidence"]
                    if item.get("status", "success") in ("success", "partial")
                ]
                if "critic_assessment" in prompt:
                    payload = {
                        "findings": [
                            {
                                "finding_type": "signal",
                                "summary": "second round observation",
                                "confidence": 0.7,
                                "evidence_ids": refs,
                            }
                        ],
                        "candidates": [],
                    }
                else:
                    if (
                        high_budget and narrow_query and not correcting
                        and prompt["task"]["title"] == "task-0"
                    ):
                        self.rejected_candidate = response["evidence"]
                        refs = ["unavailable-evidence"]
                    payload = {
                        "candidates": [
                            {
                                "affected_entity": "checkout",
                                "failure_class": "timeout",
                                "failure_mechanism": f"{prompt['task']['title']} timeout",
                                "supporting_evidence_ids": list(dict.fromkeys(seed + refs)),
                                "contradicting_evidence_ids": [],
                            }
                        ]
                    }
            output = [
                ResponseOutputMessage(
                    id=f"message-{call_number}",
                    type="message",
                    role="assistant",
                    status="completed",
                    content=[
                        ResponseOutputText(
                            type="output_text", text=json.dumps(payload), annotations=[]
                        )
                    ],
                )
            ]
            return ModelResponse(
                output=output,
                usage=Usage(requests=1, input_tokens=100, output_tokens=100),
                response_id=None,
            )

        async def stream_response(self, **kwargs):
            raise AssertionError("not used")
            yield

    case = _write_runtime_package(tmp_path / "data")
    if narrow_query:
        with (tmp_path / "data" / "cases" / case.case_id / "telemetry-00.csv").open(
            "a", encoding="utf-8"
        ) as stream:
            stream.write("12:00,checkout,unique timeout detail,ERROR,/checkout,true\n")
    engine = create_db_engine(f"sqlite:///{tmp_path / 'runtime.db'}")
    initialize_database(engine)
    repository = SQLiteInvestigationRepository(engine)
    store = SQLiteRuntimeStore(engine, repository)
    model = TwoRoundModel()
    runner = RcaEvalCaseRunner(
        runtime_package=tmp_path / "data",
        model=model,
        capability=EndpointCapabilityIdentity(
            provider="openai",
            model="fake",
            api_mode="responses",
            endpoint_id=endpoint_id("https://api.openai.com/v1"),
            artifact_hash="a" * 64,
        ),
        repository=repository,
        runtime_store=store,
    )
    budget = EvaluationBudget(
        configuration="multi_equal_token",
        token_budget=200000 if high_budget else 100000,
        # 三路补查之外保留一次最终审查纠错；窄化查询额外消耗一次请求。
        max_turns=48 if high_budget else 16 + int(narrow_query),
        tool_budget=24 if high_budget else 8,
        timeout_seconds=300 if high_budget else 120,
        max_investigators=3,
        max_rounds=2,
    )
    try:
        prediction = runner.run_case(case, budget)
        run = store.get_run(prediction.runtime_run_id)
        executions = repository.list_executions(run.investigation_id)
        assert prediction.completed, "\n".join(
            f"{item.agent_name}: {item.error_message}" for item in executions if item.error_message
        )
        assert model.calls == 15 + int(narrow_query) + int(high_budget and narrow_query)
        assert model.parallel_peak == 3
        assert run.remaining_model_turns == budget.max_turns - model.calls
        if high_budget:
            assert run.execution_contract["limits"]["max_tool_calls_per_specialist"] == 8
            assert run.token_budget == 200000
            assert run.timeout_seconds == 300
        assert prediction.tool_calls == 6 + int(narrow_query)
        assert repository.get(run.investigation_id).multi_agent_run.completed_rounds == 2
        assert repository.get(run.investigation_id).report is not None
        review = repository.get_coordination_review(run.investigation_id)
        assert review.authoritative_candidate_ids == [
            item.id for item in reversed(review.candidates)
        ]
        assert (
            sum(event.event_type == "model.started" for event in store.list_events(run.id))
            == model.calls
        )
    finally:
        engine.dispose()
