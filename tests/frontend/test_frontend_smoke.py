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


def test_app_contains_investigation_list_and_detail_ui_strings() -> None:
    app = (FRONTEND / "src" / "App.tsx").read_text(encoding="utf-8")

    assert "新建诊断" in app
    assert "诊断列表" in app
    assert "诊断详情" in app
    assert "证据链" in app
    assert "候选根因" in app
    assert "建议动作" in app
    assert "验证建议" in app
    assert "诊断报告" in app
    assert "任务规划" in app
    assert "Agent 执行过程" in app
    assert "工具调用" in app
    assert "共享上下文" in app
    assert "历史记忆" in app
    assert "Agent 判断" in app
    assert "候选根因排序" in app
    assert "证据链预览" in app
    assert "加载 RCA 工作台" in app
    assert "RCA 工作台加载失败" in app
    assert "暂无 RCA 工作台数据" in app
    assert "类型" in app
    assert "关联根因" in app
    assert "严重度" in app
    assert "缺口" in app
    assert "支持判断" in app
    assert "反对判断" in app
    assert "支持证据" in app
    assert "反对证据" in app
    assert "记录审批状态" in app
    assert "记录验证结果" in app
    assert "·" not in app

    for old_label in [
        "New Investigation",
        "Investigation List",
        "Investigation Detail",
        "Evidence Chain",
        "Candidate Causes",
        "Recommended Actions",
        "Verification Suggestions",
        "Diagnosis Report",
        "Task Plan",
        "Tool Calls",
        "Shared Context",
        "Historical Memory",
        "Create Diagnosis",
        "Record approval status",
        "Record verification result",
    ]:
        assert old_label not in app


def test_api_exposes_v4_agent_process_methods_and_v5_rca_workbench_methods() -> None:
    api = (FRONTEND / "src" / "api.ts").read_text(encoding="utf-8")

    for method_name in [
        "getPlan",
        "getTasks",
        "getAgentExecutions",
        "getContextFacts",
        "getToolCalls",
        "getMemoryHits",
        "getTaskGraph",
        "getAgentFindings",
        "getCoordinationReview",
        "getRcaWorkbench",
    ]:
        assert f"function {method_name}" in api


def test_api_exposes_v6_react_trace_method() -> None:
    api = (FRONTEND / "src" / "api.ts").read_text(encoding="utf-8")

    assert "export type ReActTrace" in api
    assert "getReActTrace" in api
    assert "/react-trace" in api


def test_app_contains_react_trace_panel_copy() -> None:
    app = (FRONTEND / "src" / "App.tsx").read_text(encoding="utf-8")

    assert "ReAct 推理过程" in app
    assert "只读" in app
    assert "输入:" in app
    assert "tool_input" in app
    assert "getReActTrace" in app


def test_app_contains_v7_hybrid_rca_copy() -> None:
    app = (FRONTEND / "src" / "App.tsx").read_text(encoding="utf-8")

    for label in [
        "混合 RCA 裁决",
        "多 Agent 复核一致",
        "存在冲突，需要人工确认",
        "多 Agent 主要候选，尚未确认",
        "多 Agent 复核未完成，以下为确定性 RCA 结果",
        "第 1 轮",
        "第 2 轮修订",
        "事实（证据）",
        "确定性推断",
        "Agent 推断",
        "建议（仅建议，未执行）",
        "不确定性",
    ]:
        assert label in app
    assert "已确认候选" not in app


def test_v7_workbench_keeps_conflicts_unselected_and_findings_separate() -> None:
    app = (FRONTEND / "src" / "App.tsx").read_text(encoding="utf-8")

    assert "const selectedCause = selectDisplayedCause(" in app
    assert "const v7FindingsByAgent = groupFindingsByAgent(v7Findings)" in app
    assert "const customFindingsByAgent = groupFindingsByAgent(customFindings)" in app
    assert re.search(
        r"<h2>Agent 推断</h2>.*?"
        r"<AgentFindingList findingsByAgent=\{run \? v7FindingsByAgent : findingsByAgent\}",
        app,
        re.S,
    )


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

    app = (FRONTEND / "src" / "App.tsx").read_text(encoding="utf-8")
    selector = re.search(
        r"export function selectDisplayedCause(?P<body>.*?)\n\}\n\n"
        r"export function isUsableV7Review",
        app,
        re.S,
    )
    assert selector is not None
    assert "hasReview" not in selector.group("body")


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


def test_all_v7_free_text_render_sites_use_the_ui_projection() -> None:
    app = (FRONTEND / "src" / "App.tsx").read_text(encoding="utf-8")

    for expression in [
        "projectAgentUiText(finding.summary, finding.execution_layer)",
        "projectAgentUiText(finding.rationale, finding.execution_layer)",
        "finding.gaps.map((gap) => projectAgentUiText(gap, finding.execution_layer))",
        "projectAgentUiText(review.summary ?? \"\", review.execution_layer)",
        "projectAgentUiText(review.uncertainty ?? \"\", review.execution_layer)",
        "projectAgentUiText(candidate.summary, persistedReview?.execution_layer)",
        "projectAgentUiText(candidate.rationale, persistedReview?.execution_layer)",
        "projectAgentUiText(candidate.uncertainty, persistedReview?.execution_layer)",
    ]:
        assert expression in app

    assert "const review = isUsableV7Review(persistedReview, run)" in app
    assert "const candidates = selectVisibleCandidates(" in app
    assert re.search(
        r"<h2>既有 Agent 判断</h2>.*?"
        r"<AgentFindingList findingsByAgent=\{customFindingsByAgent\}",
        app,
        re.S,
    )


def test_api_exposes_v7_workbench_fields() -> None:
    api = (FRONTEND / "src" / "api.ts").read_text(encoding="utf-8")

    for field in [
        "execution_layer",
        "analysis_round",
        "revises_finding_id",
        "run_status",
        "decision_status",
        "baseline_cause_type",
        "selected_cause_type",
        "coordination_review",
        "agent_executions",
        "multi_agent_run",
    ]:
        assert field in api

    diagnosis_task = re.search(r"export type DiagnosisTask = \{(?P<body>.*?)\n\};", api, re.S)
    assert diagnosis_task is not None
    assert "execution_layer?: AgentExecutionLayer" in diagnosis_task.group("body")
    assert "analysis_round?: 1 | 2 | null" in diagnosis_task.group("body")


def test_frontend_uses_vite_proxy_by_default() -> None:
    api = (FRONTEND / "src" / "api.ts").read_text(encoding="utf-8")
    vite_config = (FRONTEND / "vite.config.ts").read_text(encoding="utf-8")

    assert 'import.meta.env.VITE_API_BASE_URL ?? ""' in api
    assert '"/investigations": "http://127.0.0.1:8000"' in vite_config


def test_topbar_stats_allows_long_urls_to_wrap() -> None:
    styles = (FRONTEND / "src" / "styles.css").read_text(encoding="utf-8")
    topbar_stats = re.search(r"\.topbar-stats\s*\{[^}]+\}", styles)

    assert topbar_stats is not None
    assert "overflow-wrap: anywhere" in topbar_stats.group(0)


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
