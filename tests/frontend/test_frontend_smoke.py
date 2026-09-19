"""前端可执行行为与安全边界测试。

保留实际执行 selector、candidate visibility、redaction 和未验证操作声明过滤
的测试；UI 文案、类型字段和调用关系的源码形状不再逐字断言，由 frontend
production build 与 backend API tests 覆盖。
"""

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"


def _run_app_exports(calls: list[dict[str, object]]) -> list[object]:
    script = r"""
const fs = require("fs");
const ts = require("typescript");
const vm = require("vm");
const source = fs.readFileSync("src/App.tsx", "utf8");
const javascript = ts.transpileModule(source, {
  compilerOptions: {
    jsx: ts.JsxEmit.ReactJSX,
    module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2022,
  },
}).outputText;
const module = { exports: {} };
const dependency = new Proxy({}, { get: () => () => null });
vm.runInNewContext(javascript, {
  exports: module.exports,
  module,
  require: (name) => name === "react/jsx-runtime"
    ? { Fragment: Symbol("Fragment"), jsx: () => null, jsxs: () => null }
    : dependency,
});
const calls = JSON.parse(fs.readFileSync(0, "utf8"));
const results = calls.map(({ name, args }) => module.exports[name](...args));
process.stdout.write(JSON.stringify(results));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=FRONTEND,
        input=json.dumps(calls, ensure_ascii=False),
        capture_output=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(completed.stdout)


def test_frontend_package_json_exists() -> None:
    assert (FRONTEND / "package.json").exists()


def test_tool_calls_group_by_agent_and_task_round() -> None:
    calls = [
        {"id": "call-log-2", "task_id": "task-log-2", "agent_name": "LogAgent"},
        {"id": "call-deploy", "task_id": "task-fixed", "agent_name": "DeploymentAgent"},
        {"id": "call-log-1", "task_id": "task-log-1", "agent_name": "LogAgent"},
    ]
    tasks = [
        {"id": "task-log-1", "analysis_round": 1},
        {"id": "task-log-2", "analysis_round": 2},
    ]

    groups = _run_app_exports(
        [
            {
                "name": "groupToolCallsByAgentAndRound",
                "args": [calls, tasks],
            }
        ]
    )[0]

    assert [
        (group["agentName"], group["round"], [call["id"] for call in group["calls"]])
        for group in groups
    ] == [
        ("DeploymentAgent", None, ["call-deploy"]),
        ("LogAgent", 1, ["call-log-1"]),
        ("LogAgent", 2, ["call-log-2"]),
    ]


def test_v7_selectors_execute_against_payloads() -> None:
    results = _run_app_exports(
        [
            {
                "name": "selectDisplayedCause",
                "args": ["conflict", "deployment_regression", "database_slowdown"],
            },
            {
                "name": "selectDisplayedCause",
                "args": ["fallback", None, "database_slowdown"],
            },
            {
                "name": "selectDisplayedCause",
                "args": ["fallback", "deployment_regression", "database_slowdown"],
            },
            {
                "name": "selectDisplayedCause",
                "args": ["agreement", "deployment_regression", "database_slowdown"],
            },
            {
                "name": "selectDisplayedCause",
                "args": ["agreement", None, "database_slowdown"],
            },
            {
                "name": "selectDisplayedCause",
                "args": ["agent_leads", "deployment_regression", "database_slowdown"],
            },
            {
                "name": "selectDisplayedCause",
                "args": ["agent_leads", None, "database_slowdown"],
            },
            {
                "name": "projectAgentUiText",
                "args": ["normal diagnostic text", "openai_agents_sdk"],
            },
            {
                "name": "projectAgentUiText",
                "args": ["已执行回滚并修复成功", "openai_agents_sdk"],
            },
            {
                "name": "projectAgentUiText",
                "args": [
                    "Bearer abc token=secret user@example.com postgres://u:p@db\n next",
                    "openai_agents_sdk",
                ],
            },
            {
                "name": "projectAgentUiText",
                "args": ["已执行回滚", "custom"],
            },
        ]
    )

    assert results[:9] == [
        "未选择（冲突待人工确认）",
        "database_slowdown",
        "database_slowdown",
        "deployment_regression",
        "未选择",
        "deployment_regression",
        "未选择",
        "normal diagnostic text",
        "[未验证操作声明已省略]",
    ]
    redacted = str(results[9])
    assert "Bearer" not in redacted
    assert "secret" not in redacted
    assert "user@example.com" not in redacted
    assert "postgres://" not in redacted
    assert "\n" not in redacted
    assert "[REDACTED]" in redacted
    assert "[REDACTED_EMAIL]" in redacted
    assert "[REDACTED_URL]" in redacted
    assert results[10] == "已执行回滚"


def test_v7_agent_text_suppresses_active_past_actions_only_for_sdk() -> None:
    claims = [
        "I updated the production configuration successfully.",
        "I rolled back the deployment.",
        "We already applied the config change.",
    ]

    results = _run_app_exports(
        [
            {"name": "projectAgentUiText", "args": [claim, layer]}
            for layer in ["openai_agents_sdk", "custom"]
            for claim in claims
        ]
    )

    assert results[:3] == ["[未验证操作声明已省略]"] * 3
    assert results[3:] == claims


def test_v11_review_visibility_is_run_owned_and_candidate_led() -> None:
    accepted = {
        "id": "candidate-accepted",
        "summary": "accepted",
        "rank": 1,
        "confidence": 0.9,
        "supporting_finding_ids": [],
        "contradicting_finding_ids": [],
        "supporting_evidence_ids": ["ev-1"],
        "contradicting_evidence_ids": [],
        "rationale": "bounded",
        "uncertainty": "",
    }
    rejected = {**accepted, "id": "candidate-rejected", "rank": 2}
    review = {
        "authority_mode": "agent",
        "runtime_run_id": "run-v11",
        "diagnostic_status": "complete",
        "run_status": "completed",
        "lead_decision": {
            "action": "conclude",
            "task_ids": [],
            "candidate_ids": [accepted["id"]],
        },
        "candidates": [accepted, rejected],
    }
    run = {
        "status": "completed",
        "authority_mode": "agent",
        "runtime_run_id": "run-v11",
        "diagnostic_status": "complete",
    }
    results = _run_app_exports(
        [
            {"name": "isUsableV11Review", "args": [review, run]},
            {"name": "isUsableV11Review", "args": [review, run, None]},
            {
                "name": "isUsableV11Review",
                "args": [review, {**run, "runtime_run_id": "prior-run"}],
            },
            {
                "name": "isUsableV11Review",
                "args": [review, {**run, "diagnostic_status": "partial"}],
            },
            {
                "name": "isUsableV11Review",
                "args": [review, {**run, "status": "partial"}],
            },
            {
                "name": "isUsableV11Review",
                "args": [
                    {**review, "run_status": "partial"},
                    run | {"status": "partial"},
                    "run-v11",
                ],
            },
            {
                "name": "isUsableV11Review",
                "args": [
                    {**review, "diagnostic_status": "partial"},
                    run | {"diagnostic_status": "partial"},
                    "run-v11",
                ],
            },
            {
                "name": "isUsableV11Review",
                "args": [review, run, "run-v11"],
            },
            {
                "name": "isUsableV11Review",
                "args": [review, run, "prior-run"],
            },
            {
                "name": "selectVisibleCandidates",
                "args": [review, run, [], "run-v11"],
            },
            {
                "name": "selectVisibleCandidates",
                "args": [
                    {**review, "diagnostic_status": "inconclusive"},
                    run,
                    [],
                    "run-v11",
                ],
            },
        ]
    )

    assert results[0] is False
    assert results[1] is False
    assert results[2:5] == [False, False, False]
    assert results[5:7] == [False, False]
    assert results[7] is True
    assert results[8] is False
    assert [item["id"] for item in results[9]] == ["candidate-accepted"]
    assert results[10] == []

    current = {**review, "diagnosis_contract_revision": 2}
    final_only = {
        **current,
        "lead_decision": None,
        "final_decision": {
            "actor": "critic",
            "action": "conclude",
            "candidate_ids": [rejected["id"], accepted["id"]],
        },
    }
    results = _run_app_exports(
        [
            {"name": "isUsableV11Review", "args": [current, run, "run-v11"]},
            {"name": "isUsableV11Review", "args": [final_only, run, "run-v11"]},
            {"name": "selectVisibleCandidates", "args": [final_only, run, [], "run-v11"]},
        ]
    )
    assert results[:2] == [False, True]
    assert [item["id"] for item in results[2]] == [rejected["id"], accepted["id"]]


def test_v7_review_guard_and_candidate_visibility_execute_against_payloads() -> None:
    agreement = {
        "execution_layer": "openai_agents_sdk",
        "run_status": "completed",
        "decision_status": "agreement",
        "baseline_cause_type": "deployment_regression",
        "selected_cause_type": "deployment_regression",
        "candidates": [{"id": "sdk-candidate"}],
    }
    partial_conflict = {
        **agreement,
        "run_status": "partial",
        "decision_status": "conflict",
        "selected_cause_type": None,
    }
    completed_run = {"status": "completed"}
    partial_run = {"status": "partial"}

    calls = [
        {"name": "isUsableV7Review", "args": [agreement, {"status": "failed"}]},
        {"name": "isUsableV7Review", "args": [agreement, {"status": "skipped"}]},
        {"name": "isUsableV7Review", "args": [agreement, partial_run]},
        {
            "name": "isUsableV7Review",
            "args": [{**agreement, "selected_cause_type": None}, completed_run],
        },
        {
            "name": "isUsableV7Review",
            "args": [
                {
                    **agreement,
                    "decision_status": "agent_leads",
                    "selected_cause_type": None,
                },
                completed_run,
            ],
        },
        {
            "name": "isUsableV7Review",
            "args": [
                {
                    **partial_conflict,
                    "decision_status": "fallback",
                },
                partial_run,
            ],
        },
        {
            "name": "isUsableV7Review",
            "args": [
                {
                    **agreement,
                    "decision_status": "conflict",
                    "selected_cause_type": "deployment_regression",
                },
                completed_run,
            ],
        },
        {"name": "isUsableV7Review", "args": [agreement, completed_run]},
        {"name": "isUsableV7Review", "args": [partial_conflict, partial_run]},
        {
            "name": "isUsableV7Review",
            "args": [
                {
                    **agreement,
                    "decision_status": "fallback",
                    "selected_cause_type": None,
                },
                completed_run,
            ],
        },
        {
            "name": "isUsableV7Review",
            "args": [{**agreement, "run_status": "partial"}, partial_run],
        },
        {
            "name": "isUsableV7Review",
            "args": [
                {**agreement, "baseline_cause_type": "database_slowdown"},
                completed_run,
            ],
        },
        {
            "name": "selectVisibleCandidates",
            "args": [
                {**agreement, "run_status": "partial"},
                partial_run,
                [{"id": "legacy"}],
            ],
        },
        {
            "name": "selectVisibleCandidates",
            "args": [agreement, {"status": "failed"}, [{"id": "legacy"}]],
        },
        {
            "name": "selectVisibleCandidates",
            "args": [agreement, completed_run, [{"id": "legacy"}]],
        },
        {
            "name": "selectVisibleCandidates",
            "args": [
                {**agreement, "execution_layer": "custom"},
                None,
                [{"id": "legacy"}],
            ],
        },
    ]

    results = _run_app_exports(calls)

    assert results[:10] == [
        False,
        False,
        False,
        False,
        False,
        True,
        False,
        True,
        True,
        True,
    ]
    assert results[10:13] == [False, False, []]
    assert results[13:] == [[], [{"id": "sdk-candidate"}], [{"id": "legacy"}]]


def test_ui_does_not_claim_production_changes_are_automatic() -> None:
    app = (FRONTEND / "src" / "App.tsx").read_text(encoding="utf-8").lower()

    forbidden_claims = [
        "automatically execute rollback",
        "automatically executes rollback",
        "automatically executed rollback",
        "automatic rollback",
        "automatically execute restart",
        "automatically executes restart",
        "automatically executed restart",
        "automatic restart",
        "automatically execute scale",
        "automatically executes scale",
        "automatically executed scale",
        "automatic scale",
        "automatically execute config change",
        "automatically executes config change",
        "automatically executed config change",
        "automatic config change",
        "自动执行回滚",
        "自动回滚",
        "自动执行重启",
        "自动重启",
        "自动执行扩容",
        "自动扩容",
        "自动执行配置变更",
        "自动配置变更",
    ]

    for claim in forbidden_claims:
        assert claim not in app


def test_frontend_source_contains_no_secret_tokens() -> None:
    pattern = re.compile(r"[A-Z][A-Z0-9_]*API_KEY|Bearer\s+\S+|-----BEGIN")
    offenders = [
        str(path.relative_to(FRONTEND))
        for path in (FRONTEND / "src").rglob("*.*")
        if pattern.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []
