"""V11 四条冻结的 data-only 诊断 skill 记录。

skill 只是版本化提示策略（name/version/when_to_use/required_tools/steps/
expected_evidence/stop_conditions），不包含任何可执行内容；spec 7.6 明确
禁止 Skill registry 服务、动态代码加载、文件系统发现或 MCP。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from backend.domain.tool_calls import ToolSpec

SKILL_CATALOG_VERSION = "v11-skills-v1"


@dataclass(frozen=True)
class DiagnosticSkill:
    name: str
    version: str
    when_to_use: str
    required_tools: tuple[str, ...]
    steps: tuple[str, ...]
    expected_evidence: tuple[str, ...]
    stop_conditions: tuple[str, ...]


DIAGNOSTIC_SKILLS: tuple[DiagnosticSkill, ...] = (
    DiagnosticSkill(
        name="first_failure_timeline",
        version="1.0.0",
        when_to_use="多信号 onset 不一致，需要对齐最早的指标、日志、trace、变更与告警起点。",
        required_tools=(
            "query_metrics",
            "read_logs",
            "query_traces",
            "read_deployments",
            "query_related_alerts",
        ),
        steps=(
            "按事件窗口查询关键指标与告警的最早异常点。",
            "对齐日志错误与 trace 错误的最早出现时间。",
            "对比变更时间，确定 first failure 候选边界。",
        ),
        expected_evidence=(
            "metric_trend", "log_pattern", "trace_error", "deployment", "related_alert"
        ),
        stop_conditions=("所有信号源均已覆盖或明确缺席。", "时间线已收敛到单一最早边界。"),
    ),
    DiagnosticSkill(
        name="trace_backtracking",
        version="1.0.0",
        when_to_use="错误沿调用链传播，需要回溯到最后一个健康边界。",
        required_tools=("query_traces", "query_dependencies"),
        steps=(
            "从事件服务的错误 span 出发沿 parent/child 链向上游回溯。",
            "标记延迟关键路径上的最后一个 ok span 边界。",
        ),
        expected_evidence=("trace_error", "trace_path", "trace_latency", "dependency_health"),
        stop_conditions=("到达 trace 根或健康边界。", "无更多上游 span 可查。"),
    ),
    DiagnosticSkill(
        name="change_and_peer_comparison",
        version="1.0.0",
        when_to_use="怀疑变更回归或单实例异常，需要变更前后与健康对等体对比。",
        required_tools=(
            "read_deployments",
            "query_metrics",
            "read_runtime_state",
            "read_service_catalog",
        ),
        steps=(
            "列出事件窗口内的变更记录。",
            "对比变更实例与健康对等体的指标与运行时状态。",
        ),
        expected_evidence=("deployment", "metric_trend", "runtime_state", "service_metadata"),
        stop_conditions=("变更与对等体差异已确认或排除。"),
    ),
    DiagnosticSkill(
        name="causal_falsification",
        version="1.0.0",
        when_to_use="已有候选解释，需要主动寻找反证与可区分的替代解释。",
        required_tools=(
            "query_metrics",
            "read_logs",
            "query_traces",
            "read_runtime_state",
            "query_related_alerts",
            "lookup_memory",
        ),
        steps=(
            "列出候选机制预测应出现但尚未观测到的信号。",
            "查询这些缺失信号与候选的反证。",
            "对比 verified memory 中相似历史事件的区分特征。",
        ),
        expected_evidence=(
            "metric_trend", "log_pattern", "trace_error", "runtime_state", "verified_incident"
        ),
        stop_conditions=("反证足以否决候选或候选通过全部证伪检查。", "预算耗尽。"),
    ),
)


def validate_skill_catalog(
    skills: tuple[DiagnosticSkill, ...], agent_specs: list[ToolSpec]
) -> None:
    """skill 的 required_tools 必须全部来自当前 Agent manifest 且名称唯一。"""
    available = {spec.name for spec in agent_specs}
    names = [skill.name for skill in skills]
    if len(set(names)) != len(names):
        raise ValueError("diagnostic skill names must be unique")
    for skill in skills:
        missing = set(skill.required_tools) - available
        if missing:
            raise ValueError(
                f"skill {skill.name} requires tools outside the agent manifest: "
                f"{sorted(missing)}"
            )


def skill_catalog_hash(skills: tuple[DiagnosticSkill, ...]) -> str:
    """对完整 catalog（含顺序与版本）生成稳定哈希，用于执行契约冻结。"""
    canonical = json.dumps(
        [asdict(skill) for skill in skills],
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def skill_catalog_identity(agent_specs: list[ToolSpec]) -> dict[str, str]:
    """返回可持久化的 catalog 身份；加载即校验，失败快速暴露。"""
    validate_skill_catalog(DIAGNOSTIC_SKILLS, agent_specs)
    return {
        "catalog_version": SKILL_CATALOG_VERSION,
        "catalog_hash": skill_catalog_hash(DIAGNOSTIC_SKILLS),
        "skill_names": ",".join(f"{skill.name}@{skill.version}" for skill in DIAGNOSTIC_SKILLS),
    }
