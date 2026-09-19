import { FormEvent, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { OpenRcaBenchmark } from "./OpenRcaBenchmark";
import { RuntimeWorkbench } from "./RuntimeWorkbench";
import {
  API_BASE_URL,
  createManualInvestigation,
  getAgentExecutions,
  getContextFacts,
  getInvestigation,
  getMemoryHits,
  getPlan,
  getRcaWorkbench,
  getTaskGraph,
  getTasks,
  getToolCalls,
  listInvestigations,
  updateActionStatus,
  updateVerificationStatus,
  type ActionStatus,
  type AgentExecutionLayer,
  type AgentExecution,
  type AgentFinding,
  type ContextFact,
  type CoordinationDecisionStatus,
  type CoordinationReview,
  type DiagnosisPlan,
  type DiagnosisTask,
  type InvestigationRecord,
  type InvestigationStrategy,
  type InvestigationSummary,
  type MemoryItem,
  type MultiAgentRunSummary,
  type RecommendedAction,
  type RcaWorkbench,
  type RootCauseCandidate,
  type TaskGraph,
  type ToolCallRecord,
  type VerificationStatus,
} from "./api";

const statusLabels: Record<string, string> = {
  approved: "已批准",
  cancelled: "已取消",
  completed: "已完成",
  done: "已完成",
  failed: "失败",
  partial: "部分完成",
  passed: "已通过",
  pending: "待处理",
  proposed: "待审批",
  rejected: "已驳回",
  running: "诊断中",
  skipped: "已跳过",
  success: "成功",
};

const adaptiveStopLabels: Record<string, string> = {
  budget_exhausted: "预算耗尽",
  duplicate_query: "重复查询",
  failed: "执行失败",
  no_new_evidence: "无新增证据",
  round_limit: "轮次上限",
  sufficient_evidence: "证据充分",
  timeout: "超时",
};

function allowedActionTargets(action: RecommendedAction): ActionStatus[] {
  if (action.status === "proposed") {
    return action.requires_approval
      ? ["approved", "rejected", "skipped"]
      : ["done", "rejected", "skipped"];
  }
  return action.status === "approved" ? ["done", "skipped"] : [];
}

function allowedVerificationTargets(status: VerificationStatus): VerificationStatus[] {
  return status === "pending" ? ["passed", "failed", "skipped"] : [];
}
const decisionLabels: Record<CoordinationDecisionStatus, string> = {
  agreement: "多 Agent 复核一致",
  conflict: "存在冲突，需要人工确认",
  agent_leads: "多 Agent 主要候选，尚未确认",
  fallback: "多 Agent 复核未完成，以下为确定性 RCA 结果",
};

export function selectDisplayedCause(
  decisionStatus: CoordinationDecisionStatus,
  selectedCause: string | null | undefined,
  baselineCause: string,
) {
  if (decisionStatus === "conflict") {
    return "未选择（冲突待人工确认）";
  }
  if (decisionStatus === "fallback") {
    return baselineCause;
  }
  if (selectedCause) {
    return selectedCause;
  }
  return "未选择";
}

export function isUsableV7Review(
  review: CoordinationReview | null | undefined,
  run: MultiAgentRunSummary | null | undefined,
): review is CoordinationReview {
  if (
    review?.execution_layer !== "openai_agents_sdk" ||
    !run ||
    !["completed", "partial"].includes(run.status) ||
    review.run_status !== run.status ||
    !review.decision_status
  ) {
    return false;
  }
  if (review.decision_status === "conflict") {
    return review.selected_cause_type === null;
  }
  if (review.decision_status === "agreement") {
    return (
      run.status === "completed" &&
      Boolean(review.baseline_cause_type) &&
      review.selected_cause_type === review.baseline_cause_type
    );
  }
  if (review.decision_status === "agent_leads") {
    return review.selected_cause_type !== null && review.selected_cause_type !== undefined;
  }
  return true;
}

export function isUsableV11Review(
  review: CoordinationReview | null | undefined,
  run: MultiAgentRunSummary | null | undefined,
  activeRuntimeRunId?: string | null,
): review is CoordinationReview {
  if (
    review?.authority_mode !== "agent" ||
    run?.authority_mode !== "agent" ||
    !review.runtime_run_id ||
    review.runtime_run_id !== run.runtime_run_id ||
    !activeRuntimeRunId ||
    activeRuntimeRunId !== review.runtime_run_id ||
    !["completed", "partial"].includes(run.status) ||
    review.run_status !== run.status ||
    !["complete", "partial", "inconclusive"].includes(review.diagnostic_status ?? "") ||
    review.diagnostic_status !== run.diagnostic_status ||
    (review.diagnostic_status === "partial" && run.status !== "partial") ||
    (review.diagnostic_status !== "partial" && run.status !== "completed") ||
    (review.diagnosis_contract_revision === 2 && !review.final_decision) ||
    !(review.final_decision ?? review.lead_decision)
  ) {
    return false;
  }

  const decision = review.final_decision ?? review.lead_decision!;
  if (["complete", "partial"].includes(review.diagnostic_status ?? "")) {
    return decision.action === "conclude" && decision.candidate_ids.length > 0;
  }
  return (
    decision.action === "inconclusive" &&
    (!("task_ids" in decision) || decision.task_ids.length === 0) &&
    decision.candidate_ids.length === 0
  );
}

export function selectVisibleCandidates(
  review: CoordinationReview | null | undefined,
  run: MultiAgentRunSummary | null | undefined,
  legacyCandidates: RootCauseCandidate[],
  activeRuntimeRunId?: string | null,
) {
  if (review?.authority_mode === "agent") {
    if (!isUsableV11Review(review, run, activeRuntimeRunId)) return [];
    if (review.diagnostic_status === "inconclusive") return [];
    const orderedIds = (review.final_decision ?? review.lead_decision)?.candidate_ids ?? [];
    return orderedIds.flatMap((id) => review.candidates.filter((candidate) => candidate.id === id));
  }
  if (review?.execution_layer !== "openai_agents_sdk") {
    return legacyCandidates;
  }
  return isUsableV7Review(review, run) ? review.candidates : [];
}

export function projectAgentUiText(text: string, executionLayer?: AgentExecutionLayer) {
  if (executionLayer !== "openai_agents_sdk") {
    return text;
  }
  if (
    /已执行|已修复|修复成功|回滚|重启|扩容|缩容|配置.*修改|修复.*完成|配置变更|\b(?:i|we)\s+(?:successfully\s+)?updated\s+(?:the\s+)?(?:production\s+)?config(?:uration)?(?:\s+successfully)?\b|\b(?:i|we)\s+(?:already\s+)?rolled\s+back\s+(?:the\s+)?(?:deployment|service|release)\b|\b(?:i|we)\s+(?:already\s+)?applied\s+(?:the\s+)?config(?:uration)?\s+(?:change|update)\b|\b(?:ssh|rollback|restart|scale|rebooted|repaired|fixed|executed)\b/isu.test(
      text,
    )
  ) {
    return "[未验证操作声明已省略]";
  }

  return text
    .replace(/\bbearer\s+[^\s,;]+/giu, "[REDACTED]")
    .replace(
      /\b(?:[\w-]*(?:token|secret|password|passwd|pwd)|(?:api|access|private)[_-]?key|key)\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;]+)/giu,
      "[REDACTED]",
    )
    .replace(/\b[a-z][a-z0-9+.-]*:\/\/[^\s/@:]+:[^\s/@]+@[^\s]+/giu, "[REDACTED_URL]")
    .replace(/\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/giu, "[REDACTED_EMAIL]")
    .trim()
    .replace(/\s+/gu, " ");
}

export function projectV11UiText(text: string | null | undefined) {
  if (!text) return "";
  if (
    /(system\s+prompt|developer\s+message|private\s+reasoning|chain\s+of\s+thought|thought\s+process|internal\s+prompt|原始提示词|私有推理|思维链|系统提示)/iu.test(
      text,
    )
  ) {
    return "[内部推理内容已省略]";
  }
  return projectAgentUiText(text, "openai_agents_sdk");
}

function formatDate(value?: string | null) {
  if (!value) {
    return "暂无";
  }
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function formatPercent(value: number) {
  return `${Math.round(value * 100)}%`;
}

function formatIdList(values: string[]) {
  return values.length > 0 ? values.join(", ") : "无";
}

export function groupToolCallsByAgentAndRound(
  toolCalls: ToolCallRecord[],
  tasks: DiagnosisTask[],
) {
  const taskById = new Map(tasks.map((task) => [task.id, task]));
  const groups = new Map<
    string,
    { agentName: string; round: 1 | 2 | null; calls: ToolCallRecord[] }
  >();
  for (const call of toolCalls) {
    const round = taskById.get(call.task_id)?.analysis_round ?? null;
    const key = `${call.agent_name}:${round ?? "fixed"}`;
    const group = groups.get(key) ?? { agentName: call.agent_name, round, calls: [] };
    group.calls.push(call);
    groups.set(key, group);
  }
  return [...groups.values()].sort(
    (left, right) =>
      left.agentName.localeCompare(right.agentName) || (left.round ?? 0) - (right.round ?? 0),
  );
}

function toolCallParameters(input: Record<string, unknown>) {
  const parameters = { ...input };
  delete parameters.reason;
  return Object.keys(parameters).length > 0 ? JSON.stringify(parameters) : "无";
}

function StatusBadge({ value }: { value: string }) {
  return <span className={`badge badge-${value}`}>{statusLabels[value] ?? value}</span>;
}

function MarkdownReport({ markdown }: { markdown: string }) {
  const lines = markdown.split("\n");

  return (
    <div className="markdown-report">
      {lines.map((line, index) => {
        const key = `${index}-${line}`;
        if (line.startsWith("### ")) {
          return <h4 key={key}>{line.slice(4)}</h4>;
        }
        if (line.startsWith("## ")) {
          return <h3 key={key}>{line.slice(3)}</h3>;
        }
        if (line.startsWith("# ")) {
          return <h2 key={key}>{line.slice(2)}</h2>;
        }
        if (line.startsWith("- ")) {
          return <p key={key} className="markdown-list-item">{line.slice(2)}</p>;
        }
        if (!line.trim()) {
          return <div key={key} className="markdown-break" />;
        }
        return <p key={key}>{line}</p>;
      })}
    </div>
  );
}

function ManualInvestigationForm({ onCreated }: { onCreated: (id: string) => void }) {
  const queryClient = useQueryClient();
  const [text, setText] = useState("");
  const [service, setService] = useState("checkout-service");
  const [environment, setEnvironment] = useState("prod");
  const [strategy, setStrategy] = useState<InvestigationStrategy>("fixed");

  const mutation = useMutation({
    mutationFn: createManualInvestigation,
    onSuccess: (summary) => {
      queryClient.invalidateQueries({ queryKey: ["investigations"] });
      onCreated(summary.id);
      setText("");
    },
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    mutation.mutate({ text, service, environment, strategy });
  }

  return (
    <form className="panel manual-form" onSubmit={submit}>
      <div className="panel-heading">
        <h2>新建诊断</h2>
      </div>
      <label>
        事件描述
        <textarea
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder="例如：checkout-service 发布后错误率升高"
          required
        />
      </label>
      <div className="field-row">
        <label>
          服务
          <input value={service} onChange={(event) => setService(event.target.value)} required />
        </label>
        <label>
          环境
          <input
            value={environment}
            onChange={(event) => setEnvironment(event.target.value)}
            required
          />
        </label>
        <label>
          调查模式
          <select
            value={strategy}
            onChange={(event) => setStrategy(event.target.value as InvestigationStrategy)}
          >
            <option value="fixed">固定采集</option>
            <option value="adaptive">自适应调查</option>
          </select>
        </label>
      </div>
      {mutation.isError ? <p className="error">{mutation.error.message}</p> : null}
      <button type="submit" disabled={mutation.isPending || !text.trim()}>
        {mutation.isPending ? "创建中..." : "创建诊断"}
      </button>
    </form>
  );
}

function InvestigationList({
  investigations,
  selectedId,
  onSelect,
}: {
  investigations: InvestigationSummary[];
  selectedId?: string;
  onSelect: (id: string) => void;
}) {
  return (
    <section className="panel list-panel">
      <div className="panel-heading">
        <h2>诊断列表</h2>
        <span>{investigations.length}</span>
      </div>
      <div className="investigation-list">
        {investigations.map((item) => {
          return (
            <button
              className={item.id === selectedId ? "investigation-item selected" : "investigation-item"}
              key={item.id}
              onClick={() => onSelect(item.id)}
              type="button"
            >
              <span className="item-title">{item.title}</span>
              <span className="item-meta">{item.service}</span>
              <span className="item-footer">
                <StatusBadge value={item.status} />
                <span>{formatPercent(item.confidence)}</span>
              </span>
            </button>
          );
        })}
        {investigations.length === 0 ? (
          <div className="empty-state">暂无诊断记录。</div>
        ) : null}
      </div>
    </section>
  );
}

function InvestigationDetail({ investigation }: { investigation: InvestigationRecord }) {
  const topHypothesis = investigation.hypotheses[0];
  const topDiagnosis = investigation.report?.diagnoses[0];

  return (
    <section className="panel detail-panel">
      <div className="panel-heading">
        <h2>诊断详情</h2>
        <StatusBadge value={investigation.status} />
      </div>
      <div className="detail-grid">
        <div>
          <span className="label">服务</span>
          <strong>{investigation.event.service}</strong>
        </div>
        <div>
          <span className="label">环境</span>
          <strong>{investigation.event.environment}</strong>
        </div>
        <div>
          <span className="label">严重级别</span>
          <strong>{investigation.event.severity}</strong>
        </div>
        <div>
          <span className="label">开始时间</span>
          <strong>{formatDate(investigation.event.started_at)}</strong>
        </div>
      </div>
      <h3>{investigation.event.title}</h3>
      <p>{investigation.event.description}</p>
      {topHypothesis ? (
        <div className="callout">
          <span className="label">首选根因</span>
          <strong>{topHypothesis.cause_type}</strong>
          <span>置信度 {formatPercent(topHypothesis.confidence)}</span>
        </div>
      ) : null}
      {!topHypothesis && topDiagnosis ? (
        <div className="callout">
          <span className="label">V11 接受诊断</span>
          <strong>{topDiagnosis.affected_entity ?? "未指定实体"}</strong>
          <span>{topDiagnosis.failure_mechanism ?? topDiagnosis.summary}</span>
          <span>置信度 {formatPercent(topDiagnosis.confidence)}</span>
        </div>
      ) : null}
      {investigation.report?.authority_mode === "agent" ? (
        <p className="muted">
          权威模式: agent · 状态: {investigation.report.diagnostic_status ?? "pending"} ·
          Run: {investigation.report.runtime_run_id ?? "未知"}
        </p>
      ) : null}
      {investigation.failure_reason ? <p className="error">{investigation.failure_reason}</p> : null}
    </section>
  );
}

function EvidenceList({ investigation }: { investigation: InvestigationRecord }) {
  return (
    <section className="panel">
      <div className="panel-heading">
        <h2>证据链</h2>
        <span>{investigation.evidence.length}</span>
      </div>
      <div className="evidence-list">
        {investigation.evidence.map((item) => (
          <article className="evidence-item" key={item.id}>
            <div>
              <StatusBadge value={item.status} />
              <span className="provider">{item.provider}</span>
              <span className="timestamp">{formatDate(item.timestamp)}</span>
            </div>
            <strong>{item.summary}</strong>
            <p>
              {item.kind} / 置信度 {formatPercent(item.confidence)}
            </p>
            {item.error_message ? <p className="error">{item.error_message}</p> : null}
          </article>
        ))}
      </div>
    </section>
  );
}

function Hypotheses({ investigation }: { investigation: InvestigationRecord }) {
  const diagnoses = investigation.report?.diagnoses ?? [];
  const isV11 = investigation.report?.authority_mode === "agent";
  const items = isV11 ? diagnoses : investigation.hypotheses;
  return (
    <section className="panel">
      <div className="panel-heading">
        <h2>候选根因</h2>
        <span>{items.length}</span>
      </div>
      <div className="hypothesis-list">
        {isV11
          ? diagnoses.map((candidate) => (
              <article className="hypothesis-item" key={candidate.id}>
                <div className="score-row">
                  <strong>{projectV11UiText(candidate.affected_entity ?? "未指定实体")}</strong>
                  <span>{formatPercent(candidate.confidence)}</span>
                </div>
                <p>{projectV11UiText(candidate.failure_mechanism ?? candidate.summary)}</p>
                <p className="muted">候选 ID: {candidate.id}</p>
              </article>
            ))
          : investigation.hypotheses.map((hypothesis) => (
              <article className="hypothesis-item" key={hypothesis.id}>
                <div className="score-row">
                  <strong>{hypothesis.cause_type}</strong>
                  <span>{formatPercent(hypothesis.confidence)}</span>
                </div>
                <p>{hypothesis.summary}</p>
                {hypothesis.next_actions.length > 0 ? (
                  <p className="muted">下一步: {hypothesis.next_actions.join(", ")}</p>
                ) : null}
              </article>
            ))}
      </div>
    </section>
  );
}

function groupFindingsByAgent(findings: AgentFinding[]) {
  return findings.reduce<Record<string, AgentFinding[]>>((groups, finding) => {
    groups[finding.agent_name] = [...(groups[finding.agent_name] ?? []), finding];
    return groups;
  }, {});
}

function AgentFindingList({ findingsByAgent }: { findingsByAgent: Record<string, AgentFinding[]> }) {
  return (
    <div className="compact-list">
      {Object.entries(findingsByAgent).map(([agentName, findings]) => (
        <article className="compact-row" key={agentName}>
          <strong>{agentName}</strong>
          {findings.map((finding) => (
            <div key={finding.id}>
              <p>
                {finding.runtime_run_id
                  ? projectV11UiText(finding.summary)
                  : projectAgentUiText(finding.summary, finding.execution_layer)}
              </p>
              <div className="process-detail">
                {finding.execution_layer === "openai_agents_sdk" ? (
                  <>
                    <span>{finding.analysis_round === 2 ? "第 2 轮修订" : "第 1 轮"}</span>
                    {finding.revises_finding_id ? (
                      <span>修订自: {finding.revises_finding_id}</span>
                    ) : null}
                  </>
                ) : null}
                <span>类型: {finding.finding_type}</span>
                <span>关联根因: {finding.related_cause_type ?? "无"}</span>
                <span>严重度: {finding.severity}</span>
                <span>置信度 {formatPercent(finding.confidence)}</span>
                <span className="evidence-link">证据: {formatIdList(finding.evidence_ids)}</span>
                <span>
                  缺口: {formatIdList(
                    finding.gaps.map((gap) =>
                      finding.runtime_run_id
                        ? projectV11UiText(gap)
                        : projectAgentUiText(gap, finding.execution_layer),
                    ),
                  )}
                </span>
              </div>
              <p className="muted">
                {finding.runtime_run_id
                  ? projectV11UiText(finding.rationale)
                  : projectAgentUiText(finding.rationale, finding.execution_layer)}
              </p>
            </div>
          ))}
        </article>
      ))}
    </div>
  );
}

function RcaWorkbenchPanel({ investigationId }: { investigationId: string }) {
  const workbenchQuery = useQuery<RcaWorkbench>({
    queryKey: ["rca-workbench", investigationId],
    queryFn: () => getRcaWorkbench(investigationId),
    enabled: Boolean(investigationId),
  });
  const legacyCandidates = workbenchQuery.data?.candidates ?? [];
  const run = workbenchQuery.data?.multi_agent_run ?? null;
  const agentConfig = workbenchQuery.data?.agent_config ?? null;
  const persistedReview = workbenchQuery.data?.coordination_review;
  const activeRuntimeRunId = workbenchQuery.data?.investigation.active_runtime_run_id;
  const v11Review = isUsableV11Review(persistedReview, run, activeRuntimeRunId)
    ? persistedReview
    : null;
  const isV11Projection = Boolean(v11Review || persistedReview?.authority_mode === "agent");
  const v11Decision = v11Review?.final_decision ?? v11Review?.lead_decision;
  const review = isUsableV7Review(persistedReview, run) ? persistedReview : null;
  const candidates = selectVisibleCandidates(
    persistedReview,
    run,
    legacyCandidates,
    activeRuntimeRunId,
  );
  const visibleCandidateIds = new Set(candidates.map((candidate) => candidate.id));
  const hiddenCandidateIds = new Set(
    legacyCandidates
      .map((candidate) => candidate.id)
      .filter((candidateId) => !visibleCandidateIds.has(candidateId)),
  );
  const edges = (workbenchQuery.data?.graph_seed.edges ?? []).filter(
    (edge) => !hiddenCandidateIds.has(edge.source) && !hiddenCandidateIds.has(edge.target),
  );
  const baselineHypothesis = workbenchQuery.data?.investigation.hypotheses[0];
  const baselineCause = review?.baseline_cause_type ?? baselineHypothesis?.cause_type ?? "未知";
  const baselineConfidence = baselineHypothesis?.confidence;
  const decisionStatus = review?.decision_status ?? "fallback";
  const selectedCause = selectDisplayedCause(
    decisionStatus,
    review?.selected_cause_type,
    baselineCause,
  );
  const findings = workbenchQuery.data?.findings ?? [];
  const findingsByAgent = groupFindingsByAgent(findings);
  const v7Findings = findings.filter(
    (finding) => finding.execution_layer === "openai_agents_sdk",
  );
  const customFindings = findings.filter(
    (finding) => finding.execution_layer !== "openai_agents_sdk",
  );
  const v7FindingsByAgent = groupFindingsByAgent(v7Findings);
  const customFindingsByAgent = groupFindingsByAgent(customFindings);
  const revisionCount = v7Findings.filter((finding) => finding.analysis_round === 2).length;
  const reviewSummary = review
    ? projectAgentUiText(review.summary ?? "", review.execution_layer)
    : "";
  const reviewUncertainty = review
    ? projectAgentUiText(review.uncertainty ?? "", review.execution_layer)
    : "";
  const safeRunFailureReason = isV11Projection
    ? projectV11UiText(run?.failure_reason ?? "")
    : projectAgentUiText(
        run?.failure_reason ?? "",
        run ? "openai_agents_sdk" : undefined,
      );
  const evidenceIds = (workbenchQuery.data?.evidence ?? []).map((item) => item.id);
  const recommendationTitles = (workbenchQuery.data?.investigation.actions ?? []).map(
    (action) => action.title,
  );
  const v11Diagnoses = v11Review
    ? selectVisibleCandidates(v11Review, run, legacyCandidates, activeRuntimeRunId)
    : [];
  const v11Executions = workbenchQuery.data?.agent_executions ?? [];
  const v11Actors = [...new Set(v11Executions.map((execution) => execution.agent_name))];
  const v11TaskIds = [...new Set(v11Executions.map((execution) => execution.task_id))];
  const v11Rounds = [...new Set(
    v11Executions
      .map((execution) => execution.analysis_round)
      .filter((round): round is 1 | 2 => round === 1 || round === 2),
  )].sort();
  const v11EvidenceIds = workbenchQuery.data?.evidence.map((item) => item.id) ?? [];
  const isEmpty =
    Object.keys(findingsByAgent).length === 0 && candidates.length === 0 && edges.length === 0 && !run;

  return (
    <section className="panel rca-workbench">
      <div className="panel-heading">
        <h2>{run ? "混合 RCA 裁决" : "Agent 判断"}</h2>
        <span>{workbenchQuery.data?.findings.length ?? 0}</span>
      </div>
      {workbenchQuery.isLoading ? (
        <div className="empty-state">加载 RCA 工作台...</div>
      ) : workbenchQuery.isError ? (
        <p className="error">RCA 工作台加载失败：{workbenchQuery.error.message}</p>
      ) : isEmpty ? (
        <div className="empty-state">暂无 RCA 工作台数据。</div>
      ) : (
        <>
          {run ? (
            <>
              {isV11Projection ? (
                <div className="detail-grid">
                  <div>
                    <span className="label">权威模式</span>
                    <strong>agent</strong>
                  </div>
                  <div>
                    <span className="label">诊断状态</span>
                    <StatusBadge value={v11Review?.diagnostic_status ?? "inconclusive"} />
                  </div>
                  <div>
                    <span className="label">Runtime Run</span>
                    <strong>{v11Review?.runtime_run_id ?? run.runtime_run_id ?? "未知"}</strong>
                  </div>
                  <div>
                    <span className="label">接受诊断</span>
                    <strong>{projectV11UiText(v11Diagnoses[0]?.affected_entity ?? "无")}</strong>
                    {v11Diagnoses[0]?.failure_mechanism ? (
                      <span>{projectV11UiText(v11Diagnoses[0].failure_mechanism)}</span>
                    ) : null}
                  </div>
                  {run.model_provider && run.model_name ? (
                    <div>
                      <span className="label">Provider / model</span>
                      <strong>{run.model_provider} / {run.model_name}</strong>
                    </div>
                  ) : null}
                </div>
              ) : (
              <div className="detail-grid">
                <div>
                  <span className="label">运行状态</span>
                  <StatusBadge value={run.status} />
                </div>
                <div>
                  <span className="label">裁决</span>
                  <strong>{decisionLabels[decisionStatus]}</strong>
                </div>
                <div>
                  <span className="label">确定性 RCA baseline</span>
                  <strong>{baselineCause}</strong>
                  {baselineConfidence === undefined ? null : (
                    <span>{formatPercent(baselineConfidence)}</span>
                  )}
                </div>
                <div>
                  <span className="label">最终候选</span>
                  <strong>{selectedCause}</strong>
                </div>
                {review && review.model_provider && review.model_name ? (
                  <div>
                    <span className="label">Provider / model</span>
                    <strong>{review.model_provider} / {review.model_name}</strong>
                  </div>
                ) : run.model_provider && run.model_name ? (
                  <div>
                    <span className="label">Provider / model</span>
                    <strong>{run.model_provider} / {run.model_name}</strong>
                  </div>
                ) : null}
                {agentConfig ? (
                  <>
                    <div>
                      <span className="label">实现状态</span>
                      <strong>{agentConfig.implementation_status}</strong>
                    </div>
                    <div>
                      <span className="label">认证状态</span>
                      <strong>{agentConfig.certification_status}</strong>
                    </div>
                  </>
                ) : null}
                {run.primary_stabilization_category ? (
                  <div>
                    <span className="label">Stabilization</span>
                    <strong>{run.primary_stabilization_category}</strong>
                    {run.secondary_stabilization_categories?.length ? (
                      <span>{run.secondary_stabilization_categories.join(", ")}</span>
                    ) : null}
                  </div>
                ) : null}
              </div>
              )}

              {isV11Projection ? (
                <div className="compact-list">
                  <article className="compact-row">
                    <strong>V11 接受诊断</strong>
                    {v11Diagnoses.length ? v11Diagnoses.map((candidate) => (
                      <p key={candidate.id}>
                        {projectV11UiText(candidate.affected_entity ?? "未指定实体")} · {projectV11UiText(candidate.failure_mechanism ?? candidate.summary)}
                        （置信度 {formatPercent(candidate.confidence)}）
                      </p>
                    )) : <p>当前没有接受的诊断候选。</p>}
                  </article>
                  <article className="compact-row">
                    <strong>备选候选</strong>
                    <p>
                      {(workbenchQuery.data?.investigation.report?.alternatives ?? []).map(
                        (candidate) => projectV11UiText(candidate.summary),
                      ).join("；") || "无"}
                    </p>
                  </article>
                  <article className="compact-row">
                    <strong>Critic / 证据缺口</strong>
                    <p>{v11Review?.critic_assessments?.map((item) => projectV11UiText(item.summary)).join("；") || "暂无 Critic 摘要"}</p>
                    <p>{workbenchQuery.data?.investigation.report?.evidence_gaps?.map((item) => projectV11UiText(item)).join("；") || "未记录证据缺口"}</p>
                  </article>
                  {v11Decision ? (
                    <article className="compact-row">
                      <strong>{v11Review?.final_decision ? "最终诊断" : "Lead decision"}: {v11Decision.action}</strong>
                      <p>{projectV11UiText(v11Decision.summary)}</p>
                      {v11Review?.final_decision?.uncertainty ? (
                        <p>最可能原因（暂定）：{projectV11UiText(v11Review.final_decision.uncertainty)}</p>
                      ) : null}
                    </article>
                  ) : null}
                  <article className="compact-row">
                    <strong>证据</strong>
                    <p>{v11EvidenceIds.join(", ") || "未记录证据"}</p>
                  </article>
                  <article className="compact-row">
                    <strong>执行上下文</strong>
                    <p>Actor: {v11Actors.join(", ") || "未记录"}</p>
                    <p>Task IDs: {v11TaskIds.join(", ") || "未记录"}</p>
                    <p>分析轮次: {v11Rounds.length ? v11Rounds.map((round) => `第 ${round} 轮`).join("、") : "未记录"}</p>
                  </article>
                  <article className="compact-row">
                    <strong>运行用量</strong>
                    <p>
                      tokens {run.total_input_tokens ?? 0}/{run.total_output_tokens ?? 0} ·
                      耗时 {run.elapsed_time_ms ?? 0} ms · Investigator {run.investigator_count ?? 0}
                    </p>
                  </article>
                  {v11Review?.diagnostic_status === "inconclusive" ? (
                    <p className="muted">not_activated: 没有激活可接受的 V11 诊断投影。</p>
                  ) : null}
                  {safeRunFailureReason ? <p className="error">失败原因: {safeRunFailureReason}</p> : null}
                </div>
              ) : (
              <div className="compact-list">
                <article className="compact-row">
                  <strong>事实（证据）</strong>
                  <p className="evidence-link">证据 IDs: {formatIdList(evidenceIds)}</p>
                </article>
                <article className="compact-row">
                  <strong>确定性推断</strong>
                  <p>{baselineHypothesis?.summary ?? baselineCause}</p>
                </article>
                <article className="compact-row">
                  <strong>Agent 推断</strong>
                  <p>{reviewSummary || "本次多 Agent 复核未形成可持久化裁决。"}</p>
                  <p>
                    冲突复核: {revisionCount > 0 ? `${revisionCount} 条第 2 轮修订` : "未触发第 2 轮修订"}
                  </p>
                </article>
                <article className="compact-row">
                  <strong>建议（仅建议，未执行）</strong>
                  <p>{recommendationTitles.length > 0 ? recommendationTitles.join("；") : "暂无建议"}</p>
                </article>
                <article className="compact-row">
                  <strong>不确定性</strong>
                  <p>{reviewUncertainty || safeRunFailureReason || "本次未提供额外不确定性说明。"}</p>
                  {safeRunFailureReason ? (
                    <p className="error">失败原因: {safeRunFailureReason}</p>
                  ) : null}
                </article>
              </div>
              )}

              <div className="panel-heading">
                <h2>{isV11Projection ? "V11 Agent 推断" : "Agent 推断"}</h2>
                <span>{v7Findings.length}</span>
              </div>
            </>
          ) : null}
          <AgentFindingList findingsByAgent={run ? v7FindingsByAgent : findingsByAgent} />
          {run ? (
            <>
              <div className="panel-heading">
                <h2>既有 Agent 判断</h2>
                <span>{customFindings.length}</span>
              </div>
              <AgentFindingList findingsByAgent={customFindingsByAgent} />
            </>
          ) : null}

          <div className="panel-heading">
            <h2>候选根因排序</h2>
            <span>{candidates.length}</span>
          </div>
          <div className="compact-list">
            {candidates.map((candidate) => (
              <article className="compact-row" key={candidate.id}>
                <div className="score-row">
                  <strong>#{candidate.rank} {candidate.cause_type}</strong>
                  <span>{formatPercent(candidate.confidence)}</span>
                </div>
                <p>{isV11Projection ? projectV11UiText(candidate.summary) : projectAgentUiText(candidate.summary, persistedReview?.execution_layer)}</p>
                <p>{isV11Projection ? projectV11UiText(candidate.rationale) : projectAgentUiText(candidate.rationale, persistedReview?.execution_layer)}</p>
                <div className="process-detail">
                  <span>支持判断: {formatIdList(candidate.supporting_finding_ids)}</span>
                  <span>反对判断: {formatIdList(candidate.contradicting_finding_ids)}</span>
                  <span>支持证据: {formatIdList(candidate.supporting_evidence_ids)}</span>
                  <span>反对证据: {formatIdList(candidate.contradicting_evidence_ids)}</span>
                </div>
                <p className="muted">
                  {isV11Projection ? projectV11UiText(candidate.uncertainty) : projectAgentUiText(candidate.uncertainty, persistedReview?.execution_layer)}
                </p>
              </article>
            ))}
          </div>

          <div className="panel-heading">
            <h2>证据链预览</h2>
            <span>{edges.length}</span>
          </div>
          <div className="compact-list">
            {edges.map((edge, index) => (
              <div className="compact-row evidence-link" key={`${edge.source}-${edge.relation}-${edge.target}-${index}`}>
                {edge.source} {edge.relation} {edge.target}
              </div>
            ))}
          </div>
        </>
      )}
    </section>
  );
}

function RecommendedActions({ investigation }: { investigation: InvestigationRecord }) {
  const queryClient = useQueryClient();
  const [notes, setNotes] = useState<Record<string, string>>({});
  const mutation = useMutation({
    mutationFn: ({
      actionId,
      status,
      note,
    }: {
      actionId: string;
      status: ActionStatus;
      note: string;
    }) => updateActionStatus(investigation.id, actionId, status, note),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["investigation", investigation.id] });
      queryClient.invalidateQueries({ queryKey: ["investigations"] });
    },
  });

  return (
    <section className="panel">
      <div className="panel-heading">
        <h2>建议动作</h2>
        <span>{investigation.actions.length}</span>
      </div>
      <div className="action-list">
        {investigation.actions.map((action) => (
          <article className="workflow-item" key={action.id}>
            <div className="workflow-heading">
              <strong>{action.title}</strong>
              <StatusBadge value={action.status} />
            </div>
            <p>{action.description}</p>
            <div className="meta-row">
              <span>风险级别: {action.risk_level}</span>
              <span>{action.requires_approval ? "需记录审批状态" : "可选记录审批状态"}</span>
              {action.related_candidate_ids?.length ? (
                <span>候选: {action.related_candidate_ids.join(", ")}</span>
              ) : null}
            </div>
            <div className="control-row">
              <select
                aria-label={`${action.title} 的动作状态`}
                value={action.status}
                disabled={allowedActionTargets(action).length === 0}
                onChange={(event) =>
                  mutation.mutate({
                    actionId: action.id,
                    status: event.target.value as ActionStatus,
                    note: notes[action.id] ?? action.note ?? "",
                  })
                }
              >
                <option value={action.status}>{statusLabels[action.status] ?? action.status}</option>
                {allowedActionTargets(action).map((status) => (
                  <option key={status} value={status}>
                    {statusLabels[status] ?? status}
                  </option>
                ))}
              </select>
              <input
                placeholder="记录审批状态"
                value={notes[action.id] ?? action.note ?? ""}
                onChange={(event) => setNotes({ ...notes, [action.id]: event.target.value })}
              />
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}

function VerificationSuggestions({ investigation }: { investigation: InvestigationRecord }) {
  const queryClient = useQueryClient();
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [evidenceRefs, setEvidenceRefs] = useState<Record<string, string>>({});
  const [relationRefs, setRelationRefs] = useState<Record<string, string>>({});
  const resultEvidence = investigation.evidence.filter(
    (item) => item.status === "success" || item.status === "partial",
  );
  const mutation = useMutation({
    mutationFn: ({
      verificationId,
      status,
      resultNote,
      resultEvidenceIds,
      relatedActionIds,
      relatedCauseTypes,
      relatedCandidateIds,
    }: {
      verificationId: string;
      status: VerificationStatus;
      resultNote: string;
      resultEvidenceIds: string[];
      relatedActionIds: string[];
      relatedCauseTypes: string[];
      relatedCandidateIds: string[];
    }) =>
      updateVerificationStatus(
        investigation.id,
        verificationId,
        status,
        resultNote,
        resultEvidenceIds,
        relatedActionIds,
        relatedCauseTypes,
        relatedCandidateIds,
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["investigation", investigation.id] });
      queryClient.invalidateQueries({ queryKey: ["investigations"] });
    },
  });

  return (
    <section className="panel">
      <div className="panel-heading">
        <h2>验证建议</h2>
        <span>{investigation.verification_suggestions.length}</span>
      </div>
      <div className="action-list">
        {investigation.verification_suggestions.map((verification) => (
          <article className="workflow-item" key={verification.id}>
            <div className="workflow-heading">
              <strong>{verification.title}</strong>
              <StatusBadge value={verification.status} />
            </div>
            <p>{verification.description}</p>
            <p className="muted">预期信号: {verification.expected_signal}</p>
            <div className="control-row">
              <select
                aria-label={`${verification.title} 的验证结果`}
                value={verification.status}
                disabled={allowedVerificationTargets(verification.status).length === 0}
                onChange={(event) => {
                  const status = event.target.value as VerificationStatus;
                  const relation = relationRefs[verification.id] ?? "";
                  mutation.mutate({
                    verificationId: verification.id,
                    status,
                    resultNote: notes[verification.id] ?? verification.result_note ?? "",
                    resultEvidenceIds:
                      status === "skipped"
                        ? []
                        : [evidenceRefs[verification.id] ?? ""].filter(Boolean),
                    relatedActionIds:
                      status === "skipped" || !relation.startsWith("action:")
                        ? []
                        : [relation.slice("action:".length)],
                    relatedCauseTypes:
                      status === "skipped" || !relation.startsWith("cause:")
                        ? []
                        : [relation.slice("cause:".length)],
                    relatedCandidateIds:
                      status === "skipped" || !relation.startsWith("candidate:")
                        ? []
                        : [relation.slice("candidate:".length)],
                  });
                }}
              >
                <option value={verification.status}>
                  {statusLabels[verification.status] ?? verification.status}
                </option>
                {allowedVerificationTargets(verification.status).map((status) => (
                  <option key={status} value={status}>
                    {statusLabels[status] ?? status}
                  </option>
                ))}
              </select>
              <input
                placeholder="记录验证结果"
                value={notes[verification.id] ?? verification.result_note ?? ""}
                onChange={(event) =>
                  setNotes({ ...notes, [verification.id]: event.target.value })
                }
              />
              {verification.status === "pending" ? (
                <>
                  <select
                    aria-label={`${verification.title} 的结果证据`}
                    value={evidenceRefs[verification.id] ?? ""}
                    onChange={(event) =>
                      setEvidenceRefs({ ...evidenceRefs, [verification.id]: event.target.value })
                    }
                  >
                    <option value="">选择结果 Evidence</option>
                    {resultEvidence.map((item) => (
                      <option key={item.id} value={item.id}>{item.id}</option>
                    ))}
                  </select>
                  <select
                    aria-label={`${verification.title} 的关联对象`}
                    value={relationRefs[verification.id] ?? ""}
                    onChange={(event) =>
                      setRelationRefs({ ...relationRefs, [verification.id]: event.target.value })
                    }
                  >
                    <option value="">选择关联 action、candidate 或 cause</option>
                    {investigation.actions.map((action) => (
                      <option key={action.id} value={`action:${action.id}`}>action: {action.id}</option>
                    ))}
                    {investigation.hypotheses.map((hypothesis) => (
                      <option key={hypothesis.cause_type} value={`cause:${hypothesis.cause_type}`}>
                        cause: {hypothesis.cause_type}
                      </option>
                    ))}
                    {(investigation.report?.diagnoses ?? []).map((candidate) => (
                      <option key={candidate.id} value={`candidate:${candidate.id}`}>
                        candidate: {candidate.id}
                      </option>
                    ))}
                  </select>
                </>
              ) : null}
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}

function AgentProcessPanels({ investigationId }: { investigationId: string }) {
  const planQuery = useQuery<DiagnosisPlan | null>({
    queryKey: ["plan", investigationId],
    queryFn: () => getPlan(investigationId),
    enabled: Boolean(investigationId),
  });
  const tasksQuery = useQuery<DiagnosisTask[]>({
    queryKey: ["tasks", investigationId],
    queryFn: () => getTasks(investigationId),
    enabled: Boolean(investigationId),
  });
  const executionsQuery = useQuery<AgentExecution[]>({
    queryKey: ["agent-executions", investigationId],
    queryFn: () => getAgentExecutions(investigationId),
    enabled: Boolean(investigationId),
  });
  const contextQuery = useQuery<ContextFact[]>({
    queryKey: ["context", investigationId],
    queryFn: () => getContextFacts(investigationId),
    enabled: Boolean(investigationId),
  });
  const toolCallsQuery = useQuery<ToolCallRecord[]>({
    queryKey: ["tool-calls", investigationId],
    queryFn: () => getToolCalls(investigationId),
    enabled: Boolean(investigationId),
  });
  const workbenchQuery = useQuery<RcaWorkbench>({
    queryKey: ["rca-workbench", investigationId],
    queryFn: () => getRcaWorkbench(investigationId),
    enabled: Boolean(investigationId),
  });
  const memoryQuery = useQuery<MemoryItem[]>({
    queryKey: ["memory", investigationId],
    queryFn: () => getMemoryHits(investigationId),
    enabled: Boolean(investigationId),
  });
  const taskGraphQuery = useQuery<TaskGraph>({
    queryKey: ["task-graph", investigationId],
    queryFn: () => getTaskGraph(investigationId),
    enabled: Boolean(investigationId),
  });
  const tasks = tasksQuery.data?.length
    ? tasksQuery.data
    : planQuery.data?.tasks ?? [];
  const executions = executionsQuery.data ?? [];
  const toolCalls = toolCallsQuery.data ?? [];
  const adaptiveRun = workbenchQuery.data?.multi_agent_run ?? null;
  const displayedToolCalls =
    adaptiveRun?.strategy === "adaptive"
      ? workbenchQuery.data?.tool_calls ?? []
      : toolCalls;
  const toolCallGroups = useMemo(
    () => groupToolCallsByAgentAndRound(displayedToolCalls, tasks),
    [displayedToolCalls, tasks],
  );
  const contextFacts = contextQuery.data ?? [];
  const memoryHits = memoryQuery.data ?? [];
  const dependencyCount = taskGraphQuery.data?.edges.length ?? 0;
  const tasksLoading = tasks.length === 0 && (tasksQuery.isLoading || planQuery.isLoading);
  const tasksError = tasks.length === 0 ? tasksQuery.error ?? planQuery.error : null;

  return (
    <>
      <section className="panel agent-process-panel">
        <div className="panel-heading">
          <h2>任务规划</h2>
          <span>
            {tasks.length} 项, {taskGraphQuery.isError ? "依赖数据不可用" : `依赖 ${dependencyCount} 条`}
          </span>
        </div>
        {taskGraphQuery.isError ? (
          <p className="error">任务依赖图不可用：{taskGraphQuery.error.message}</p>
        ) : null}
        {tasksLoading ? (
          <div className="empty-state">加载中...</div>
        ) : tasksError ? (
          <p className="error">{tasksError.message}</p>
        ) : tasks.length === 0 ? (
          <div className="empty-state">暂无任务规划。</div>
        ) : (
          <div className="process-list">
            {tasks.map((task) => (
              <article className="process-item" key={task.id}>
                <div className="workflow-heading">
                  <strong>{task.title}</strong>
                  <StatusBadge value={task.status} />
                </div>
                <div className="process-detail">
                  <span>类型: {task.task_type}</span>
                  <span>Agent: {task.agent_name}</span>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>

      <section className="panel agent-process-panel">
        <div className="panel-heading">
          <h2>Agent 执行过程</h2>
          <span>{executions.length}</span>
        </div>
        {executionsQuery.isLoading ? (
          <div className="empty-state">加载中...</div>
        ) : executionsQuery.isError ? (
          <p className="error">{executionsQuery.error.message}</p>
        ) : executions.length === 0 ? (
          <div className="empty-state">暂无 Agent 执行记录。</div>
        ) : (
          <div className="process-list">
            {executions.map((execution) => (
              <article className="process-item" key={execution.id}>
                <div className="workflow-heading">
                  <strong>{execution.agent_name}</strong>
                  <StatusBadge value={execution.status} />
                </div>
                <p>{execution.summary ?? "暂无摘要"}</p>
                <div className="process-detail">
                  {execution.step_kind ? <span>Step: {execution.step_kind}</span> : null}
                  {execution.attempt ? <span>Attempt: {execution.attempt}</span> : null}
                  {execution.failure_category && execution.failure_category !== "none" ? (
                    <span>Failure: {execution.failure_category}</span>
                  ) : null}
                  {execution.result_validation_category ? (
                    <span>Validation: {execution.result_validation_category}</span>
                  ) : null}
                  {execution.model_provider ? (
                    <span>
                      Provider: {execution.model_provider}
                      {execution.model_name ? ` / ${execution.model_name}` : ""}
                    </span>
                  ) : null}
                </div>
                {execution.execution_layer !== "openai_agents_sdk" && execution.error_message ? (
                  <p className="error">{execution.error_message}</p>
                ) : null}
              </article>
            ))}
          </div>
        )}
      </section>

      <section className="panel agent-process-panel">
        <div className="panel-heading">
          <h2>工具调用</h2>
          <span>
            {adaptiveRun?.strategy === "adaptive"
              ? `预算 ${adaptiveRun.tool_call_count ?? displayedToolCalls.length}/${adaptiveRun.max_total_tool_calls ?? 0}`
              : displayedToolCalls.length}
          </span>
        </div>
        {adaptiveRun ? (
          <div className="process-detail trace-run-status">
            <span>调查策略: {adaptiveRun.strategy}</span>
            <span>Adaptive 状态: {adaptiveRun.adaptive_status ?? "not_applicable"}</span>
          </div>
        ) : null}
        {adaptiveRun?.adaptive_stop_reason ? (
          <p className="trace-stop-reason">
            停止原因: {adaptiveStopLabels[adaptiveRun.adaptive_stop_reason] ?? adaptiveRun.adaptive_stop_reason}
          </p>
        ) : null}
        {toolCallsQuery.isLoading ? (
          <div className="empty-state">加载中...</div>
        ) : toolCallsQuery.isError ? (
          <p className="error">{toolCallsQuery.error.message}</p>
        ) : displayedToolCalls.length === 0 ? (
          <div className="empty-state">暂无工具调用。</div>
        ) : (
          <div className="tool-call-groups">
            {toolCallGroups.map((group) => (
              <section className="tool-call-group" key={`${group.agentName}-${group.round ?? "fixed"}`}>
                <div className="tool-call-group-heading">
                  <strong>{group.agentName}</strong>
                  <span>{group.round ? `第 ${group.round} 轮` : "固定采集"}</span>
                </div>
                <div className="process-list">
                  {group.calls.map((call) => (
                    <article className="process-item" key={call.id}>
                      <div className="workflow-heading">
                        <strong>{call.tool_name}</strong>
                        <StatusBadge value={call.status} />
                      </div>
                      <div className="process-detail">
                        <span>耗时: {call.duration_ms} ms</span>
                        <span>证据: {formatIdList(call.output_evidence_ids)}</span>
                      </div>
                      <p>
                        <strong>调用原因:</strong>{" "}
                        {projectAgentUiText(String(call.input.reason ?? "未提供"), "openai_agents_sdk")}
                      </p>
                      <p>
                        <strong>查询参数:</strong>{" "}
                        <code className="tool-parameters">{toolCallParameters(call.input)}</code>
                      </p>
                      {call.error_message ? <p className="error">{call.error_message}</p> : null}
                    </article>
                  ))}
                </div>
              </section>
            ))}
          </div>
        )}
      </section>

      <section className="panel agent-process-panel">
        <div className="panel-heading">
          <h2>共享上下文</h2>
          <span>{contextFacts.length}</span>
        </div>
        {contextQuery.isLoading ? (
          <div className="empty-state">加载中...</div>
        ) : contextQuery.isError ? (
          <p className="error">{contextQuery.error.message}</p>
        ) : contextFacts.length === 0 ? (
          <div className="empty-state">暂无共享上下文。</div>
        ) : (
          <div className="process-list">
            {contextFacts.map((fact) => (
              <article className="process-item" key={fact.id}>
                <div className="workflow-heading">
                  <strong>{fact.summary}</strong>
                  <span>{formatPercent(fact.confidence)}</span>
                </div>
                <div className="process-detail">
                  <span>类型: {fact.fact_type}</span>
                  <span>来源: {fact.source_agent}</span>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>

      <section className="panel agent-process-panel">
        <div className="panel-heading">
          <h2>历史记忆</h2>
          <span>{memoryHits.length}</span>
        </div>
        {memoryQuery.isLoading ? (
          <div className="empty-state">加载中...</div>
        ) : memoryQuery.isError ? (
          <p className="error">{memoryQuery.error.message}</p>
        ) : memoryHits.length === 0 ? (
          <div className="empty-state">暂无历史记忆。</div>
        ) : (
          <div className="process-list">
            {memoryHits.map((memory) => (
              <article className="process-item" key={memory.id}>
                <strong>{memory.summary}</strong>
                <div className="process-detail">
                  <span>类型: {memory.memory_type}</span>
                  <span>标签: {formatIdList(memory.tags)}</span>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>
    </>
  );
}

function ReportPanel({ investigation }: { investigation: InvestigationRecord }) {
  return (
    <section className="panel report-panel">
      <div className="panel-heading">
        <h2>诊断报告</h2>
      </div>
      {investigation.report ? (
        <MarkdownReport
          markdown={
            investigation.report.authority_mode === "agent"
              ? projectV11UiText(investigation.report.markdown)
              : investigation.report.markdown
          }
        />
      ) : (
        <div className="empty-state">该诊断尚未生成报告。</div>
      )}
    </section>
  );
}

export default function App() {
  const [activeView, setActiveView] = useState<"investigations" | "runtime" | "benchmark">(
    "investigations",
  );
  const [selectedId, setSelectedId] = useState<string>();
  const investigationsQuery = useQuery({
    queryKey: ["investigations"],
    queryFn: listInvestigations,
    enabled: activeView === "investigations",
  });

  const investigations = investigationsQuery.data ?? [];

  const activeId = selectedId ?? investigations[0]?.id;
  const detailQuery = useQuery({
    queryKey: ["investigation", activeId],
    queryFn: () => getInvestigation(activeId as string),
    enabled: activeView === "investigations" && Boolean(activeId),
  });

  const activeInvestigation = detailQuery.data;

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <h1>DiagOps 诊断控制台</h1>
          <p>API 地址: {API_BASE_URL}</p>
        </div>
        <nav className="view-tabs" aria-label="Primary views">
          <button
            className={activeView === "investigations" ? "active" : ""}
            onClick={() => setActiveView("investigations")}
            type="button"
          >
            Investigations
          </button>
          <button
            className={activeView === "runtime" ? "active" : ""}
            onClick={() => setActiveView("runtime")}
            type="button"
          >
            Runtime Workbench
          </button>
          <button
            className={activeView === "benchmark" ? "active" : ""}
            onClick={() => setActiveView("benchmark")}
            type="button"
          >
            OpenRCA Benchmark
          </button>
        </nav>
        <div className="topbar-stats">
          {activeView === "investigations" ? (
            <>
              <span>共 {investigations.length} 条诊断</span>
              <span>记录审批状态和验证结果</span>
            </>
          ) : activeView === "runtime" ? (
            <>
              <span>多会话 durable Runtime</span>
              <span>SSE + 历史回放</span>
            </>
          ) : (
            <>
              <span>Fixed / Adaptive</span>
              <span>只读冻结结果</span>
            </>
          )}
        </div>
      </header>

      {activeView === "benchmark" ? (
        <OpenRcaBenchmark />
      ) : activeView === "runtime" ? (
        <RuntimeWorkbench
          investigations={investigations}
          selectedInvestigationId={selectedId ?? investigations[0]?.id}
          onSelectInvestigation={setSelectedId}
        />
      ) : (
        <div className="workspace">
        <aside className="left-column">
          <ManualInvestigationForm onCreated={setSelectedId} />
          {investigationsQuery.isError ? (
            <div className="panel error-panel">{investigationsQuery.error.message}</div>
          ) : null}
          <InvestigationList
            investigations={investigations}
            selectedId={activeId}
            onSelect={setSelectedId}
          />
        </aside>

        <section className="center-column">
          {activeInvestigation ? (
            <>
              <InvestigationDetail investigation={activeInvestigation} />
              <EvidenceList investigation={activeInvestigation} />
              <Hypotheses investigation={activeInvestigation} />
              <RcaWorkbenchPanel investigationId={activeInvestigation.id} />
            </>
          ) : detailQuery.isError ? (
            <div className="panel error-panel">{detailQuery.error.message}</div>
          ) : detailQuery.isLoading ? (
            <div className="panel empty-state">加载诊断详情...</div>
          ) : (
            <div className="panel empty-state">选择或创建一个诊断开始。</div>
          )}
        </section>

        <aside className="right-column">
          {activeInvestigation ? (
            <>
              <RecommendedActions investigation={activeInvestigation} />
              <VerificationSuggestions investigation={activeInvestigation} />
              <AgentProcessPanels investigationId={activeInvestigation.id} />
              <ReportPanel investigation={activeInvestigation} />
            </>
          ) : (
            <div className="panel empty-state">审批、验证和报告会显示在这里。</div>
          )}
        </aside>
        </div>
      )}
    </main>
  );
}
