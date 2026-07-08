import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"


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
